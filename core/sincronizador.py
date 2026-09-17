"""
Orquestador de sincronizacion contable (Fase 3).

Conecta las tres piezas de las fases anteriores en un flujo idempotente:

    state_store (idempotencia)  ->  mapper (traduce)  ->  execute (crea + postea)

La funcion sincronizar_entidad() implementa el patron comun a facturas y pagos:

  1. IDEMPOTENCIA: si (entidad, id_origen) ya esta PROCESADO, devuelve el id_odoo
     sin volver a tocar Odoo.
  2. MARCA PROCESANDO (para trazabilidad y evitar carreras).
  3. MAPEA el registro de origen al esquema Odoo (resuelve datos maestros).
  4. CREA el registro en Odoo (create) -> obtiene id_odoo.
  5. POSTEA (action_post) para generar los asientos contables.
  6. MARCA PROCESADO y guarda la correlacion id_origen <-> id_odoo.

Cualquier fallo marca ERROR (con el mensaje) y re-lanza una excepcion tipada
para que la capa HTTP la traduzca al codigo adecuado.
"""

from dataclasses import dataclass

from core import impuestos, mapper, state_store
from core.models_db import EstadoSync
from odoo_universal import (
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
)


class SincronizacionError(Exception):
    """
    Fallo de negocio durante la sincronizacion (mapeo, create o post).

    Lleva dos datos que el llamador necesita para decidir si hay algo que
    COMPENSAR en Odoo:
      id_odoo   - id del registro si llego a crearse (None si fallo antes).
      descuadre - True si el fallo fue de validacion de total, es decir: el
                  registro esta creado y POSTEADO en Odoo pero sus importes no
                  cuadran con el origen. Es el unico caso en que cancelar
                  automaticamente es seguro; el resto puede ser transitorio.
    """
    def __init__(self, mensaje, id_odoo=None, descuadre=False):
        super().__init__(mensaje)
        self.id_odoo = id_odoo
        self.descuadre = descuadre


@dataclass
class ResultadoSync:
    """Resultado de una sincronizacion."""
    id_origen: str
    id_odoo: int
    estado: str
    idempotente: bool  # True si ya estaba procesado y no se toco Odoo


def sincronizar_entidad(
    entidad: str,
    registro: dict,
    odoo: OdooUniversalAPI,
) -> ResultadoSync:
    """
    Orquesta la sincronizacion de un registro de origen hacia Odoo.

    Parametros:
      entidad  - clave en mappings.yaml (p.ej. "factura", "pago").
      registro - datos crudos de la DB de origen.
      odoo     - conector del tenant correspondiente.

    Devuelve ResultadoSync. Lanza SincronizacionError ante cualquier fallo
    (el estado queda como ERROR en el state store).
    """
    # id de origen segun la config de mapeo (necesario para idempotencia).
    try:
        id_origen = mapper.id_origen_de(entidad, registro)
    except mapper.MapeoError as e:
        raise SincronizacionError(str(e)) from e

    conf = mapper.cargar_config()[entidad]
    model_odoo = conf["model"]
    hash_payload = state_store.calcular_hash(registro)

    # 1 y 2. IDEMPOTENCIA + marca PROCESANDO en un solo paso ATOMICO.
    # No se comprueba "ya procesado" y luego se marca por separado: entre las
    # dos operaciones cabe otra peticion con el mismo id_origen, y ambas
    # crearian el registro en Odoo (duplicado contable). reservar() delega el
    # arbitraje en la UniqueConstraint de sync_map.
    id_odoo_existente = state_store.reservar(
        entidad, id_origen, model_odoo=model_odoo, hash_payload=hash_payload,
    )
    if id_odoo_existente is not None:
        state_store.log(
            entidad, "idempotente", "OK", id_origen,
            f"Ya procesado como id_odoo={id_odoo_existente}",
        )
        return ResultadoSync(
            id_origen=id_origen,
            id_odoo=id_odoo_existente,
            estado=EstadoSync.PROCESADO.value,
            idempotente=True,
        )

    # A partir de aqui se toca Odoo. Un fallo de CONEXION no es culpa del dato:
    # hay que LIBERAR la reserva (dejarla en PENDIENTE, conservando el id_odoo)
    # para que el reintento pueda retomarla y adoptar lo que hubiera quedado
    # creado. En PROCESANDO, reservar() la rechazaria para siempre.
    with state_store.reserva_liberada_si_cae_odoo(entidad, id_origen):
        return _sincronizar_contra_odoo(
            entidad, registro, odoo, id_origen, conf, model_odoo, hash_payload,
        )


def _sincronizar_contra_odoo(
    entidad: str,
    registro: dict,
    odoo: OdooUniversalAPI,
    id_origen: str,
    conf: dict,
    model_odoo: str,
    hash_payload: str,
) -> ResultadoSync:
    """
    Pasos que hablan con Odoo (mapeo -> create -> post -> validar total).

    Separado de sincronizar_entidad para que la reserva se libere de forma
    uniforme ante cualquier OdooConnectionError, ocurra en el paso que ocurra.
    """
    # 3. MAPEO (traduce y valida datos maestros).
    try:
        valores = mapper.mapear(entidad, registro, odoo)
    except mapper.MapeoError as e:
        state_store.marcar_estado(entidad, id_origen, EstadoSync.ERROR, error=str(e))
        state_store.log(entidad, "mapear", "ERROR", id_origen, str(e))
        raise SincronizacionError(f"Error de mapeo: {e}") from e

    # 4. CREATE en Odoo.
    # Si una pasada anterior se corto por un fallo de conexion DESPUES del
    # create, el id_odoo ya consta en sync_map: se adopta ese registro en vez
    # de crear un duplicado (Odoo no participa en la transaccion, asi que el
    # create pudo completarse aunque la respuesta no llegara).
    mapa_previo = state_store.buscar_mapeo(entidad, id_origen)
    if mapa_previo is not None and mapa_previo.id_odoo:
        id_odoo = mapa_previo.id_odoo
        state_store.log(
            entidad, "adoptar", "OK", id_origen,
            f"Se retoma id_odoo={id_odoo} de un intento interrumpido; no se recrea.",
        )
        return _postear_y_validar(
            entidad, registro, odoo, id_origen, conf, model_odoo, id_odoo,
        )

    try:
        id_odoo = odoo.execute(model_odoo, "create", valores)
        state_store.registrar_mapeo(
            entidad, id_origen,
            model_odoo=model_odoo, id_odoo=id_odoo,
            estado=EstadoSync.PROCESANDO, hash_payload=hash_payload,
        )
        state_store.log(entidad, "crear", "OK", id_origen, f"id_odoo={id_odoo}")
    except OdooExecutionError as e:
        state_store.marcar_estado(entidad, id_origen, EstadoSync.ERROR, error=str(e))
        state_store.log(entidad, "crear", "ERROR", id_origen, str(e))
        raise SincronizacionError(f"Error al crear en Odoo: {e}") from e

    return _postear_y_validar(
        entidad, registro, odoo, id_origen, conf, model_odoo, id_odoo,
    )


def _ya_posteado(odoo: OdooUniversalAPI, model_odoo: str, id_odoo: int) -> bool:
    """
    Si el registro ya esta posteado en Odoo. Se consulta al RETOMAR un intento
    interrumpido: action_post sobre algo ya posteado falla con "debe ser un
    borrador", asi que hay que saltarse el paso en vez de provocar ese error.

    account.payment usa otros estados (Odoo 19: in_process/paid), de ahi que se
    comprueben ambos vocabularios.
    """
    datos = odoo.execute(model_odoo, "read", [id_odoo], fields=["state"])
    # Ante una respuesta inesperada se responde False: como mucho se intenta un
    # action_post de mas, que Odoo rechaza con un error claro. Devolver True por
    # error seria peor: se saltaria el posteo y la factura quedaria en borrador
    # dada por buena.
    if not isinstance(datos, list) or not datos or not isinstance(datos[0], dict):
        return False
    return datos[0].get("state") in ("posted", "in_process", "paid")


def _postear_y_validar(
    entidad: str,
    registro: dict,
    odoo: OdooUniversalAPI,
    id_origen: str,
    conf: dict,
    model_odoo: str,
    id_odoo: int,
) -> ResultadoSync:
    """
    Postea el registro y valida su total. Separado del create para poder
    RETOMARLO sobre un id_odoo ya existente: si un intento anterior se corto
    tras el create, hay que continuar desde aqui, no crear otro registro.

    OJO: action_post NO es idempotente en Odoo 19. Reposteario un asiento ya
    posteado devuelve "El asiento (...) debe ser un borrador". Por eso al
    retomar se consulta primero el estado y solo se postea si sigue en draft.

    El registro puede pedir "postear": false para dejar el documento en
    BORRADOR (draft) en vez de contabilizarlo. En ese caso no se genera asiento
    contable y NO se valida el total (esa comprobacion lee amount_total de un
    documento posteado): la factura queda creada, sin contabilizar, a la espera
    de que alguien la confirme en Odoo. Por defecto se postea, para no cambiar
    el comportamiento de las integraciones que ya existen.
    """
    postear = registro.get("postear", True)

    if not postear:
        # Documento en borrador: creado pero sin contabilizar. No se postea ni
        # se valida el total (requiere el documento posteado). Se marca
        # PROCESADO igualmente: la sincronizacion cumplio lo que se le pidio.
        state_store.log(
            entidad, "postear", "OK", id_origen,
            f"id_odoo={id_odoo} dejado en BORRADOR (postear=false)",
        )
        state_store.marcar_estado(entidad, id_origen, EstadoSync.PROCESADO)
        return ResultadoSync(
            id_origen=id_origen,
            id_odoo=id_odoo,
            estado=EstadoSync.PROCESADO.value,
            idempotente=False,
        )

    # 5. POST (action_post) para generar los asientos contables.
    try:
        if not _ya_posteado(odoo, model_odoo, id_odoo):
            odoo.execute(model_odoo, "action_post", [id_odoo])
        state_store.log(entidad, "postear", "OK", id_origen, f"id_odoo={id_odoo}")
    except OdooExecutionError as e:
        # La factura/pago quedo creada pero en borrador. Se marca ERROR para
        # intervencion humana; el rollback automatico llegara en la Fase 5.
        state_store.marcar_estado(
            entidad, id_origen, EstadoSync.ERROR,
            error=f"Creado (id_odoo={id_odoo}) pero fallo action_post: {e}",
        )
        state_store.log(entidad, "postear", "ERROR", id_origen, str(e))
        raise SincronizacionError(
            f"Creado en Odoo (id={id_odoo}) pero fallo al postear: {e}",
            id_odoo=id_odoo,
        ) from e

    # 5b. VALIDACION DE TOTAL/IMPUESTOS (opcional, si la config lo pide).
    campo_total = conf.get("validar_total")
    if campo_total and registro.get(campo_total) is not None:
        try:
            # campo_total_odoo: donde guarda Odoo el importe de ESTA entidad.
            # account.move usa amount_total; account.payment, amount.
            impuestos.verificar_total(
                registro[campo_total], id_odoo, odoo,
                model_odoo=model_odoo,
                campo_odoo=conf.get("campo_total_odoo", "amount_total"),
            )
            state_store.log(entidad, "validar_total", "OK", id_origen, f"id_odoo={id_odoo}")
        except impuestos.DescuadreError as e:
            state_store.marcar_estado(
                entidad, id_origen, EstadoSync.ERROR,
                error=f"Posteado (id_odoo={id_odoo}) pero descuadre de total: {e}",
            )
            state_store.log(entidad, "validar_total", "ERROR", id_origen, str(e))
            raise SincronizacionError(
                f"Descuadre de total en Odoo (id={id_odoo}): {e}",
                id_odoo=id_odoo, descuadre=True,
            ) from e

    # 6. PROCESADO.
    state_store.marcar_estado(entidad, id_origen, EstadoSync.PROCESADO)
    return ResultadoSync(
        id_origen=id_origen,
        id_odoo=id_odoo,
        estado=EstadoSync.PROCESADO.value,
        idempotente=False,
    )
