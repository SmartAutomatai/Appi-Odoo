"""
Tests del orquestador de sincronizacion contable (Fase 3).

Aislan el state store con un SQLite temporal y mockean tanto Odoo como el
modulo mapper, de modo que verifican el FLUJO (idempotencia, create, post,
manejo de errores) sin conexion real ni dependencia del mappings.yaml real.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """
    Prepara state_store con SQLite temporal, recarga sincronizador contra el,
    y mockea mapper.cargar_config / id_origen_de / mapear.
    Devuelve (sincronizador, mapper_mock, state_store).
    """
    db_file = tmp_path / "control_sync.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.sincronizador as sincronizador
    importlib.reload(sincronizador)  # toma el state_store recargado

    # Mockea el mapper para no depender del YAML real.
    mapper_mock = sincronizador.mapper
    monkeypatch.setattr(mapper_mock, "id_origen_de", lambda ent, reg: str(reg["fid"]))
    monkeypatch.setattr(
        mapper_mock, "cargar_config",
        lambda: {"factura": {"model": "account.move"}},
    )
    monkeypatch.setattr(
        mapper_mock, "mapear",
        lambda ent, reg, odoo: {"move_type": "out_invoice", "partner_id": 7},
    )
    return sincronizador, mapper_mock, state_store


@pytest.fixture()
def odoo_ok():
    """Odoo que crea con id=555 y postea sin error."""
    odoo = MagicMock()
    # create -> 555 ; action_post -> True
    odoo.execute.side_effect = lambda model, method, *a, **k: 555 if method == "create" else True
    return odoo


# --- flujo feliz ---


class TestFlujoExitoso:
    def test_crea_y_postea(self, entorno, odoo_ok):
        sincronizador, _, state_store = entorno
        res = sincronizador.sincronizar_entidad("factura", {"fid": "F-1"}, odoo_ok)

        assert res.id_odoo == 555
        assert res.idempotente is False
        assert res.estado == "PROCESADO"

        # Verifica que llamo create y action_post.
        metodos = [c.args[1] for c in odoo_ok.execute.call_args_list]
        assert "create" in metodos
        assert "action_post" in metodos

        # El state store quedo PROCESADO con el id_odoo.
        mapa = state_store.buscar_mapeo("factura", "F-1")
        assert mapa.estado == "PROCESADO"
        assert mapa.id_odoo == 555

    def test_postear_false_deja_en_borrador_sin_postear(self, entorno, odoo_ok):
        """
        Con postear:false la factura se crea pero NO se postea: queda en draft
        en Odoo. Se marca PROCESADO igual (la sincronizacion cumplio lo pedido).
        """
        sincronizador, _, state_store = entorno
        res = sincronizador.sincronizar_entidad(
            "factura", {"fid": "F-DRAFT", "postear": False}, odoo_ok,
        )

        assert res.estado == "PROCESADO"
        assert res.id_odoo == 555

        # Llamo create pero NO action_post.
        metodos = [c.args[1] for c in odoo_ok.execute.call_args_list]
        assert "create" in metodos
        assert "action_post" not in metodos

        mapa = state_store.buscar_mapeo("factura", "F-DRAFT")
        assert mapa.estado == "PROCESADO"

    def test_postear_por_defecto_sigue_posteando(self, entorno, odoo_ok):
        """Sin el campo postear, el comportamiento no cambia: se postea."""
        sincronizador, _, _ = entorno
        sincronizador.sincronizar_entidad("factura", {"fid": "F-DEF"}, odoo_ok)

        metodos = [c.args[1] for c in odoo_ok.execute.call_args_list]
        assert "action_post" in metodos


# --- idempotencia end-to-end ---


class TestIdempotencia:
    def test_segundo_envio_no_toca_odoo(self, entorno, odoo_ok):
        sincronizador, _, _ = entorno

        sincronizador.sincronizar_entidad("factura", {"fid": "F-2"}, odoo_ok)
        llamadas_primer_envio = odoo_ok.execute.call_count

        # Segundo envio del MISMO id_origen.
        res2 = sincronizador.sincronizar_entidad("factura", {"fid": "F-2"}, odoo_ok)

        assert res2.idempotente is True
        assert res2.id_odoo == 555
        # No hubo nuevas llamadas a Odoo.
        assert odoo_ok.execute.call_count == llamadas_primer_envio


# --- errores ---


class TestErrores:
    def test_error_de_mapeo_marca_error(self, entorno, odoo_ok, monkeypatch):
        sincronizador, mapper_mock, state_store = entorno

        def mapear_falla(ent, reg, odoo):
            raise mapper_mock.MapeoError("cliente no existe")

        monkeypatch.setattr(mapper_mock, "mapear", mapear_falla)

        with pytest.raises(sincronizador.SincronizacionError, match="mapeo"):
            sincronizador.sincronizar_entidad("factura", {"fid": "F-3"}, odoo_ok)

        mapa = state_store.buscar_mapeo("factura", "F-3")
        assert mapa.estado == "ERROR"
        assert "cliente no existe" in mapa.error

    def test_fallo_en_post_deja_error_con_id(self, entorno, monkeypatch):
        sincronizador, _, state_store = entorno
        from odoo_universal import OdooExecutionError

        odoo = MagicMock()

        def execute(model, method, *a, **k):
            if method == "create":
                return 777
            raise OdooExecutionError("no se puede postear")

        odoo.execute.side_effect = execute

        with pytest.raises(sincronizador.SincronizacionError, match="postear"):
            sincronizador.sincronizar_entidad("factura", {"fid": "F-4"}, odoo)

        mapa = state_store.buscar_mapeo("factura", "F-4")
        assert mapa.estado == "ERROR"
        assert mapa.id_odoo == 777          # se creo pero no se posteo
        assert "777" in mapa.error
