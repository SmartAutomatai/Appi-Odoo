"""
Compensacion del descuadre en TODOS los caminos de sincronizacion.

El hueco que cubren: validar_total corre despues del action_post, asi que un
documento descuadrado queda posteado en Odoo -un asiento contable real- aunque
el middleware devuelva error. La compensacion existia solo en el poller: por
HTTP el cliente recibia un 422 y el asiento se quedaba vivo. Se detecto en una
prueba de carga, con 8 facturas posteadas y 24 apuntes contables huerfanos.
"""

from unittest.mock import MagicMock

import pytest


def _odoo_descuadrado(total_odoo=1150.0, modelo="account.move"):
    """Odoo postea el documento pero devuelve un importe distinto del enviado."""
    odoo = MagicMock()
    odoo.acciones = []

    def execute(model, method, *a, **k):
        if model == "res.partner":
            return [{"id": 7}]
        if model == "account.journal":
            return [{"id": 6}]
        if method == "create":
            return 999
        if method == "action_post":
            return True
        if method == "read":
            campo = "amount" if modelo == "account.payment" else "amount_total"
            return [{campo: total_odoo, "state": "posted"}]
        if method in ("button_draft", "button_cancel", "action_cancel"):
            odoo.acciones.append(method)
            return True
        return None

    odoo.execute.side_effect = execute
    return odoo


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
    monkeypatch.setenv("POLLER_CANCELAR_DESCUADRE", "true")
    import importlib
    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()
    yield state_store


class TestCompensacionPorHTTP:
    """El modo push debe compensar igual que el poller."""

    def test_una_factura_descuadrada_se_cancela(self):
        from core.facturacion import crear_factura
        from core.sincronizador import SincronizacionError

        odoo = _odoo_descuadrado()
        with pytest.raises(SincronizacionError) as exc:
            crear_factura({
                "factura_id": "F-DESC", "cliente_nif": "B1", "fecha": "2026-08-26",
                "total": 800.0, "lineas": [],
            }, odoo)

        assert odoo.acciones == ["button_draft", "button_cancel"], (
            "la factura descuadrada quedaria posteada en Odoo"
        )
        assert "CANCELAD" in str(exc.value), "el error no avisa de la cancelacion"

    def test_factura_descuadrada_con_cancel_bloqueado_dice_borrador(self):
        """
        Con la localizacion VE, button_cancel se bloquea (documento de retencion
        de IVA). La factura queda en draft sin apuntes vigentes: el mensaje debe
        decir BORRADOR, no 'CANCELADO' (que seria enganoso).
        """
        from unittest.mock import MagicMock
        from core.facturacion import crear_factura
        from core.sincronizador import SincronizacionError
        from odoo_universal import OdooExecutionError

        odoo = MagicMock()
        odoo.acciones = []

        def execute(model, method, *a, **k):
            if model == "res.partner":
                return [{"id": 7}]
            if model == "account.journal":
                return [{"id": 6}]
            if method == "create":
                return 999
            if method == "action_post":
                return True
            if method == "read":
                return [{"amount_total": 1150.0, "state": "posted"}]
            if method == "button_draft":
                odoo.acciones.append(method)
                return True
            if method == "button_cancel":
                odoo.acciones.append(method)
                raise OdooExecutionError(
                    "No puede cancelar una factura cuyo documento de retencion "
                    "de IVA no esta cancelado."
                )
            return None

        odoo.execute.side_effect = execute

        with pytest.raises(SincronizacionError) as exc:
            crear_factura({
                "factura_id": "F-WH", "cliente_nif": "B1", "fecha": "2026-08-26",
                "total": 800.0, "lineas": [],
            }, odoo)

        # Intento los dos pasos, pero el mensaje refleja que quedo en BORRADOR.
        assert odoo.acciones == ["button_draft", "button_cancel"]
        assert "BORRADOR" in str(exc.value)
        assert "CANCELADO" not in str(exc.value), (
            "no debe decir 'cancelado' si quedo en borrador"
        )

    def test_un_pago_descuadrado_se_cancela(self):
        """
        account.payment NO tiene button_draft/button_cancel (comprobado contra
        Odoo 19: "the method does not exist"): usa action_cancel.
        """
        from core.pagos import crear_pago
        from core.sincronizador import SincronizacionError

        odoo = _odoo_descuadrado(total_odoo=99.0, modelo="account.payment")
        with pytest.raises(SincronizacionError):
            crear_pago({
                "pago_id": "P-DESC", "cliente_nif": "B1",
                "diario_codigo": "BNK1", "monto": 500.0, "fecha": "2026-08-26",
            }, odoo)

        assert odoo.acciones == ["action_cancel"]

    def test_se_puede_desactivar(self, monkeypatch):
        from core.facturacion import crear_factura
        from core.sincronizador import SincronizacionError

        monkeypatch.setenv("POLLER_CANCELAR_DESCUADRE", "false")
        odoo = _odoo_descuadrado()
        with pytest.raises(SincronizacionError):
            crear_factura({"factura_id": "F-OFF", "cliente_nif": "B1",
                           "fecha": "2026-08-26", "total": 800.0, "lineas": []}, odoo)
        assert odoo.acciones == []


class TestNoCompensaOtrosFallos:
    """
    Solo el descuadre. Un fallo de mapeo no llego a crear nada, y uno de
    action_post deja la factura en borrador: no contabiliza y puede ser
    transitorio.
    """

    def test_un_fallo_de_mapeo_no_cancela_nada(self):
        from core.facturacion import crear_factura
        from core.sincronizador import SincronizacionError

        odoo = MagicMock()
        odoo.acciones = []

        def execute(model, method, *a, **k):
            if model == "res.partner":
                return []            # el cliente no existe -> MapeoError
            if method in ("button_draft", "button_cancel", "action_cancel"):
                odoo.acciones.append(method)
            return None

        odoo.execute.side_effect = execute
        with pytest.raises(SincronizacionError):
            crear_factura({"factura_id": "F-MAP", "cliente_nif": "NADIE",
                           "fecha": "2026-08-26", "total": 100.0, "lineas": []}, odoo)
        assert odoo.acciones == []

    def test_un_fallo_de_posteo_no_cancela(self):
        from core.facturacion import crear_factura
        from core.sincronizador import SincronizacionError
        from odoo_universal import OdooExecutionError

        odoo = MagicMock()
        odoo.acciones = []

        def execute(model, method, *a, **k):
            if model == "res.partner":
                return [{"id": 7}]
            if method == "create":
                return 999
            if method == "action_post":
                raise OdooExecutionError("diario cerrado")
            if method == "read":
                return [{"state": "draft"}]
            if method in ("button_draft", "button_cancel", "action_cancel"):
                odoo.acciones.append(method)
            return None

        odoo.execute.side_effect = execute
        with pytest.raises(SincronizacionError):
            crear_factura({"factura_id": "F-POST", "cliente_nif": "B1",
                           "fecha": "2026-08-26", "total": 100.0, "lineas": []}, odoo)
        assert odoo.acciones == [], "un borrador no contabiliza: no hay que cancelarlo"
