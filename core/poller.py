"""
Poller: sincroniza desde la base de datos del CLIENTE hacia Odoo (modo pull).

Alternativa al modo push (el cliente hace POST a /facturas, /pagos...): aqui el
cliente deja registros en su propia base de datos (ver core/poller_source.py) y
el middleware los sondea periodicamente (Celery Beat) y los sincroniza.

Flujo de una pasada (procesar_lote):

  1. Lee un lote de filas PENDIENTE de la cola del cliente (FOR UPDATE SKIP
     LOCKED, para que varios workers no cojan la misma fila).
  2. Por cada fila llama a sincronizar_entidad() -> la MISMA maquinaria del modo
     push: idempotencia (sync_map), mapeo (mappings.yaml), create + post.
  3. Escribe el resultado de vuelta en la cola del cliente (PROCESADO | ERROR)
     para que el cliente lo vea sin llamar al middleware.

Doble fuente de verdad, resuelta: la cola del cliente dice QUE hay que enviar;
el sync_map del middleware dice QUE YA se envio. sincronizar_entidad consulta
sync_map antes de tocar Odoo (ya_procesado), asi que una fila reprocesada no
duplica nada en Odoo: solo se re-marca su resultado.

Politica de errores por fila (aislamiento): un fallo de DATOS en una fila la deja
en ERROR y el poller CONTINUA con las demas. Un fallo de CONEXION (Odoo caido)
aborta el lote entero y se propaga, para que Celery reintente la pasada completa
mas tarde (las filas ya procesadas son idempotentes).

Compensacion automatica del descuadre: la validacion de total corre DESPUES del
action_post, de modo que una factura descuadrada queda posteada en Odoo (asiento
contable real) aunque el middleware la marque ERROR. El poller la CANCELA
(rollback.cancelar_factura: button_draft + button_cancel) para no dejar
contabilidad huerfana. Solo se compensa el descuadre, no cualquier error: es el
unico caso en que consta que el registro esta mal. La fila queda en ERROR y NO
se reintenta sola —reintentarla recrearia y volveria a cancelar la misma factura
en bucle—; el cliente corrige el importe en origen y la reencola.
Se apaga con POLLER_CANCELAR_DESCUADRE=false.
"""

import logging
from dataclasses import dataclass

from core import poller_source, state_store
from core.state_store import ReservaOcupada
from core.asientos import AsientoError, crear_asiento
from core.inventario import InventarioError, ajustar_stock
from core.rollback import compensar_descuadre
from core.sincronizador import SincronizacionError, sincronizar_entidad
from odoo_universal import OdooConnectionError, OdooUniversalAPI, get_tenant

logger = logging.getLogger("api-odoo")


def _despachar(entidad: str, payload: dict, odoo: OdooUniversalAPI):
    """
    Envia una fila de la cola al modulo que le corresponde.

    La mayoria de entidades (factura, pago...) son traducciones declarativas y
    pasan por sincronizar_entidad + mappings.yaml. Pero inventario y asientos
    tienen esquema FIJO y su propia logica de negocio (modos de ajuste, cuadre
    debe/haber), asi que no viven en el YAML: sin este despacho, el modo pull
    solo admitiria facturas y pagos.

    Devuelve (id_odoo, numero_odoo, texto_resultado). numero_odoo es el numero
    fiscal de la factura ("INV/2026/00001"); se escribe de vuelta en la cola
    para que el cliente cruce su registro con la factura sin entrar a Odoo. Solo
    las facturas lo tienen: en ajustes de stock y asientos es None.
    """
    if entidad == "ajuste_stock":
        r = ajustar_stock(payload, odoo)
        return r.get("quant_id"), None, f"quant_id={r.get('quant_id')}"
    if entidad == "asiento":
        r = crear_asiento(payload, odoo)
        return r.get("id_odoo"), None, f"id_odoo={r.get('id_odoo')}"

    resultado = sincronizar_entidad(entidad, payload, odoo)
    # El numero fiscal SOLO se lee cuando la factura se acaba de crear. En un
    # reenvio idempotente NO se lee: la idempotencia promete no tocar Odoo, y un
    # read() lo romperia. En ese caso numero=None y marcar_resultado no
    # sobrescribe lo que ya hubiera en la cola.
    numero = None
    if not resultado.idempotente:
        numero = _numero_de_factura(entidad, resultado.id_odoo, odoo)
    return resultado.id_odoo, numero, (
        f"id_odoo={resultado.id_odoo}"
        + (f" ({numero})" if numero else "")
        + (" (idempotente)" if resultado.idempotente else "")
    )


def _numero_de_factura(entidad: str, id_odoo, odoo: OdooUniversalAPI):
    """
    Lee el numero fiscal ('name') de una factura recien creada.

    Solo para account.move; para otras entidades devuelve None sin tocar Odoo.
    Un fallo al leerlo NO debe tumbar la fila (ya esta creada y posteada), asi
    que se traga la excepcion: el numero es un dato de conveniencia.

    OJO: no llamar en un reenvio idempotente. La idempotencia promete no tocar
    Odoo; este read() lo romperia (ver el guardia en _despachar).
    """
    if not id_odoo or entidad not in ("factura", "nota_credito",
                                      "factura_proveedor", "nota_debito"):
        return None
    try:
        datos = odoo.execute("account.move", "read", [id_odoo], fields=["name"])
        return datos[0]["name"] if datos else None
    except Exception:
        return None


@dataclass
class ResultadoLote:
    """Resumen de una pasada del poller."""
    leidas: int       # filas tomadas de la cola
    procesadas: int   # sincronizadas con exito (o ya idempotentes)
    con_error: int    # filas marcadas ERROR por fallo de datos
    omitidas: int = 0  # las estaba procesando otra pasada (no son un error)


def _procesar_fila(fila: dict, odoo: OdooUniversalAPI):
    """
    Sincroniza una fila de la cola. Devuelve True si termino en PROCESADO,
    False si quedo en ERROR por un fallo de datos, y None si otra pasada la
    tenia reservada (no es un error: la termina el otro).

    Re-lanza OdooConnectionError para que el llamador aborte el lote (Odoo esta
    caido: no tiene sentido seguir con el resto).
    """
    fila_id = fila["id"]
    entidad = fila["entidad"]
    id_origen = fila["id_origen"]
    payload = fila["payload"]

    try:
        id_odoo, numero_odoo, detalle_ok = _despachar(entidad, payload, odoo)
        # El id y el numero de la factura se escriben de vuelta en la cola: es
        # lo que permite cruzar una nota de origen con su factura sin entrar a
        # Odoo ni consultar la base de control del middleware.
        poller_source.marcar_resultado(
            fila_id, "PROCESADO", id_odoo=id_odoo, numero_odoo=numero_odoo,
        )
        state_store.log(entidad, "poller", "OK", id_origen, f"cola#{fila_id} -> {detalle_ok}")
        return True
    except OdooConnectionError:
        # Fallo de conexion: no es culpa de la fila. Se deja PENDIENTE (no se
        # marca resultado) y se propaga para abortar el lote y reintentar luego.
        logger.warning(
            "Poller: Odoo no disponible al procesar cola#%s (%s/%s); "
            "se aborta el lote para reintentar.",
            fila_id, entidad, id_origen,
        )
        raise
    except ReservaOcupada as e:
        # Otra pasada del poller (o una peticion HTTP) esta procesando esta
        # misma fila ahora mismo. No es un error de datos ni un fallo: el
        # ganador la terminara. Se deja PENDIENTE -sin marcar resultado- para
        # que la siguiente pasada la recoja si hiciera falta.
        logger.info(
            "Poller: cola#%s (%s/%s) la esta procesando otro; se omite.",
            fila_id, entidad, id_origen,
        )
        return None
    except (SincronizacionError, InventarioError, AsientoError) as e:
        # Fallo de datos (mapeo, create, post, descuadre, cuadre, stock
        # insuficiente...): aisla la fila y sigue con el resto del lote.
        detalle = str(e)

        # COMPENSACION: un descuadre de total deja la factura POSTEADA en Odoo
        # (la validacion ocurre despues del action_post). Ese asiento contable
        # es real y nadie lo dio por bueno, asi que se cancela para no dejar
        # contabilidad huerfana esperando revision manual.
        #
        # Solo se compensa el descuadre: es el unico fallo en que sabemos que
        # el registro esta objetivamente mal. Un fallo de action_post, por
        # ejemplo, deja la factura en borrador (no contabiliza nada) y puede
        # deberse a una causa transitoria.
        detalle += compensar_descuadre(
            e, odoo, entidad=entidad, id_origen=id_origen,
            origen=f"el poller (cola#{fila_id})",
        )

        # Si el fallo ocurrio DESPUES del create (al postear, o por descuadre),
        # el documento existe en Odoo y el sincronizador guardo su id en el
        # sync_map. Se recupera para escribirlo tambien en la cola: sin el,
        # habria que buscar a mano que quedo a medias en Odoo.
        mapa = state_store.buscar_mapeo(entidad, id_origen)
        # La fila queda en ERROR, no PENDIENTE: reintentarla sola volveria a
        # crear y cancelar la misma factura en bucle. El cliente corrige el
        # importe en su sistema y la reencola.
        poller_source.marcar_resultado(
            fila_id, "ERROR", error_detalle=detalle,
            id_odoo=mapa.id_odoo if mapa else None,
        )
        state_store.log(entidad, "poller", "ERROR", id_origen, f"cola#{fila_id}: {detalle}")
        logger.info("Poller: fila cola#%s en ERROR: %s", fila_id, detalle)
        return False


def procesar_lote(tenant: str = "default", limite: int = 50) -> ResultadoLote:
    """
    Ejecuta una pasada del poller: toma un lote de la cola del cliente y lo
    sincroniza hacia Odoo. Devuelve un ResultadoLote con el resumen.

    Si el modo polling no esta configurado (sin SOURCE_DATABASE_URL), no hace
    nada y devuelve un lote vacio.
    """
    if not poller_source.polling_habilitado():
        logger.debug("Poller: SOURCE_DATABASE_URL no configurado; nada que hacer.")
        return ResultadoLote(leidas=0, procesadas=0, con_error=0)

    odoo = get_tenant(tenant)
    lote = poller_source.tomar_lote(limite)

    procesadas = 0
    con_error = 0
    omitidas = 0
    for fila in lote:
        r = _procesar_fila(fila, odoo)
        if r is None:
            omitidas += 1          # la esta procesando otro; no cuenta como error
        elif r:
            procesadas += 1
        else:
            con_error += 1

    if lote:
        logger.info(
            "Poller: lote de %s fila(s) -> %s ok, %s error, %s omitidas.",
            len(lote), procesadas, con_error, omitidas,
        )
    return ResultadoLote(
        leidas=len(lote), procesadas=procesadas,
        con_error=con_error, omitidas=omitidas,
    )
