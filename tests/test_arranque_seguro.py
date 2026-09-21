"""
Tests del arranque seguro (auditoria H-4).

Sin API_KEY el servicio queda ENTERO sin autenticacion. Antes eso pasaba en
silencio: un .env mal desplegado publicaba el middleware abierto y /health
seguia diciendo "ok". Ahora el modo abierto hay que pedirlo a proposito.

La comprobacion vive a nivel de import de api.py, asi que estos tests lanzan
un proceso aparte: dentro de pytest el modulo ya esta importado y la
comprobacion se relaja adrede (la suite prueba justamente el modo abierto).
"""

import os
import subprocess
import sys

_IMPORTAR_API = (
    "import api; "
    "print('IMPORTADO', api.AUTENTICACION_ACTIVA, api.ENTORNO)"
)


def _arrancar(entorno_extra, tmp_path):
    """
    Importa api.py en un proceso limpio, sin la marca de pytest.

    Se ejecuta desde un directorio temporal SIN .env: api.py llama a
    load_dotenv(), que busca el .env relativo al cwd, y el del repo
    reintroduciria API_KEY invalidando la prueba. El repo va en PYTHONPATH
    para que el import siga resolviendo.
    """
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items()
           if k != "PYTEST_CURRENT_TEST" and k != "API_KEY"}
    env.update({
        "ODOO_URL": "https://test-odoo.invalid",
        "ODOO_DB": "test-db",
        "ODOO_USERNAME": "u",
        "ODOO_PASSWORD": "p",
        "PYTHONPATH": raiz,
        # Base de control propia: no tocar la del proyecto.
        "DATABASE_URL": "sqlite:///" + str(tmp_path / "control.db").replace("\\", "/"),
    })
    env.pop("SOURCE_DATABASE_URL", None)
    env.update(entorno_extra)
    return subprocess.run(
        [sys.executable, "-c", _IMPORTAR_API],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=180,
    )


class TestArranqueSinApiKey:
    def test_produccion_sin_api_key_no_arranca(self, tmp_path):
        r = _arrancar({"ENTORNO": "produccion"}, tmp_path)
        assert r.returncode != 0, "deberia negarse a arrancar sin clave"
        assert "API_KEY no configurada" in r.stderr

    def test_desarrollo_sin_api_key_arranca_con_aviso(self, tmp_path):
        r = _arrancar({"ENTORNO": "desarrollo"}, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "IMPORTADO" in r.stdout
        assert "SIN PROTEGER" in r.stderr, "debe avisar de que esta abierto"

    def test_con_api_key_arranca_en_produccion(self, tmp_path):
        r = _arrancar({"ENTORNO": "produccion", "API_KEY": "una-clave"}, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "IMPORTADO True" in r.stdout


class TestExponerDocs:
    """
    /docs, /redoc y /openapi.json revelan toda la estructura de la API.
    EXPONER_DOCS=false debe cerrarlos (auditoria, seccion 1). Se comprueba a
    nivel de import porque la URL se fija al construir la app.
    """

    def _import_docs(self, extra, tmp_path):
        script = "import api; print('DOCS', api.app.openapi_url, api.app.docs_url)"
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTEST_CURRENT_TEST",)}
        env.update({
            "ODOO_URL": "https://test-odoo.invalid", "ODOO_DB": "d",
            "ODOO_USERNAME": "u", "ODOO_PASSWORD": "p",
            "API_KEY": "una-clave", "PYTHONPATH": raiz,
            "DATABASE_URL": "sqlite:///" + str(tmp_path / "c.db").replace("\\", "/"),
        })
        env.pop("SOURCE_DATABASE_URL", None)
        env.update(extra)
        return subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, env=env,
                              cwd=str(tmp_path), timeout=180)

    def test_por_defecto_docs_abiertos(self, tmp_path):
        r = self._import_docs({}, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "DOCS /openapi.json /docs" in r.stdout

    def test_exponer_docs_false_los_cierra(self, tmp_path):
        r = self._import_docs({"EXPONER_DOCS": "false"}, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "DOCS None None" in r.stdout


class TestSaludReflejaLaAutenticacion:
    def test_health_expone_el_estado_de_autenticacion(self, monkeypatch):
        """Auditoria H-4: monitorizacion debe poder ver el modo abierto."""
        from unittest.mock import patch
        with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
            os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
            os.environ.setdefault("ODOO_DB", "test-db")
            os.environ.setdefault("ODOO_USERNAME", "u")
            os.environ.setdefault("ODOO_PASSWORD", "p")
            from api import app
        from fastapi.testclient import TestClient

        cliente = TestClient(app)

        # El estado de autenticacion se movio a /health/detalle (protegido):
        # /health en abierto ya no filtra la superficie interna.
        monkeypatch.setenv("API_KEY", "algo")
        det = cliente.get("/health/detalle", headers={"X-Api-Key": "algo"})
        assert det.json()["autenticacion"] == "activa"

        # Sin API_KEY el servicio esta en modo abierto: verify_api_key no
        # valida, asi que el detalle responde sin cabecera y reporta el modo.
        monkeypatch.delenv("API_KEY", raising=False)
        assert cliente.get("/health/detalle").json()["autenticacion"] == "DESACTIVADA"
