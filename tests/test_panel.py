"""
Tests del panel de observabilidad.

  - unidad: observabilidad.py sobre una DB de control temporal (SQLite), poblada
    con state_store, verificando resumen y listados con filtros.
  - HTTP: el router /panel (HTML sin auth) y /panel/api/* (JSON con API Key),
    parcheando _login antes de importar api.py como en test_routers.py.
"""

import importlib
import os
from unittest.mock import patch

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """State store aislado en una DB SQLite temporal, con datos de ejemplo."""
    db = tmp_path / "control_panel.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")

    import core.state_store as state_store
    importlib.reload(state_store)
    import core.observabilidad as observabilidad
    importlib.reload(observabilidad)

    from core.models_db import EstadoSync
    state_store.init_db()

    # Dos facturas (una OK, una ERROR) y un pago OK.
    state_store.registrar_mapeo("factura", "F-1", model_odoo="account.move",
                                id_odoo=10, estado=EstadoSync.PROCESADO)
    state_store.log("factura", "crear", "OK", "F-1", "id_odoo=10")
    state_store.registrar_mapeo("factura", "F-2", model_odoo="account.move",
                                estado=EstadoSync.ERROR)
    state_store.marcar_estado("factura", "F-2", EstadoSync.ERROR, error="cliente no existe")
    state_store.log("factura", "mapear", "ERROR", "F-2", "cliente no existe")
    state_store.registrar_mapeo("pago", "P-1", model_odoo="account.payment",
                                id_odoo=20, estado=EstadoSync.PROCESADO)
    state_store.log("pago", "crear", "OK", "P-1", "id_odoo=20")

    return observabilidad


class TestResumen:
    def test_totales_por_estado(self, store):
        r = store.resumen()
        assert r["total"] == 3
        assert r["por_estado"]["PROCESADO"] == 2
        assert r["por_estado"]["ERROR"] == 1
        assert r["por_entidad"] == {"factura": 2, "pago": 1}
        assert r["logs"]["total"] == 3
        assert r["logs"]["errores"] == 1


class TestListados:
    def test_lista_todo(self, store):
        d = store.listar_sincronizaciones()
        assert d["total"] == 3
        assert len(d["items"]) == 3

    def test_filtro_por_estado_error(self, store):
        d = store.listar_sincronizaciones(estado="ERROR")
        assert d["total"] == 1
        assert d["items"][0]["id_origen"] == "F-2"
        assert d["items"][0]["error"] == "cliente no existe"

    def test_filtro_por_entidad(self, store):
        d = store.listar_sincronizaciones(entidad="pago")
        assert d["total"] == 1
        assert d["items"][0]["id_odoo"] == 20

    def test_filtro_id_origen_parcial(self, store):
        d = store.listar_sincronizaciones(id_origen="F-")
        assert d["total"] == 2

    def test_logs_filtra_por_resultado(self, store):
        d = store.listar_logs(resultado="ERROR")
        assert d["total"] == 1
        assert d["items"][0]["accion"] == "mapear"

    def test_detalle_incluye_mapeo_y_logs(self, store):
        d = store.detalle_registro("factura", "F-1")
        assert d["mapeo"]["estado"] == "PROCESADO"
        assert len(d["logs"]) == 1
        assert d["logs"][0]["accion"] == "crear"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api  # noqa: F401,E402
    from api import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


class TestRouterPanel:
    def test_panel_html_sin_auth(self, monkeypatch):
        monkeypatch.delenv("API_KEY", raising=False)
        r = client.get("/panel")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Panel de sincronizacion" in r.text
        assert "Poller ahora" in r.text

    def test_esc_escapa_comillas_para_evitar_xss_en_atributos(self, monkeypatch):
        # esc() se usa dentro de atributos (class="badge ${esc(x)}"); si no
        # escapa comillas, un dato con '"' podria romper el atributo e inyectar
        # codigo. El HTML servido debe traer la version endurecida de esc().
        monkeypatch.delenv("API_KEY", raising=False)
        html = client.get("/panel").text
        assert "&quot;" in html and "&#39;" in html, (
            "esc() debe escapar comillas dobles y simples (defensa XSS)"
        )

    def test_api_resumen_sin_key_devuelve_401(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "secreta")
        r = client.get("/panel/api/resumen")
        assert r.status_code == 401

    def test_api_resumen_con_key_ok(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "secreta")
        r = client.get("/panel/api/resumen", headers={"X-Api-Key": "secreta"})
        assert r.status_code == 200
        assert "por_estado" in r.json()

    def test_api_sincronizaciones_sin_apikey_configurada(self, monkeypatch):
        # Sin API_KEY en el entorno, el acceso pasa (modo desarrollo).
        monkeypatch.delenv("API_KEY", raising=False)
        r = client.get("/panel/api/sincronizaciones")
        assert r.status_code == 200
        assert "items" in r.json()


class TestPaginacion:
    """
    Sin paginar, el panel solo mostraba los primeros registros y el resto era
    inalcanzable: tras una tanda de pruebas de 338 registros no habia forma de
    ver los mas antiguos desde la interfaz.
    """

    def _muchos(self, store):
        """Crea 25 mapeos para poder recorrer varias paginas."""
        from core.models_db import EstadoSync
        import core.state_store as state_store

        for i in range(25):
            state_store.registrar_mapeo(
                "factura", f"PAG-{i:03d}", model_odoo="account.move",
                id_odoo=1000 + i, estado=EstadoSync.PROCESADO,
            )

    def test_el_total_no_depende_de_la_pagina(self, store):
        """`total` cuenta TODO lo que hay, no lo que cabe en la pagina."""
        self._muchos(store)
        pagina = store.listar_sincronizaciones(limite=10, offset=0)
        assert len(pagina["items"]) == 10
        assert pagina["total"] >= 25, "el total debe contar todos los registros"

    def test_las_paginas_no_se_solapan(self, store):
        """Dos paginas consecutivas deben traer registros distintos."""
        self._muchos(store)
        p1 = store.listar_sincronizaciones(limite=10, offset=0)
        p2 = store.listar_sincronizaciones(limite=10, offset=10)

        ids1 = {i["id_origen"] for i in p1["items"]}
        ids2 = {i["id_origen"] for i in p2["items"]}
        assert not (ids1 & ids2), "hay registros repetidos entre paginas"

    def test_recorrer_todas_las_paginas_cubre_el_total(self):
        """Ningun registro queda inalcanzable al paginar."""
        import core.observabilidad as observabilidad

        vistos, offset = set(), 0
        total = observabilidad.listar_sincronizaciones(limite=1)["total"]
        while offset < total:
            for item in observabilidad.listar_sincronizaciones(
                limite=10, offset=offset
            )["items"]:
                vistos.add(item["id_origen"])
            offset += 10
        assert len(vistos) == total, (
            f"se recorrieron {len(vistos)} de {total}: hay registros inalcanzables"
        )

    def test_un_offset_pasado_del_final_no_falla(self, store):
        """Pedir una pagina inexistente devuelve vacio, no un error."""
        r = store.listar_sincronizaciones(limite=10, offset=99999)
        assert r["items"] == []
        assert r["total"] >= 0

    def test_los_logs_tambien_paginan(self, store):
        r = store.listar_logs(limite=2, offset=0)
        assert len(r["items"]) <= 2
        assert "total" in r
