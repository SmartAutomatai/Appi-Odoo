"""
Pruebas de la capa de seguridad compartida (core/seguridad.py).

Fijan el contrato de la auditoria H-1: la comparacion de la API Key debe ser
en tiempo constante y leerse del entorno en cada llamada, no congelarse en el
import. Un "!=" o una constante de modulo harian fallar estas pruebas.
"""

import hmac
import inspect

import pytest
from fastapi import HTTPException

from core.seguridad import (
    error_interno,
    resolver_tenant,
    sanear_error_odoo,
    verify_api_key,
)


class TestVerificacionApiKey:
    def test_acepta_clave_correcta(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-buena")
        verify_api_key("clave-buena")          # no debe lanzar

    def test_rechaza_clave_incorrecta(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-buena")
        with pytest.raises(HTTPException) as e:
            verify_api_key("clave-mala")
        assert e.value.status_code == 401

    def test_rechaza_clave_ausente(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-buena")
        with pytest.raises(HTTPException) as e:
            verify_api_key(None)
        assert e.value.status_code == 401

    def test_rechaza_prefijo_correcto(self, monkeypatch):
        """Un prefijo valido no basta: es el caso que explota el timing attack."""
        monkeypatch.setenv("API_KEY", "clave-buena")
        with pytest.raises(HTTPException):
            verify_api_key("clave-bue")

    def test_sin_api_key_configurada_pasa(self, monkeypatch):
        """Modo desarrollo: sin API_KEY en el entorno, no se valida."""
        monkeypatch.delenv("API_KEY", raising=False)
        verify_api_key(None)                   # no debe lanzar

    def test_lee_el_entorno_en_cada_llamada(self, monkeypatch):
        """Rotar la clave surte efecto sin reiniciar el servicio."""
        monkeypatch.setenv("API_KEY", "primera")
        verify_api_key("primera")
        monkeypatch.setenv("API_KEY", "segunda")
        with pytest.raises(HTTPException):
            verify_api_key("primera")
        verify_api_key("segunda")

    def test_usa_comparacion_en_tiempo_constante(self):
        """
        Guardia contra regresiones: el codigo debe usar hmac.compare_digest.
        El "!=" corta en el primer byte distinto y filtra cuantos acerto.
        """
        fuente = inspect.getsource(verify_api_key)
        assert "compare_digest" in fuente
        assert hmac.compare_digest is not None


class TestResolverTenant:
    def test_tenant_desconocido_da_400(self):
        with pytest.raises(HTTPException) as e:
            resolver_tenant("no-existe")
        assert e.value.status_code == 400


class TestErrorInterno:
    """Auditoria H-5: el 500 no debe filtrar el texto de la excepcion."""

    def test_no_devuelve_el_texto_de_la_excepcion(self):
        secreto = "postgresql://usuario:clave@10.0.0.5/produccion"
        exc = RuntimeError(f"fallo conectando a {secreto}")
        http = error_interno(exc, "contexto de prueba")
        assert http.status_code == 500
        assert secreto not in http.detail
        assert "fallo conectando" not in http.detail

    def test_devuelve_una_referencia_para_cruzar_con_el_log(self):
        http = error_interno(ValueError("x"), "ctx")
        assert "Referencia:" in http.detail
        # 12 hex del uuid, suficiente para localizarlo en el log.
        referencia = http.detail.rsplit(": ", 1)[1]
        assert len(referencia) == 12 and all(c in "0123456789abcdef" for c in referencia)

    def test_cada_incidencia_tiene_su_propia_referencia(self):
        a = error_interno(ValueError("a"), "ctx")
        b = error_interno(ValueError("b"), "ctx")
        assert a.detail != b.detail

    def test_registra_la_traza_completa_en_el_log(self, caplog):
        """Lo que no va al cliente SI debe quedar en el log del servidor."""
        with caplog.at_level("ERROR"):
            http = error_interno(RuntimeError("detalle-secreto-xyz"), "ctx-abc")
        registrado = caplog.text
        assert "detalle-secreto-xyz" in registrado
        assert "ctx-abc" in registrado
        referencia = http.detail.rsplit(": ", 1)[1]
        assert referencia in registrado, "la referencia debe permitir cruzar log y cliente"


class TestSaneadoDeErroresDeOdoo:
    """
    Auditoria H-5b: el 422 conserva los errores de negocio, pero no el SQL.

    Es un equilibrio deliberado: si se ocultara todo, el cliente perderia la
    informacion que necesita para corregir sus datos y la API dejaria de ser
    util; si se dejara todo, se regala el esquema de la base.
    """

    ERRORES_DE_NEGOCIO = [
        "El asiento (MISC/2026/0001) debe ser un borrador.",
        "No se puede validar un pago sin importe.",
        "El diario BNK1 no existe en la compania actual.",
    ]

    ERRORES_CON_SQL = [
        chr(34).join(["invalid input syntax for type bigint: ", "abc"])
        + chr(10) + "LINE 23: ...LIMIT abc",
        "relation " + chr(34) + "account_move_line" + chr(34) + " does not exist",
        "null value in column " + chr(34) + "move_id" + chr(34)
        + " violates not-null constraint",
        "psycopg2.errors.UniqueViolation: duplicate key",
    ]

    @pytest.mark.parametrize("mensaje", ERRORES_DE_NEGOCIO)
    def test_los_errores_de_negocio_pasan_intactos(self, mensaje):
        assert sanear_error_odoo(Exception(mensaje)) == mensaje

    @pytest.mark.parametrize("mensaje", ERRORES_CON_SQL)
    def test_los_errores_con_sql_se_ocultan(self, mensaje):
        salida = sanear_error_odoo(Exception(mensaje))
        assert salida != mensaje
        assert "Referencia:" in salida
        for fragmento in ("LINE", "relation", "column", "psycopg", "constraint"):
            assert fragmento not in salida

    def test_no_filtra_nombres_de_tablas(self):
        salida = sanear_error_odoo(
            Exception('relation "account_move_line" does not exist'))
        assert "account_move_line" not in salida

    def test_el_texto_completo_queda_en_el_log(self, caplog):
        with caplog.at_level("WARNING"):
            salida = sanear_error_odoo(Exception('relation "secreta_xyz" does not exist'))
        assert "secreta_xyz" in caplog.text
        referencia = salida.rsplit(": ", 1)[1]
        assert referencia in caplog.text


class TestTenantEnListaBlanca:
    """
    El tenant viaja en el JSON del cliente: no se acepta a ciegas.

    Hallazgo de la auditoria (21/09/2026): resolver_tenant() solo comprobaba
    que el tenant estuviese REGISTRADO, asi que quien tuviera la API Key podia
    elegir contra que instancia de Odoo escribir con solo cambiar un campo.
    """

    def test_por_defecto_solo_default(self, monkeypatch):
        monkeypatch.setenv("TENANTS_PERMITIDOS", "")
        from core.seguridad import tenants_permitidos
        assert tenants_permitidos() == ("default",)

    def test_rechaza_un_tenant_registrado_pero_no_permitido(self, monkeypatch):
        # El caso que importa: existe en _tenants, luego get_tenant lo
        # devolveria. Lo frena la lista blanca, no la ausencia de registro.
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
