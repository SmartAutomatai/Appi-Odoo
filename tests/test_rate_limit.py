"""
Tests del limitador de tasa en los endpoints de negocio (auditoria H-3).

Antes solo /odoo tenia limite: /facturas, /pagos, /conciliar, /asientos,
/stock/* y /poller/ejecutar quedaban abiertos a un numero ilimitado de
peticiones. Eso permitia agotar el pool de conexiones a Odoo y, sobre todo,
hacer fuerza bruta comoda contra la API Key.

Estos tests comprueban que el limite EXISTE y DISPARA (429), no solo que el
decorador este puesto: un limite mal cableado deja pasar todo en silencio.
"""

import os
from unittest.mock import patch

import pytest

with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api  # noqa: F401  (monta los routers)
    from api import app

from fastapi.testclient import TestClient

import routers.facturas as r_facturas
from core.seguridad import limiter
from core.sincronizador import ResultadoSync

client = TestClient(app)


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    """Sin API key, y con el contador del limitador a cero en cada test."""
    monkeypatch.delenv("API_KEY", raising=False)
    # slowapi guarda los contadores en memoria entre peticiones: sin limpiarlos,
    # el consumo de un test agotaria la cuota del siguiente.
    limiter.reset()


def _factura(n):
    return {"registro": {"factura_id": f"RL-{n}", "cliente_nif": "B1",
                         "fecha": "2026-01-01", "total": 10.0, "lineas": []}}


class TestLimiteEnEndpointsDeNegocio:
    def test_dispara_429_al_superar_el_limite(self, monkeypatch):
        """Superado el limite, /facturas responde 429 y deja de tocar Odoo."""
        monkeypatch.setattr(
            r_facturas, "crear_factura",
            lambda registro, odoo, entidad="factura": ResultadoSync(
                id_origen="RL", id_odoo=1, estado="PROCESADO", idempotente=False),
        )
        # El limite por defecto es 120/minute; se piden 130 para rebasarlo.
        codigos = [client.post("/facturas", json=_factura(i)).status_code
                   for i in range(130)]
        assert 429 in codigos, "el limite no disparo: el endpoint sigue abierto"
        assert codigos[0] == 200, "las primeras peticiones deben pasar"
        # El 429 llega despues de un tramo util, no desde la primera.
        assert codigos.index(429) > 100

    def test_el_limite_no_estorba_al_uso_normal(self, monkeypatch):
        """20 peticiones seguidas (uso real medido) no deben verse afectadas."""
        monkeypatch.setattr(
            r_facturas, "crear_factura",
            lambda registro, odoo, entidad="factura": ResultadoSync(
                id_origen="RL", id_odoo=1, estado="PROCESADO", idempotente=False),
        )
        codigos = [client.post("/facturas", json=_factura(i)).status_code
                   for i in range(20)]
        assert codigos == [200] * 20


class TestCoberturaDelLimite:
    """Guardia contra regresiones: ningun endpoint de negocio sin limite."""

    ENDPOINTS = [
        ("POST", "/facturas"), ("POST", "/pagos"), ("POST", "/conciliar"),
        ("POST", "/asientos"), ("DELETE", "/asientos"),
        ("POST", "/stock/ajustar"), ("POST", "/stock/consultar"),
        ("POST", "/poller/ejecutar"),
        ("GET", "/panel/api/resumen"), ("GET", "/panel/api/sincronizaciones"),
        ("GET", "/panel/api/logs"),
    ]

    # Rutas que FastAPI monta solas y no son nuestras. Todo lo demas debe
    # llevar limite, incluido /health: es publico y sin autenticar, o sea el
    # blanco mas comodo para saturar el servicio.
    SIN_LIMITE_JUSTIFICADO = {
        "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    }

    def test_todos_los_endpoints_tienen_limite(self):
        rutas_con_limite = set()
        for ruta in app.routes:
            fn = getattr(ruta, "endpoint", None)
            # slowapi marca la funcion decorada con _rate_limit_exceeded.
            if fn is not None and hasattr(fn, "__wrapped__"):
                for metodo in getattr(ruta, "methods", []):
                    rutas_con_limite.add((metodo, ruta.path))

        faltan = [e for e in self.ENDPOINTS if tuple(e) not in rutas_con_limite]
        assert not faltan, f"endpoints sin rate limit: {faltan}"

    def test_ninguna_ruta_montada_se_queda_fuera(self):
        """
        Recorre las rutas REALES de la app en vez de una lista escrita a mano.

        La lista de arriba solo protege lo que alguien se acordo de anotar:
        /health llevaba sin limite y este test pasaba en verde, porque nadie lo
        habia anadido. Un endpoint nuevo entra aqui solo.
        """
        sin_limite = []
        for ruta in app.routes:
            camino = getattr(ruta, "path", "")
            fn = getattr(ruta, "endpoint", None)
            metodos = getattr(ruta, "methods", None)
            if fn is None or not metodos or camino in self.SIN_LIMITE_JUSTIFICADO:
                continue
            if not hasattr(fn, "__wrapped__"):
                sin_limite.append((sorted(metodos), camino))

        assert not sin_limite, (
            f"rutas sin rate limit: {sin_limite}. Anade @limiter.limit(...) o, "
            "si de verdad debe quedar fuera, justificalo en "
            "SIN_LIMITE_JUSTIFICADO."
        )

    def test_el_poller_tiene_un_limite_mas_estricto(self):
        """Una pasada del poller cuesta ~30 s: su limite debe ser bajo."""
        from core.seguridad import LIMITE_NEGOCIO, LIMITE_POLLER
        n_poller = int(LIMITE_POLLER.split("/")[0])
        n_negocio = int(LIMITE_NEGOCIO.split("/")[0])
        assert n_poller < n_negocio
