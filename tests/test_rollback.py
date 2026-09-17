"""
Tests del rollback logico / compensacion (Fase 5).
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store con SQLite temporal + rollback recargado contra el."""
    db_file = tmp_path / "control_rb.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.rollback as rollback
    importlib.reload(rollback)
    return rollback, state_store


class TestCancelarFactura:
    def test_cancela_con_draft_y_cancel(self, entorno):
        rollback, state_store = entorno
        odoo = MagicMock()
        odoo.execute.return_value = True

        estado = rollback.cancelar_factura(42, odoo, id_origen="F-1", motivo="pago fallo")

        assert estado == "cancelada"
        metodos = [c.args[1] for c in odoo.execute.call_args_list]
        assert metodos == ["button_draft", "button_cancel"]

    def test_fallo_al_llevar_a_borrador_no_relanza_y_loguea(self, entorno):
        rollback, state_store = entorno
        from odoo_universal import OdooExecutionError

        odoo = MagicMock()
        # Falla ya el button_draft: los asientos siguen vivos -> fallo real.
        odoo.execute.side_effect = OdooExecutionError("no se puede pasar a borrador")

        estado = rollback.cancelar_factura(42, odoo, id_origen="F-2", motivo="x")
        assert estado == ""

        # Debe haber un log ERROR de rollback para intervencion manual.
        from core.models_db import SyncLog
        with state_store.get_session() as s:
            logs = s.query(SyncLog).filter_by(id_origen="F-2", accion="rollback").all()
            assert len(logs) == 1
            assert logs[0].resultado == "ERROR"

    def test_draft_ok_pero_cancel_bloqueado_es_exito(self, entorno):
        """
        Con la localizacion VE, button_cancel puede fallar porque hay un
        documento de retencion de IVA vinculado. La factura queda en draft SIN
        apuntes vigentes: es compensacion aceptable, no un fallo.
        """
        rollback, state_store = entorno
        from odoo_universal import OdooExecutionError

        odoo = MagicMock()

        def execute(model, method, *args, **kwargs):
            if method == "button_draft":
                return True                      # a borrador: OK
            if method == "button_cancel":
                raise OdooExecutionError(
                    "No puede cancelar una factura cuyo documento de retencion "
                    "de IVA no esta cancelado."
                )
            return True

        odoo.execute.side_effect = execute

        # draft si, cancel no -> se considera exito (sin contabilidad viva).
        estado = rollback.cancelar_factura(42, odoo, id_origen="F-3", motivo="descuadre")
        assert estado == "borrador"

        # El log es OK (no ERROR): no requiere intervencion urgente, pero deja
        # constancia de que quedo en borrador.
        from core.models_db import SyncLog
        with state_store.get_session() as s:
            logs = s.query(SyncLog).filter_by(id_origen="F-3", accion="rollback").all()
            assert len(logs) == 1
            assert logs[0].resultado == "OK"
            assert "BORRADOR" in logs[0].detalle or "borrador" in logs[0].detalle.lower()
