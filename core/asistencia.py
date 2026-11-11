"""
Asistencia biometrica en Odoo (hr.attendance) - registrar fichajes.

Cada fichaje es un tramo de presencia de un empleado: una entrada (check_in) y,
cuando se produce, una salida (check_out). Odoo deriva solo las horas trabajadas
(worked_hours); no se le envian.

Como inventario.py y asientos.py, este modulo tiene esquema FIJO (no pasa por
mappings.yaml + el sincronizador generico): el emparejamiento entrada/salida y la
resolucion del empleado por un dato de negocio no encajan en el mapeo declarativo
1:1. Ver docs/INFORME-ASISTENCIA-BIOMETRICA.md.

Flujo (registrar_asistencia):
  1. Idempotencia por marcaje_id (state store, entidad "asistencia").
  2. Resuelve el empleado (employee_id) traduciendo un dato de negocio
     (por defecto 'barcode') contra hr.employee. Debe existir ya en la ficha.
  3. create del hr.attendance con check_in (y check_out si viene).

Entrada y salida en dos mensajes (recomendado por el informe): el proveedor
manda primero la entrada (solo check_in) y mas tarde la salida, ambos con el
MISMO marcaje_id. El segundo mensaje NO crea un fichaje nuevo: COMPLETA el que
quedo abierto escribiendole el check_out. Un fichaje completo en un solo mensaje
(check_in + check_out juntos) tambien es valido.

Idempotencia matizada: a diferencia de factura/pago, aqui un segundo mensaje con
el mismo marcaje_id es LEGITIMO si aporta el check_out que faltaba. Por eso el
modulo distingue "reenvio exacto" (mismo payload -> idempotente, no toca Odoo) de
"completar la salida" (trae check_out sobre un fichaje abierto -> write).

Las fechas se pasan a Odoo TAL CUAL las envia la API (ISO 8601, preferiblemente
UTC con sufijo 'Z'). El middleware no reinterpreta la zona: si la API manda hora
local sin zona, los fichajes quedarian corridos (ver informe).

Lanza AsistenciaError ante datos invalidos o fallo de Odoo.
"""

import logging
import os
from datetime import datetime, timezone

from core import state_store
from core.models_db import EstadoSync
from odoo_universal import OdooExecutionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

ENTIDAD = "asistencia"
MODEL = "hr.attendance"
MODEL_EMPLEADO = "hr.employee"


class AsistenciaError(Exception):
    """Fallo al registrar un fichaje (empleado no hallado, datos o Odoo)."""
    pass


def _campo_empleado() -> str:
    """
    Campo de hr.employee por el que se resuelve al trabajador.

    Por defecto 'barcode' (codigo de empleado), que es lo que un lector
    biometrico emite de forma nativa y Odoo trae como campo estandar y unico.
    Si el proveedor solo expone la cedula, se cambia a 'identification_id' SIN
    tocar codigo: solo esta variable de entorno. Ver informe, seccion de
    identificacion del trabajador.
    """
    return os.getenv("ASISTENCIA_CAMPO_EMPLEADO", "barcode").strip() or "barcode"


def _campo_biometrico() -> str:
    """
    Campo personalizado de hr.attendance donde se deja el identificador del
    dispositivo/sede. hr.attendance no trae un campo estandar para esto; el
    informe recomienda un campo personalizado 'x_biometrico_id' (texto).

    Si el campo no existe en la instancia, el fichaje se crea igual sin el
    (se registra un aviso): la trazabilidad por sede es deseable, no un
    bloqueo para registrar la asistencia.
    """
    return os.getenv("ASISTENCIA_CAMPO_BIOMETRICO", "x_biometrico_id").strip() or "x_biometrico_id"


def _validar_fecha(valor, campo: str) -> str:
    """
    Valida una fecha/hora ISO 8601 CON ZONA y la normaliza al formato que Odoo
    acepta por RPC: 'YYYY-MM-DD HH:MM:SS' en UTC (naive).

    Odoo NO acepta el ISO 8601 con 'T' ni con sufijo de zona ('Z'/'+00:00') por
    JSON-RPC: espera 'YYYY-MM-DD HH:MM:SS' y almacena en UTC. Fibex entrega la
    hora CON zona (ej. '2026-10-05T13:02:00Z' UTC, o '...-04:00' Venezuela), asi
    que aqui se parsea con su zona, se convierte a UTC y se formatea como lo
    espera Odoo. Convertir (no truncar) es lo correcto: una hora '-04:00' debe
    guardarse como su equivalente UTC, o el fichaje quedaria corrido 4 horas.

    Se EXIGE zona explicita (como pide el PDF de Fibex): una fecha sin zona se
    rechaza, para no guardar horas ambiguas. 'Z' se entiende desde Python 3.11
    normalizandolo a '+00:00'.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise AsistenciaError(f"Falta '{campo}' o no es una fecha ISO 8601.")
    texto = valor.strip()
    try:
        dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError as e:
        raise AsistenciaError(
            f"'{campo}' no es una fecha ISO 8601 valida: {valor!r}."
        ) from e
    if dt.tzinfo is None:
        raise AsistenciaError(
            f"'{campo}' debe traer zona horaria (sufijo 'Z' o '+/-HH:MM'): "
            f"{valor!r}. Una fecha sin zona guardaria horas corridas en Odoo."
        )
    # A UTC, naive, en el formato que Odoo entiende por RPC.
    dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


def _resolver_empleado(odoo: OdooUniversalAPI, codigo: str) -> int:
    """
    Traduce el identificador de negocio del trabajador (por defecto 'barcode')
    al employee_id real de Odoo. Debe existir ya en la ficha del empleado.

    Si no existe, se rechaza el marcaje con un error claro en vez de crear un
    fichaje huerfano (ver informe, "el flujo de extremo a extremo").
    """
    if not codigo or not str(codigo).strip():
        raise AsistenciaError("Falta 'empleado_codigo' (identificador del trabajador).")
    codigo = str(codigo).strip()
    campo = _campo_empleado()
    encontrados = odoo.execute(
        MODEL_EMPLEADO, "search_read",
        [[campo, "=", codigo]], fields=["id"], limit=1,
    )
    if not encontrados:
        raise AsistenciaError(
            f"No existe en Odoo un empleado (hr.employee) con {campo}='{codigo}'. "
            f"El identificador debe estar cargado en la ficha antes del fichaje."
        )
    return encontrados[0]["id"]


def _completar_salida(
    registro: dict, marcaje_id: str, previo, odoo: OdooUniversalAPI
) -> dict:
    """
    Segundo mensaje de un fichaje abierto: escribe el check_out en el
    hr.attendance que ya existe (mismo marcaje_id). NO crea un registro nuevo.

    'previo' es el SyncMap del primer mensaje (ya PROCESADO, con id_odoo).
    """
    check_out = _validar_fecha(registro.get("check_out"), "check_out")
    id_odoo = previo.id_odoo

    with state_store.reserva_liberada_si_cae_odoo(ENTIDAD, marcaje_id):
        # El fichaje de entrada pudo ser BORRADO en Odoo a mano (RR.HH.) despues
        # de que lo registraramos. Si ya no existe, un write daria un error crudo;
        # se detecta antes y se devuelve un mensaje reconocible ("ya no existe en
        # Odoo"), que es el caso 'fichaje_inexistente' del contrato de Fibex.
        try:
            existe = odoo.execute(MODEL, "search_count", [["id", "=", id_odoo]])
        except OdooExecutionError as e:
            state_store.log(ENTIDAD, "salida", "ERROR", marcaje_id, str(e))
            raise AsistenciaError(
                f"Error al verificar el fichaje id={id_odoo} en Odoo: {e}"
            ) from e
        if not existe:
            detalle = (
                f"El fichaje id={id_odoo} del marcaje {marcaje_id} ya no existe en "
                f"Odoo (fue eliminado); no se puede completar la salida."
            )
            state_store.marcar_estado(ENTIDAD, marcaje_id, EstadoSync.ERROR, error=detalle)
            state_store.log(ENTIDAD, "salida", "ERROR", marcaje_id, detalle)
            raise AsistenciaError(detalle)

        try:
            odoo.execute(MODEL, "write", [id_odoo], {"check_out": check_out})
        except OdooExecutionError as e:
            # No se marca ERROR: el fichaje de entrada quedo bien PROCESADO; solo
            # falla completar la salida (p.ej. check_out anterior al check_in).
            # Odoo valida el orden y los solapamientos.
            state_store.log(ENTIDAD, "salida", "ERROR", marcaje_id, str(e))
            raise AsistenciaError(
                f"Error al completar la salida del fichaje id={id_odoo}: "
                f"{e}"
            ) from e

    # Se actualiza el hash: el registro ahora incluye el check_out, para que un
    # reenvio exacto posterior se detecte como idempotente.
    state_store.registrar_mapeo(
        ENTIDAD, marcaje_id, model_odoo=MODEL, id_odoo=id_odoo,
        estado=EstadoSync.PROCESADO,
        hash_payload=state_store.calcular_hash(registro),
    )
    state_store.log(ENTIDAD, "salida", "OK", marcaje_id, f"id_odoo={id_odoo} check_out={check_out}")
    logger.info("ASISTENCIA_SALIDA | marcaje=%s id_odoo=%s", marcaje_id, id_odoo)
    return {
        "marcaje_id": marcaje_id,
        "id_odoo": id_odoo,
        "estado": EstadoSync.PROCESADO.value,
        "accion": "salida_completada",
        "idempotente": False,
    }


def registrar_asistencia(registro: dict, odoo: OdooUniversalAPI) -> dict:
    """
    Registra un fichaje en Odoo (hr.attendance), o completa la salida de uno
    abierto si llega un segundo mensaje con el mismo marcaje_id.

    Campos del registro (ver docs/INFORME-ASISTENCIA-BIOMETRICA.md):
      marcaje_id       (obligatorio) identificador unico del marcaje en origen.
      empleado_codigo  (obligatorio) identificador del trabajador (p.ej. barcode).
      check_in         (obligatorio) fecha/hora de entrada, ISO 8601 UTC.
      check_out        (opcional) fecha/hora de salida; puede llegar despues.
      biometrico_id    (opcional) identificador del dispositivo/sede.

    Devuelve un dict con el resultado. Idempotente por marcaje_id.
    Lanza AsistenciaError ante datos invalidos o fallo de Odoo.
    """
    marcaje_id = registro.get("marcaje_id")
    if not marcaje_id:
        raise AsistenciaError("Falta 'marcaje_id' (identificador unico del marcaje).")
    marcaje_id = str(marcaje_id)

    # Validacion minima de la entrada ANTES de reservar (un marcaje sin check_in
    # no es procesable y no debe ocupar una reserva).
    check_in = _validar_fecha(registro.get("check_in"), "check_in")
    tiene_salida = bool(registro.get("check_out"))
    if tiene_salida:
        _validar_fecha(registro.get("check_out"), "check_out")

    hash_actual = state_store.calcular_hash(registro)

    # 1. Idempotencia ATOMICA. A diferencia de factura/pago, aqui un segundo
    # mensaje con el mismo marcaje_id puede ser LEGITIMO (aporta el check_out
    # que faltaba), asi que no basta con "ya procesado -> no hacer nada":
    #   - mismo payload exacto           -> reenvio idempotente, no se toca Odoo.
    #   - trae check_out sobre un fichaje
    #     abierto sin salida             -> se COMPLETA la salida (write).
    previo = state_store.reservar_estricto(
        ENTIDAD, marcaje_id, model_odoo=MODEL, hash_payload=hash_actual,
    )
    if previo is not None:
        # Reenvio exacto: mismo payload -> nada que hacer.
        if previo.hash_payload == hash_actual:
            state_store.log(ENTIDAD, "idempotente", "OK", marcaje_id, "Fichaje ya registrado")
            return {
                "marcaje_id": marcaje_id,
                "id_odoo": previo.id_odoo,
                "estado": previo.estado,
                "accion": "sin_cambios",
                "idempotente": True,
            }
        # Mismo marcaje, payload distinto: el unico cambio admitido es aportar
        # el check_out que faltaba. Cualquier otra diferencia se rechaza (el
        # fichaje de entrada ya quedo registrado; no se reescribe su contenido).
        if tiene_salida and previo.id_odoo:
            return _completar_salida(registro, marcaje_id, previo, odoo)
        state_store.log(ENTIDAD, "idempotente", "OK", marcaje_id, "Fichaje ya registrado (payload distinto)")
        return {
            "marcaje_id": marcaje_id,
            "id_odoo": previo.id_odoo,
            "estado": previo.estado,
            "accion": "sin_cambios",
            "idempotente": True,
        }

    # 2 y 3. A partir de aqui se habla con Odoo. Un corte de conexion debe
    # LIBERAR la reserva: en PROCESANDO, reservar_estricto la rechazaria para
    # siempre, bloqueando el marcaje.
    with state_store.reserva_liberada_si_cae_odoo(ENTIDAD, marcaje_id):
        try:
            employee_id = _resolver_empleado(odoo, registro.get("empleado_codigo"))
        except AsistenciaError as e:
            state_store.marcar_estado(ENTIDAD, marcaje_id, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "resolver", "ERROR", marcaje_id, str(e))
            raise
        except OdooExecutionError as e:
            state_store.marcar_estado(ENTIDAD, marcaje_id, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "resolver", "ERROR", marcaje_id, str(e))
            raise AsistenciaError(f"Error de Odoo al resolver el empleado: {e}") from e

        valores = {"employee_id": employee_id, "check_in": check_in}
        if tiene_salida:
            valores["check_out"] = _validar_fecha(registro.get("check_out"), "check_out")

        # El identificador de dispositivo/sede va en un campo personalizado que
        # puede no existir en la instancia. Se incluye solo si viene; si Odoo lo
        # rechaza por campo inexistente, se reintenta sin el (la asistencia se
        # registra igual, con un aviso en el log).
        biometrico_id = registro.get("biometrico_id")
        campo_bio = _campo_biometrico()
        if biometrico_id:
            valores[campo_bio] = str(biometrico_id)

        try:
            id_odoo = _crear_con_fallback_biometrico(odoo, valores, campo_bio)
            state_store.registrar_mapeo(
                ENTIDAD, marcaje_id, model_odoo=MODEL, id_odoo=id_odoo,
                estado=EstadoSync.PROCESADO, hash_payload=hash_actual,
            )
            state_store.log(
                ENTIDAD, "crear", "OK", marcaje_id,
                f"id_odoo={id_odoo} employee={employee_id} "
                f"check_in={check_in}{' check_out=' + valores['check_out'] if tiene_salida else ' (abierto)'}",
            )
        except OdooExecutionError as e:
            state_store.marcar_estado(ENTIDAD, marcaje_id, EstadoSync.ERROR, error=str(e))
            state_store.log(ENTIDAD, "crear", "ERROR", marcaje_id, str(e))
            raise AsistenciaError(f"Error al crear el fichaje en Odoo: {e}") from e

        logger.info(
            "ASISTENCIA_OK | marcaje=%s id_odoo=%s employee=%s abierto=%s",
            marcaje_id, id_odoo, employee_id, not tiene_salida,
        )
        return {
            "marcaje_id": marcaje_id,
            "id_odoo": id_odoo,
            "employee_id": employee_id,
            "estado": EstadoSync.PROCESADO.value,
            "accion": "fichaje_creado" if tiene_salida else "entrada_abierta",
            "abierto": not tiene_salida,
            "idempotente": False,
        }


def _crear_con_fallback_biometrico(
    odoo: OdooUniversalAPI, valores: dict, campo_bio: str
) -> int:
    """
    Crea el hr.attendance. Si el campo personalizado del biometrico no existe en
    la instancia, Odoo rechaza el create por campo invalido; en ese caso se
    reintenta SIN ese campo (la asistencia es lo esencial; la sede es un extra).

    Un fallo por cualquier otra causa se propaga tal cual.
    """
    if campo_bio not in valores:
        return odoo.execute(MODEL, "create", valores)
    try:
        return odoo.execute(MODEL, "create", valores)
    except OdooExecutionError as e:
        # El campo personalizado no esta en la instancia: se avisa y se reintenta
        # sin el. Se reconoce por el nombre del campo en el mensaje de Odoo
        # ("Invalid field 'x_biometrico_id'...").
        if campo_bio in str(e):
            logger.warning(
                "ASISTENCIA_SIN_BIOMETRICO | el campo '%s' no existe en Odoo; "
                "se registra el fichaje sin el identificador de sede.", campo_bio,
            )
            valores_sin = {k: v for k, v in valores.items() if k != campo_bio}
            return odoo.execute(MODEL, "create", valores_sin)
        raise
