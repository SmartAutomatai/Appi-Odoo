"""
Tests de asistencia biometrica (crear/completar hr.attendance).

Aislan el state store con SQLite temporal y mockean Odoo, verificando:
  - resolucion del empleado por 'barcode',
  - fichaje completo (check_in + check_out),
  - fichaje abierto (solo check_in) y su cierre posterior (mismo marcaje_id),
  - idempotencia (reenvio exacto no toca Odoo),
  - validacion de fechas,
  - fallback cuando el campo personalizado del biometrico no existe.
sin conexion real.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store con SQLite temporal + asistencia recargada contra el."""
    db_file = tmp_path / "control_asistencia.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    # Valores por defecto deterministas (no depender del .env del repo).
    monkeypatch.setenv("ASISTENCIA_CAMPO_EMPLEADO", "barcode")
    monkeypatch.setenv("ASISTENCIA_CAMPO_BIOMETRICO", "x_biometrico_id")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.asistencia as asistencia
    importlib.reload(asistencia)
    return asistencia, state_store


def _odoo(empleado_existe=True, biometrico_invalido=False, creado_id=555):
    """
    Mock de Odoo:
      hr.employee search_read -> id 42 (por barcode) o [] si no existe
      hr.attendance create    -> creado_id ; si biometrico_invalido, falla la
                                  primera vez con el campo x_biometrico_id dentro
                                  del mensaje y funciona al reintentar sin el
      hr.attendance write     -> True
    """
    from odoo_universal import OdooExecutionError

    odoo = MagicMock()
    estado = {"create_calls": 0}

    def execute(model, method, *args, **kwargs):
        if model == "hr.employee" and method == "search_read":
            return [{"id": 42}] if empleado_existe else []
        if model == "hr.attendance" and method == "create":
            estado["create_calls"] += 1
            valores = args[0]
            estado["ultimo_create"] = valores
            if biometrico_invalido and "x_biometrico_id" in valores:
                raise OdooExecutionError("Invalid field 'x_biometrico_id' on model 'hr.attendance'")
            return creado_id
        if model == "hr.attendance" and method == "write":
            return True
        if model == "hr.attendance" and method == "search_count":
            return estado.get("attendance_existe", 1)
        return True

    odoo.execute.side_effect = execute
    odoo._estado = estado
    return odoo


def _registro(**over):
    base = {
        "marcaje_id": "BIO-0001",
        "biometrico_id": "SEDE-CARACAS-01",
        "empleado_codigo": "EMP0042",
        "check_in": "2026-10-05T13:02:00Z",
        "check_out": "2026-10-05T21:10:00Z",
    }
    base.update(over)
    return base


def test_fichaje_completo(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    r = asistencia.registrar_asistencia(_registro(), odoo)
    assert r["idempotente"] is False
    assert r["id_odoo"] == 555
    assert r["employee_id"] == 42
    assert r["accion"] == "fichaje_creado"
    assert r["abierto"] is False


def test_fecha_se_normaliza_a_utc_naive(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    asistencia.registrar_asistencia(_registro(), odoo)
    vals = odoo._estado["ultimo_create"]
    # check_in/out venian en Z (UTC): mismo instante, formato Odoo (sin T ni Z).
    assert vals["check_in"] == "2026-10-05 13:02:00"
    assert vals["check_out"] == "2026-10-05 21:10:00"


def test_fecha_con_zona_venezuela_se_convierte_a_utc(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    # 09:02 en Venezuela (-04:00) = 13:02 UTC.
    asistencia.registrar_asistencia(
        _registro(check_in="2026-10-05T09:02:00-04:00", check_out=None), odoo
    )
    vals = odoo._estado["ultimo_create"]
    assert vals["check_in"] == "2026-10-05 13:02:00"


def test_fecha_sin_zona_se_rechaza(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    with pytest.raises(asistencia.AsistenciaError, match="zona horaria"):
        asistencia.registrar_asistencia(_registro(check_in="2026-10-05T13:02:00"), odoo)


def test_fichaje_abierto_y_cierre_posterior(entorno):
    asistencia, state_store = entorno
    odoo = _odoo()

    # Primer mensaje: solo entrada.
    r1 = asistencia.registrar_asistencia(
        _registro(check_out=None), odoo
    )
    assert r1["abierto"] is True
    assert r1["accion"] == "entrada_abierta"

    # Segundo mensaje: mismo marcaje_id con check_out -> completa la salida.
    r2 = asistencia.registrar_asistencia(_registro(), odoo)
    assert r2["accion"] == "salida_completada"
    assert r2["idempotente"] is False
    assert r2["id_odoo"] == r1["id_odoo"]

    # Debe haberse llamado a write con el check_out.
    llamadas_write = [
        c for c in odoo.execute.call_args_list
        if c.args[:2] == ("hr.attendance", "write")
    ]
    assert len(llamadas_write) == 1


def test_cierre_de_fichaje_borrado_en_odoo(entorno):
    asistencia, _ = entorno
    odoo = _odoo()

    # Entrada abierta.
    asistencia.registrar_asistencia(_registro(check_out=None), odoo)

    # Alguien borro el fichaje en Odoo: search_count -> 0.
    odoo._estado["attendance_existe"] = 0

    # Llega la salida -> debe rechazarse con mensaje reconocible, sin write.
    with pytest.raises(asistencia.AsistenciaError, match="ya no existe en Odoo"):
        asistencia.registrar_asistencia(_registro(), odoo)
    llamadas_write = [
        c for c in odoo.execute.call_args_list
        if c.args[:2] == ("hr.attendance", "write")
    ]
    assert len(llamadas_write) == 0


def test_reenvio_exacto_es_idempotente(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    asistencia.registrar_asistencia(_registro(), odoo)
    creates_antes = odoo._estado["create_calls"]

    r = asistencia.registrar_asistencia(_registro(), odoo)
    assert r["idempotente"] is True
    assert r["accion"] == "sin_cambios"
    # No debe haber creado un segundo fichaje.
    assert odoo._estado["create_calls"] == creates_antes


def test_empleado_inexistente_rechaza(entorno):
    asistencia, _ = entorno
    odoo = _odoo(empleado_existe=False)
    with pytest.raises(asistencia.AsistenciaError, match="empleado"):
        asistencia.registrar_asistencia(_registro(), odoo)


def test_falta_check_in(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    with pytest.raises(asistencia.AsistenciaError, match="check_in"):
        asistencia.registrar_asistencia(_registro(check_in=None), odoo)


def test_falta_marcaje_id(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    with pytest.raises(asistencia.AsistenciaError, match="marcaje_id"):
        asistencia.registrar_asistencia(_registro(marcaje_id=None), odoo)


def test_fecha_invalida(entorno):
    asistencia, _ = entorno
    odoo = _odoo()
    with pytest.raises(asistencia.AsistenciaError, match="ISO 8601"):
        asistencia.registrar_asistencia(_registro(check_in="no-es-fecha"), odoo)


def test_fallback_campo_biometrico_inexistente(entorno):
    asistencia, _ = entorno
    odoo = _odoo(biometrico_invalido=True)
    # No debe fallar: reintenta el create sin el campo personalizado.
    r = asistencia.registrar_asistencia(_registro(), odoo)
    assert r["id_odoo"] == 555
    # Dos intentos de create: el primero con el campo (falla), el segundo sin el.
    assert odoo._estado["create_calls"] == 2
