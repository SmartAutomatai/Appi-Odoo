"""
Poller de asistencia Fibex (modo PULL): trae las marcas de Fibex a Odoo.

Consulta la API de Fibex (core/fibex_source.py) por un rango de dias, mapea cada
asistencia al formato de core/asistencia.py y la registra en Odoo (hr.attendance).
La idempotencia la resuelve el state store por `marcaje_id`: reconsultar el mismo
dia NO duplica nada en Odoo (una marca ya procesada se detecta y se omite).

Flujo de una pasada (procesar_asistencias):
  1. GET /asistencias?desde&hasta&solo_cerradas de Fibex.
  2. Por cada registro: mapear -> registrar_asistencia() -> Odoo.
  3. Resumen (creadas / idempotentes / error / omitidas).

Politica de errores por fila (aislamiento, como el poller del cliente):
  - Fallo de DATOS de una marca (AsistenciaError: cedula inexistente, fecha
    invalida, Odoo rechaza el fichaje) -> se aisla esa marca y se sigue.
  - Fallo de CONEXION con Odoo (OdooConnectionError) -> aborta la pasada y se
    propaga, para que Celery la reintente entera (lo ya hecho es idempotente).
  - Fallo de la API de FIBEX (FibexSourceError: red, 403 de lista blanca de IP,
    rango invalido) -> aborta la pasada y se propaga: sin datos no hay nada que
    procesar; se reintenta mas tarde.

El PDF de Fibex recomienda "una vez al dia consultar el dia anterior". Por eso la
unidad natural es un dia, pero procesar_asistencias acepta un rango (maximo 31
dias, limite de Fibex) para reprocesos puntuales.

Las fechas de Fibex (check_in/check_out) se pasan a Odoo TAL CUAL (ISO 8601 con
zona); el mapeo no reinterpreta la hora. El empleado se resuelve por cedula
(ASISTENCIA_CAMPO_EMPLEADO=identification_id); Fibex la entrega SIN prefijo.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from core import fibex_source, state_store
from core.asistencia import AsistenciaError, registrar_asistencia
from odoo_universal import OdooConnectionError, OdooUniversalAPI, get_tenant

logger = logging.getLogger("api-odoo")

ENTIDAD = "asistencia"


def mapear_asistencia(fibex: dict) -> dict:
    """
    Traduce un registro de Fibex (schema AsistenciaConsulta) al formato que
    espera core.asistencia.registrar_asistencia().

    Campos: marcaje_id, empleado_codigo (cedula), check_in, check_out (opcional),
    biometrico_id (usa 'biometrico_id' y, si falta, 'sede'; ambos pueden ser
    null en el schema, en cuyo caso no se envia campo de sede a Odoo).

    Sobre el campo 'estado' de Fibex (enum en_curso | cerrada | sin_salida): con
    FIBEX_SOLO_CERRADAS=true (por defecto) Fibex solo entrega 'cerrada' (entrada
    + salida), que es el caso ideal para Odoo. Si se consultara con solo_cerradas
    =false, llegarian tambien 'en_curso' (abierto, normal) y 'sin_salida' (la
    persona entro y nunca ficho salida): ambos traen check_out=null y se
    registrarian como fichaje ABIERTO. Un 'sin_salida' quedaria abierto en Odoo
    indefinidamente, asi que se recomienda mantener solo_cerradas=true salvo que
    se quiera reflejar la jornada en curso. No filtramos por 'estado' aqui: lo
    gobierna el parametro de la consulta a Fibex.
    """
    return {
        "marcaje_id": fibex.get("marcaje_id"),
        "empleado_codigo": fibex.get("empleado_codigo"),
        "check_in": fibex.get("check_in"),
        "check_out": fibex.get("check_out"),
        "biometrico_id": fibex.get("biometrico_id") or fibex.get("sede"),
    }


@dataclass
class ResultadoAsistencias:
    """Resumen de una pasada del poller de asistencia Fibex."""
    leidas: int        # asistencias traidas de Fibex
    creadas: int       # fichajes nuevos o completados (salida) en Odoo
    idempotentes: int  # ya estaban (reenvio, no se toco Odoo)
    con_error: int     # marcas con fallo de datos (aisladas)


def _procesar_una(fibex: dict, odoo: OdooUniversalAPI) -> str:
    """
    Procesa una asistencia de Fibex. Devuelve:
      "creada"       si se creo/completo el fichaje,
      "idempotente"  si ya estaba (no se toco Odoo),
      "error"        si fallo por datos (se aisla y se sigue).

    Re-lanza OdooConnectionError para que la pasada se aborte y se reintente.
    """
    registro = mapear_asistencia(fibex)
    marcaje_id = registro.get("marcaje_id")
    try:
        r = registrar_asistencia(registro, odoo)
        if r.get("idempotente"):
            return "idempotente"
        return "creada"
    except OdooConnectionError:
        logger.warning(
            "Fibex-poller: Odoo no disponible al procesar marcaje %s; "
            "se aborta la pasada para reintentar.", marcaje_id,
        )
        raise
    except AsistenciaError as e:
        # Fallo de datos: cedula que no existe, fecha invalida, Odoo rechaza el
        # fichaje (solapamiento, check_out < check_in). Se aisla esta marca y se
        # sigue con el resto. Queda en ERROR en el state store (auditable en el
        # panel); el estado de la marca ya quedo registrado por el propio modulo.
        state_store.log(ENTIDAD, "fibex", "ERROR", marcaje_id, str(e))
        logger.info("Fibex-poller: marcaje %s en ERROR: %s", marcaje_id, e)
        return "error"


def procesar_asistencias(
    desde: str, hasta: str, tenant: str = "default"
) -> ResultadoAsistencias:
    """
    Trae las asistencias de Fibex del rango [desde, hasta] (AAAA-MM-DD) y las
    registra en Odoo. Devuelve un ResultadoAsistencias con el resumen.

    Si el modo pull de asistencia no esta configurado (sin FIBEX_API_URL), no
    hace nada y devuelve un resumen vacio.
    """
    if not fibex_source.polling_habilitado():
        logger.debug("Fibex-poller: FIBEX_API_URL no configurada; nada que hacer.")
        return ResultadoAsistencias(0, 0, 0, 0)

    odoo = get_tenant(tenant)
    asistencias = fibex_source.consultar_asistencias(desde, hasta)

    creadas = idempotentes = con_error = 0
    for fibex in asistencias:
        estado = _procesar_una(fibex, odoo)
        if estado == "creada":
            creadas += 1
        elif estado == "idempotente":
            idempotentes += 1
        else:
            con_error += 1

    if asistencias:
        logger.info(
            "Fibex-poller: %s asistencia(s) %s..%s -> %s creadas, %s idempotentes, %s error.",
            len(asistencias), desde, hasta, creadas, idempotentes, con_error,
        )
    return ResultadoAsistencias(
        leidas=len(asistencias), creadas=creadas,
        idempotentes=idempotentes, con_error=con_error,
    )


def procesar_dia_anterior(tenant: str = "default") -> ResultadoAsistencias:
    """
    Caso de uso recomendado por el PDF de Fibex: "una vez al dia consultar el dia
    anterior". Consulta el dia de ayer (UTC) completo y lo registra en Odoo.
    """
    ayer = (date.today() - timedelta(days=1)).isoformat()
    return procesar_asistencias(ayer, ayer, tenant=tenant)
