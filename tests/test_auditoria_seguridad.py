"""
Fija las correcciones de la auditoria de seguridad (21/09/2026).

Estos tests estan escritos para FALLAR si alguien deshace una correccion, no
solo para pasar hoy: cada uno reproduce el agujero concreto que se cerro.

El mas importante es el del limite global. El limitador ya declaraba
default_limits, pero sin SlowAPIMiddleware montado ese valor NO se aplica: solo
limitaban los endpoints con decorador propio y el resto quedaba abierto. Leyendo
el codigo parecia correcto; solo se vio ejecutandolo.
"""

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def app(monkeypatch):
    """
    Devuelve una fabrica que importa api.py de cero con el entorno indicado.

    El entorno se deja PUESTO mientras dure el test (lo revierte monkeypatch al
    terminar): api.py y core/seguridad.py releen API_KEY y las whitelists en
    cada peticion, asi que restaurarlas antes del request haria que el test
    midiera otra configuracion distinta de la que dice medir.
    """
    def fabrica(**entorno):
        base = {
            "ODOO_URL": "http://falso.test", "ODOO_DB": "d",
            "ODOO_USERNAME": "u", "ODOO_PASSWORD": "p",
            "API_KEY": "k" * 32, "RATE_LIMIT_GLOBAL": "200/minute",
            "ALLOWED_MODELS": "res.partner", "ALLOWED_METHODS": "search_read",
            "EXPONER_DOCS": "",
        }
        base.update(entorno)
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        # core.limites construye el Limiter al importarse, leyendo
        # RATE_LIMIT_GLOBAL una sola vez. Hay que recargarlo DESPUES de fijar
        # el entorno o la app se monta con el limite del import anterior; de
        # paso, el limitador nuevo trae el contador a cero, asi que un test que
        # agota la cuota no deja a los siguientes recibiendo 429.
        import core.limites
        importlib.reload(core.limites)

        sys.modules.pop("api", None)
        with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
            import api
            importlib.reload(api)
            return TestClient(api.app)
    return fabrica


class TestLimiteGlobal:
    """El limite debe cubrir TODAS las rutas, no solo las decoradas."""

    def test_una_ruta_sin_decorador_tambien_se_limita(self, app):
        # /health no lleva decorador propio: depende del default_limits, que a
        # su vez depende de que SlowAPIMiddleware este montado.
        cliente = app(RATE_LIMIT_GLOBAL="5/minute")
        codigos = [cliente.get("/health").status_code for _ in range(9)]
        assert 429 in codigos, (
            "Ninguna peticion fue rechazada: el limite global no se aplica. "
            "Comprueba que app.add_middleware(SlowAPIMiddleware) sigue en api.py."
        )

    def test_el_middleware_esta_montado(self):
        sys.modules.pop("api", None)
        with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
            import api
        nombres = [m.cls.__name__ for m in api.app.user_middleware]
        assert "SlowAPIMiddleware" in nombres, (
            f"SlowAPIMiddleware no esta montado (hay: {nombres}). Sin el, "
            "default_limits queda en decoracion muerta."
        )


class TestHealthNoFiltraWhitelists:
    """/health es publico: no debe publicar el mapa de lo que acepta /odoo."""

    def test_sin_clave_no_revela_las_listas(self, app):
        cuerpo = app().get("/health").json()
        assert "modelos_permitidos" not in cuerpo
        assert "metodos_permitidos" not in cuerpo

    def test_con_clave_valida_si_las_revela(self, app):
        # El detalle sigue siendo util para diagnosticar, autenticandose.
        cuerpo = app().get("/health", headers={"X-Api-Key": "k" * 32}).json()
        assert cuerpo["modelos_permitidos"] == ["res.partner"]

    def test_con_clave_invalida_no_las_revela(self, app):
        cuerpo = app().get("/health", headers={"X-Api-Key": "x" * 32}).json()
        assert "modelos_permitidos" not in cuerpo

    def test_sigue_respondiendo_200_sin_clave(self, app):
        # Un monitor no envia cabeceras: si esto pasa a 401, se rompe el
        # health check del balanceador.
        assert app().get("/health").status_code == 200


class TestDocumentacionCerrada:
    """El esquema completo de la API no se publica por defecto."""

    @pytest.mark.parametrize("ruta", ["/docs", "/openapi.json", "/redoc"])
    def test_cerrada_por_defecto(self, app, ruta):
        assert app().get(ruta).status_code == 404

    @pytest.mark.parametrize("ruta", ["/docs", "/openapi.json"])
    def test_se_puede_abrir_a_proposito(self, app, ruta):
        assert app(EXPONER_DOCS="true").get(ruta).status_code == 200


class TestTenantEnListaBlanca:
    """El tenant viaja en el JSON del cliente: no se acepta a ciegas."""

    def test_por_defecto_solo_default(self, monkeypatch):
        monkeypatch.setenv("TENANTS_PERMITIDOS", "")
        from core.seguridad import tenants_permitidos
        assert tenants_permitidos() == ("default",)

    def test_rechaza_un_tenant_registrado_pero_no_permitido(self, monkeypatch):
        # El caso que importa: existe en _tenants, luego get_tenant lo
        # devolveria; lo frena la lista blanca, no la ausencia de registro.
        from fastapi import HTTPException
        from unittest.mock import MagicMock
        import odoo_universal
        from core.seguridad import resolver_tenant

        odoo_universal._tenants["cliente_ajeno"] = MagicMock()
        monkeypatch.setenv("TENANTS_PERMITIDOS", "")
        try:
            with pytest.raises(HTTPException) as e:
                resolver_tenant("cliente_ajeno")
            assert e.value.status_code == 400
            assert "no permitido" in e.value.detail
        finally:
            odoo_universal._tenants.pop("cliente_ajeno", None)

    def test_se_abre_explicitamente(self, monkeypatch):
        from unittest.mock import MagicMock
        import odoo_universal
        from core.seguridad import resolver_tenant

        conector = MagicMock()
        odoo_universal._tenants["cliente_ajeno"] = conector
        monkeypatch.setenv("TENANTS_PERMITIDOS", "default,cliente_ajeno")
        try:
            assert resolver_tenant("cliente_ajeno") is conector
        finally:
            odoo_universal._tenants.pop("cliente_ajeno", None)
