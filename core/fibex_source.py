"""
Cliente de la API de asistencia biometrica de Fibex (fuente, modo PULL).

En este despliegue el middleware NO recibe las marcas: las VA A BUSCAR. Fibex
acumula las asistencias de sus biometricos (una por persona y dia) y las expone
en su `GET /asistencias`; este modulo las consulta y las entrega al poller, que
las escribe en Odoo (hr.attendance).

    Biometrico -> API Fibex (acumula) -> [GET /asistencias] -> middleware -> Odoo

Config perezosa, como core/poller_source.py: sin FIBEX_API_URL el modo pull de
asistencia esta APAGADO (polling_habilitado() -> False) y no se programa nada.
Variables de entorno:
  FIBEX_API_URL      base, p.ej. https://api-biometrico.fibextelecom.net
  FIBEX_API_KEY      clave entregada por Fibex (cabecera X-API-Key)
  FIBEX_TIMEOUT_SEG  timeout de cada peticion (por defecto 30)
  FIBEX_SOLO_CERRADAS si "true" (por defecto), pide solo las asistencias con
                      entrada Y salida (recomendado por Fibex para Odoo).

Contrato de Fibex (schema AsistenciaConsulta), campos que usamos:
  marcaje_id, empleado_codigo (CEDULA, sin prefijo), check_in, check_out,
  biometrico_id (o sede). Ver docs/INFORME-ASISTENCIA-BIOMETRICA.md y el PDF
  "API de asistencia biometrica - Fibex".

Lanza FibexSourceError ante un fallo de la API de Fibex (red, 4xx/5xx, JSON
invalido). El 403 por lista blanca de IP tambien llega como FibexSourceError con
el texto de Fibex ("ip_no_autorizada"), para que se vea claro en el log.
"""

import logging
import os

import requests

logger = logging.getLogger("api-odoo")


class FibexSourceError(Exception):
    """Fallo al consultar la API de asistencia de Fibex."""
    pass


# Una sola sesion reutilizada (pool de conexiones + keep-alive), como el
# conector de Odoo. Evita rehacer el handshake TLS en cada consulta diaria.
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def polling_habilitado() -> bool:
    """True si hay FIBEX_API_URL configurada (modo pull de asistencia activo)."""
    return bool(os.getenv("FIBEX_API_URL", "").strip())


def _base_url() -> str:
    url = os.getenv("FIBEX_API_URL", "").strip().rstrip("/")
    if not url:
        raise FibexSourceError("FIBEX_API_URL no configurada (modo pull apagado).")
    return url


def _headers() -> dict:
    # La clave se lee en cada llamada (como API_KEY del middleware): rotarla no
    # exige reiniciar. Si falta, Fibex respondera 401 y se vera en el error.
    clave = os.getenv("FIBEX_API_KEY", "").strip()
    cabeceras = {"Accept": "application/json"}
    if clave:
        cabeceras["X-API-Key"] = clave
    return cabeceras


def _timeout() -> float:
    return float(os.getenv("FIBEX_TIMEOUT_SEG", "30"))


def _solo_cerradas() -> bool:
    return os.getenv("FIBEX_SOLO_CERRADAS", "true").strip().lower() not in (
        "false", "0", "no",
    )


def salud() -> dict:
    """
    Consulta GET /salud de Fibex (no pide clave). Util para diagnostico.
    Devuelve el JSON de Fibex; lanza FibexSourceError si no responde.
    """
    try:
        resp = _get_session().get(f"{_base_url()}/salud", timeout=_timeout())
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise FibexSourceError(f"Error al consultar /salud de Fibex: {e}") from e


def consultar_asistencias(
    desde: str, hasta: str, cedula: str | None = None
) -> list[dict]:
    """
    Consulta GET /asistencias?desde&hasta[&cedula]&solo_cerradas de Fibex.

    `desde`/`hasta` en formato AAAA-MM-DD (maximo 31 dias por consulta, segun
    Fibex). Devuelve la lista `asistencias` del JSON de respuesta (cada elemento
    es un AsistenciaConsulta). Lista vacia si no hay nada en el rango.

    Lanza FibexSourceError ante cualquier fallo (red, 4xx/5xx, JSON invalido).
    El 403 de lista blanca de IP llega aqui con el texto de Fibex.
    """
    params = {
        "desde": desde,
        "hasta": hasta,
        "solo_cerradas": str(_solo_cerradas()).lower(),
    }
    if cedula:
        params["cedula"] = cedula

    try:
        resp = _get_session().get(
            f"{_base_url()}/asistencias",
            params=params, headers=_headers(), timeout=_timeout(),
        )
    except requests.RequestException as e:
        raise FibexSourceError(f"Error de red al consultar Fibex: {e}") from e

    # Fibex devuelve el error como JSON {"error": ..., "detalle": ...}. Se
    # extrae para que el log diga exactamente la causa (p.ej. ip_no_autorizada,
    # rango invalido) en vez de un "HTTP 403" desnudo.
    if resp.status_code != 200:
        detalle = _detalle_error(resp)
        raise FibexSourceError(
            f"Fibex respondio HTTP {resp.status_code} al consultar asistencias "
            f"({desde}..{hasta}): {detalle}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise FibexSourceError(f"Fibex devolvio un JSON invalido: {e}") from e

    asistencias = data.get("asistencias", [])
    if not isinstance(asistencias, list):
        raise FibexSourceError(
            "La respuesta de Fibex no trae una lista 'asistencias'."
        )
    logger.info(
        "FIBEX_CONSULTA | %s..%s -> total=%s omitidas=%s",
        desde, hasta, data.get("total"), data.get("omitidas"),
    )
    return asistencias


def _detalle_error(resp: requests.Response) -> str:
    """Extrae {error, detalle} del cuerpo de Fibex, o el texto crudo si no es JSON."""
    try:
        cuerpo = resp.json()
        error = cuerpo.get("error", "")
        detalle = cuerpo.get("detalle", "")
        return f"{error}: {detalle}".strip(": ") or resp.text[:200]
    except ValueError:
        return resp.text[:200]
