"""
Tests unitarios e de integracion del middleware API-Odoo.
Ejecutar con: pytest tests/ -v
"""

import pytest
from unittest.mock import MagicMock, patch

# Parchamos OdooUniversalAPI._login antes de importar api.py
# para evitar conexion real con Odoo durante los tests.
with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    import os
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api as api_module
    from api import app

from fastapi.testclient import TestClient
import odoo_universal

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_api_key(monkeypatch):
    """Por defecto, desactiva la API Key para simplificar tests."""
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(api_module, "ALLOWED_MODELS", set())
    monkeypatch.setattr(api_module, "ALLOWED_METHODS", set())


@pytest.fixture(autouse=True)
def mock_execute(monkeypatch):
    """Reemplaza odoo.execute con un mock en todos los tests."""
    mock = MagicMock(return_value=[{"id": 1, "name": "Test"}])
    if "default" in odoo_universal._tenants:
        monkeypatch.setattr(odoo_universal._tenants["default"], "execute", mock)
    return mock


# --- /health ---


class TestHealth:
    def test_responde_200_sin_autenticacion(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "odoo_conectado" in data

    def test_muestra_estado_ok_cuando_odoo_conectado(self):
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "ok"
        assert data["odoo_conectado"] is True

    def test_health_publico_no_filtra_la_lista_blanca(self):
        # Auditoria: /health en abierto revelaba modelos/metodos permitidos,
        # la version de Odoo y el estado de la autenticacion. Un anonimo no
        # debe obtener de aqui un mapa de la superficie interna.
        data = client.get("/health").json()
        for campo in ("modelos_permitidos", "metodos_permitidos",
                      "odoo_version", "autenticacion"):
            assert campo not in data, f"/health publico no debe exponer {campo}"

    def test_detalle_exige_api_key(self, monkeypatch):
        # El detalle operativo se movio a /health/detalle, protegido.
        # Con API_KEY configurada, sin cabecera -> 401.
        monkeypatch.setenv("API_KEY", "clave-de-prueba")
        assert client.get("/health/detalle").status_code == 401
        # Con la cabecera correcta -> 200 y ahi si aparece la lista blanca.
        r = client.get("/health/detalle", headers={"X-Api-Key": "clave-de-prueba"})
        assert r.status_code == 200
        assert "modelos_permitidos" in r.json()


# --- /odoo autenticacion ---


class TestAutenticacion:
    def test_rechaza_sin_api_key_cuando_configurada(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-secreta")
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 401

    def test_rechaza_api_key_incorrecta(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-secreta")
        response = client.post(
            "/odoo",
            json={"model": "res.partner", "method": "search_read"},
            headers={"X-Api-Key": "clave-incorrecta"},
        )
        assert response.status_code == 401

    def test_acepta_api_key_correcta(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-secreta")
        response = client.post(
            "/odoo",
            json={"model": "res.partner", "method": "search_read"},
            headers={"X-Api-Key": "clave-secreta"},
        )
        assert response.status_code == 200
        assert "result" in response.json()

    def test_permite_sin_api_key_cuando_no_configurada(self):
        """Sin API_KEY en .env, el endpoint es accesible (modo desarrollo)."""
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 200


# --- /odoo whitelist ---


class TestWhitelist:
    def test_modelo_no_permitido(self, monkeypatch):
        monkeypatch.setattr(api_module, "ALLOWED_MODELS", {"res.partner"})
        response = client.post("/odoo", json={
            "model": "hr.employee", "method": "search_read"
        })
        assert response.status_code == 422

    def test_metodo_no_permitido(self, monkeypatch):
        monkeypatch.setattr(api_module, "ALLOWED_METHODS", {"search_read", "read"})
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "unlink"
        })
        assert response.status_code == 422

    def test_modelo_permitido(self, monkeypatch):
        monkeypatch.setattr(api_module, "ALLOWED_MODELS", {"res.partner"})
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 200

    def test_sin_whitelist_permite_todo(self):
        """Sin ALLOWED_MODELS/METHODS configurados, permite cualquier modelo/metodo."""
        response = client.post("/odoo", json={
            "model": "cualquier.modelo", "method": "cualquier_metodo"
        })
        assert response.status_code == 200

    def test_el_rechazo_no_enumera_la_lista_blanca(self, monkeypatch):
        # Auditoria: el 422 no debe regalar el mapa completo de modelos/metodos
        # permitidos (mismo dato que se retiro de /health).
        monkeypatch.setattr(api_module, "ALLOWED_MODELS", {"res.partner", "account.move"})
        monkeypatch.setattr(api_module, "ALLOWED_METHODS", {"search_read"})

        r = client.post("/odoo", json={"model": "hr.employee", "method": "search_read"})
        assert r.status_code == 422
        cuerpo = r.text
        assert "hr.employee" in cuerpo, "debe decir QUE modelo se rechazo"
        assert "account.move" not in cuerpo, "no debe listar los demas permitidos"

        r2 = client.post("/odoo", json={"model": "res.partner", "method": "unlink"})
        assert r2.status_code == 422
        assert "search_read" not in r2.text, "no debe listar los metodos permitidos"


# --- /odoo errores ---


class TestErrores:
    def test_error_de_conexion_devuelve_503(self, monkeypatch):
        if "default" in odoo_universal._tenants:
            monkeypatch.setattr(
                odoo_universal._tenants["default"],
                "execute",
                MagicMock(side_effect=odoo_universal.OdooConnectionError("Sin conexion")),
            )
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 503

    def test_error_de_ejecucion_devuelve_422(self, monkeypatch):
        if "default" in odoo_universal._tenants:
            monkeypatch.setattr(
                odoo_universal._tenants["default"],
                "execute",
                MagicMock(side_effect=odoo_universal.OdooExecutionError("Campo no existe")),
            )
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 422

    def test_tenant_no_configurado_devuelve_400(self):
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read", "tenant": "inexistente"
        })
        assert response.status_code == 400


# --- /odoo funcionalidad ---


class TestFuncionalidad:
    def test_respuesta_exitosa_contiene_result(self):
        response = client.post("/odoo", json={
            "model": "res.partner",
            "method": "search_read",
            "args": [[["customer_rank", ">", 0]]],
            "kwargs": {"fields": ["name", "email"], "limit": 5},
        })
        assert response.status_code == 200
        data = response.json()
        assert "result" in data
        assert isinstance(data["result"], list)

    def test_tenant_default_se_usa_por_defecto(self, mock_execute):
        client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        mock_execute.assert_called_once()
