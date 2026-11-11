"""
Tests del poller de asistencia Fibex (modo pull).

Dos niveles:
  - fibex_source: cliente HTTP, mockeando requests.Session.get (simula la API de
    Fibex: respuesta 200 con 'asistencias', 403 ip_no_autorizada, JSON invalido).
  - fibex_poller: orquestador, mockeando fibex_source + el conector Odoo, con el
    REGISTRO REAL de ejemplo de Fibex, verificando mapeo, idempotencia y
    aislamiento de errores por fila.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest


# Registro real de ejemplo de la API de Fibex (schema AsistenciaConsulta).
FIBEX_EJEMPLO = {
    "marcaje_id": "J-2026-11-09-12345678",
    "empleado_codigo": "12345678",
    "nombre": "Ana Perez",
    "departamento": "Ventas",
    "fecha": "2026-11-09",
    "biometrico_id": "TORRE FIBEX 1",
    "sede": "TORRE FIBEX 1",
    "check_in": "2026-11-09T12:05:00Z",
    "check_out": "2026-11-09T21:02:00Z",
    "biometrico_salida": "TORRE FIBEX 1",
    "estado": "cerrada",
    "marcajes": 2,
    "observaciones": [],
}


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store temporal + modulos de asistencia/fibex recargados."""
    db_file = tmp_path / "control_fibex.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("ASISTENCIA_CAMPO_EMPLEADO", "identification_id")
    monkeypatch.setenv("ASISTENCIA_CAMPO_BIOMETRICO", "x_biometrico_id")
    monkeypatch.setenv("FIBEX_API_URL", "https://api-biometrico.ejemplo.net")
    monkeypatch.setenv("FIBEX_API_KEY", "clave-prueba")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()
    import core.asistencia as asistencia
    importlib.reload(asistencia)
    import core.fibex_source as fibex_source
    importlib.reload(fibex_source)
    import core.fibex_poller as fibex_poller
    importlib.reload(fibex_poller)
    return fibex_poller, fibex_source, state_store


def _respuesta(status=200, json_data=None, text=""):
    """MagicMock que imita una requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


def _odoo_ok(employee_id=87, attendance_id=1532):
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "hr.employee" and method == "search_read":
            return [{"id": employee_id}]
        if model == "hr.attendance" and method == "create":
            return attendance_id
        return True

    odoo.execute.side_effect = execute
    return odoo


# --- fibex_source ---------------------------------------------------------

def test_source_consulta_ok(entorno):
    _, fibex_source, _ = entorno
    data = {"desde": "2026-11-09", "hasta": "2026-11-09", "total": 1,
            "omitidas": 0, "asistencias": [FIBEX_EJEMPLO]}
    with patch("requests.Session.get", return_value=_respuesta(200, data)) as g:
        asis = fibex_source.consultar_asistencias("2026-11-09", "2026-11-09")
    assert asis == [FIBEX_EJEMPLO]
    # Debe pedir solo_cerradas y pasar la cabecera X-API-Key.
    _, kwargs = g.call_args
    assert kwargs["params"]["solo_cerradas"] == "true"
    assert kwargs["headers"]["X-API-Key"] == "clave-prueba"


def test_source_403_ip_no_autorizada(entorno):
    _, fibex_source, _ = entorno
    cuerpo = {"error": "ip_no_autorizada", "detalle": "La IP X no esta autorizada."}
    with patch("requests.Session.get", return_value=_respuesta(403, cuerpo)):
        with pytest.raises(fibex_source.FibexSourceError, match="ip_no_autorizada"):
            fibex_source.consultar_asistencias("2026-11-09", "2026-11-09")


def test_source_json_invalido(entorno):
    _, fibex_source, _ = entorno
    with patch("requests.Session.get", return_value=_respuesta(200, None, text="<html>")):
        with pytest.raises(fibex_source.FibexSourceError, match="JSON invalido"):
            fibex_source.consultar_asistencias("2026-11-09", "2026-11-09")


# --- fibex_poller ---------------------------------------------------------

def test_poller_mapea_y_registra(entorno):
    fibex_poller, fibex_source, _ = entorno
    odoo = _odoo_ok()

    with patch.object(fibex_source, "consultar_asistencias", return_value=[FIBEX_EJEMPLO]), \
         patch.object(fibex_poller, "get_tenant", return_value=odoo):
        r = fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")

    assert r.leidas == 1
    assert r.creadas == 1
    assert r.idempotentes == 0
    assert r.con_error == 0

    # Verifica el mapeo: busca por identification_id sin prefijo, crea con la Z.
    llamadas = odoo.execute.call_args_list
    busqueda = [c for c in llamadas if c.args[:2] == ("hr.employee", "search_read")][0]
    assert busqueda.args[2] == [["identification_id", "=", "12345678"]]
    create = [c for c in llamadas if c.args[:2] == ("hr.attendance", "create")][0]
    vals = create.args[2]
    assert vals["employee_id"] == 87
    # Odoo no acepta 'T' ni 'Z' por RPC: la fecha se normaliza a UTC naive.
    # Estas horas ya venian en Z (UTC), asi que solo cambia el formato.
    assert vals["check_in"] == "2026-11-09 12:05:00"
    assert vals["check_out"] == "2026-11-09 21:02:00"
    assert vals["x_biometrico_id"] == "TORRE FIBEX 1"


def test_poller_sin_biometrico_ni_sede(entorno):
    # En el schema biometrico_id y sede pueden ser null: no debe romper, y el
    # fichaje se crea sin campo de sede.
    fibex_poller, fibex_source, _ = entorno
    odoo = _odoo_ok()
    reg = {**FIBEX_EJEMPLO, "marcaje_id": "J-NULL", "biometrico_id": None, "sede": None}
    with patch.object(fibex_source, "consultar_asistencias", return_value=[reg]), \
         patch.object(fibex_poller, "get_tenant", return_value=odoo):
        r = fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")
    assert r.creadas == 1
    create = [c for c in odoo.execute.call_args_list if c.args[:2] == ("hr.attendance", "create")][0]
    assert "x_biometrico_id" not in create.args[2]


def test_poller_idempotente_en_segunda_pasada(entorno):
    fibex_poller, fibex_source, _ = entorno
    odoo = _odoo_ok()

    with patch.object(fibex_source, "consultar_asistencias", return_value=[FIBEX_EJEMPLO]), \
         patch.object(fibex_poller, "get_tenant", return_value=odoo):
        r1 = fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")
        r2 = fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")

    assert r1.creadas == 1
    assert r2.creadas == 0
    assert r2.idempotentes == 1


def test_poller_aisla_error_de_datos(entorno):
    fibex_poller, fibex_source, _ = entorno
    # Empleado no existe -> AsistenciaError -> fila aislada, el resto sigue.
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "hr.employee" and method == "search_read":
            ced = args[0][0][2]
            return [{"id": 87}] if ced == "12345678" else []
        if model == "hr.attendance" and method == "create":
            return 1532
        return True
    odoo.execute.side_effect = execute

    bueno = FIBEX_EJEMPLO
    malo = {**FIBEX_EJEMPLO, "marcaje_id": "J-X-999", "empleado_codigo": "99999999"}

    with patch.object(fibex_source, "consultar_asistencias", return_value=[bueno, malo]), \
         patch.object(fibex_poller, "get_tenant", return_value=odoo):
        r = fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")

    assert r.leidas == 2
    assert r.creadas == 1
    assert r.con_error == 1


def test_poller_conexion_odoo_aborta(entorno):
    fibex_poller, fibex_source, _ = entorno
    from odoo_universal import OdooConnectionError

    odoo = MagicMock()
    odoo.execute.side_effect = OdooConnectionError("Odoo caido")

    with patch.object(fibex_source, "consultar_asistencias", return_value=[FIBEX_EJEMPLO]), \
         patch.object(fibex_poller, "get_tenant", return_value=odoo):
        with pytest.raises(OdooConnectionError):
            fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")


def test_poller_apagado_sin_url(entorno, monkeypatch):
    fibex_poller, fibex_source, _ = entorno
    monkeypatch.delenv("FIBEX_API_URL", raising=False)
    # Sin FIBEX_API_URL no hace nada (no toca Fibex ni Odoo).
    r = fibex_poller.procesar_asistencias("2026-11-09", "2026-11-09")
    assert r.leidas == 0 and r.creadas == 0
