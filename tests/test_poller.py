"""
Tests del poller (modo pull: DB del cliente -> Odoo).

Aislan tanto la base de CONTROL como la de ORIGEN con SQLite temporal y mockean
Odoo, verificando:
  - una fila PENDIENTE se sincroniza y queda PROCESADO en la cola del cliente,
  - un fallo de datos aisla la fila (ERROR) sin abortar el resto del lote,
  - un fallo de conexion aborta el lote y deja la fila PENDIENTE (para reintento),
  - la idempotencia: reprocesar no duplica en Odoo.
"""

import importlib
from unittest.mock import MagicMock

import pytest

from odoo_universal import OdooConnectionError


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """
    state_store + poller_source con SQLite temporal (control y origen separados),
    y el poller recargado contra ellos. Registra un tenant mock 'default'.
    """
    control_db = tmp_path / "control.db"
    source_db = tmp_path / "source.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{control_db}")
    monkeypatch.setenv("SOURCE_DATABASE_URL", f"sqlite:///{source_db}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.poller_source as poller_source
    importlib.reload(poller_source)
    poller_source.init_source_db()

    import core.poller as poller
    importlib.reload(poller)

    return poller, poller_source, state_store


def _odoo(existe_partner=True):
    """
    Mock de Odoo para una factura simple:
      res.partner search_read -> id 7 (por vat) si existe_partner
      account.move create -> 999 ; action_post -> OK ; read (amount_total) -> ok
    """
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "res.partner" and method == "search_read":
            return [{"id": 7}] if existe_partner else []
        if model == "res.currency" and method == "search_read":
            return [{"id": 2}]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "read":
            return [{"id": 999, "amount_total": 121.0}]
        return True

    odoo.execute.side_effect = execute
    return odoo


def _insertar_factura(poller_source, id_origen="F-1", total=121.0, nif="B123"):
    """Inserta una fila 'factura' PENDIENTE en la cola del cliente."""
    with poller_source.get_source_session() as s:
        s.add(
            poller_source.ColaSincronizacion(
                entidad="factura",
                id_origen=id_origen,
                payload={
                    "factura_id": id_origen,
                    "cliente_nif": nif,
                    "fecha": "2026-07-01",
                    "total": total,
                    "lineas": [],
                },
            )
        )


def _estado_cola(poller_source, id_origen):
    with poller_source.get_source_session() as s:
        fila = (
            s.query(poller_source.ColaSincronizacion)
            .filter_by(id_origen=id_origen)
            .one()
        )
        return fila.estado, fila.error_detalle


def test_fila_pendiente_se_sincroniza_y_queda_procesada(entorno):
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)
    odoo = _odoo()
    monkey_tenant(poller, odoo)

    res = poller.procesar_lote()

    assert res.leidas == 1
    assert res.procesadas == 1
    assert res.con_error == 0
    assert _estado_cola(poller_source, "F-1")[0] == "PROCESADO"
    # Y quedo mapeado en el state store de control.
    assert state_store.ya_procesado("factura", "F-1") == 999


def test_escribe_id_y_numero_fiscal_de_vuelta_en_la_cola(entorno):
    """
    Al procesar una factura, el poller escribe id_odoo y numero_odoo de vuelta
    en la cola del cliente: es lo que permite cruzar la nota de origen con la
    factura sin entrar a Odoo. El numero se lee del 'name' del account.move.
    """
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)

    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "res.partner" and method == "search_read":
            return [{"id": 7}]
        if model == "res.currency" and method == "search_read":
            return [{"id": 2}]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "read":
            # El poller lee 'name' para el numero fiscal; el sincronizador lee
            # 'amount_total' para validar. Se devuelven ambos.
            return [{"id": 999, "amount_total": 121.0, "name": "INV/2026/00099"}]
        return True

    odoo.execute.side_effect = execute
    monkey_tenant(poller, odoo)

    res = poller.procesar_lote()
    assert res.procesadas == 1

    with poller_source.get_source_session() as s:
        fila = (
            s.query(poller_source.ColaSincronizacion)
            .filter_by(id_origen="F-1")
            .one()
        )
        assert fila.estado == "PROCESADO"
        assert fila.id_odoo == 999
        assert fila.numero_odoo == "INV/2026/00099"


def test_fallo_de_datos_aisla_la_fila_sin_abortar_el_lote(entorno):
    poller, poller_source, state_store = entorno
    # F-1 valida; F-2 con NIF que no existe en Odoo -> MapeoError -> ERROR.
    _insertar_factura(poller_source, id_origen="F-1")
    _insertar_factura(poller_source, id_origen="F-2", nif="NOEXISTE")

    # partner existe solo para B123; para NOEXISTE devuelve vacio.
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "res.partner" and method == "search_read":
            vat = args[0][0][2]  # dominio [["vat","=",valor]]
            return [{"id": 7}] if vat == "B123" else []
        if model == "res.currency" and method == "search_read":
            return [{"id": 2}]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "read":
            return [{"id": 999, "amount_total": 121.0}]
        return True

    odoo.execute.side_effect = execute
    monkey_tenant(poller, odoo)

    res = poller.procesar_lote()

    assert res.leidas == 2
    assert res.procesadas == 1
    assert res.con_error == 1
    assert _estado_cola(poller_source, "F-1")[0] == "PROCESADO"
    estado, error = _estado_cola(poller_source, "F-2")
    assert estado == "ERROR"
    assert error  # trae el detalle del fallo


def test_fallo_de_conexion_aborta_el_lote_y_deja_pendiente(entorno):
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)

    odoo = MagicMock()
    odoo.execute.side_effect = OdooConnectionError("Odoo caido")
    monkey_tenant(poller, odoo)

    with pytest.raises(OdooConnectionError):
        poller.procesar_lote()

    # La fila NO se marca: sigue PENDIENTE para el reintento.
    assert _estado_cola(poller_source, "F-1")[0] == "PENDIENTE"


def test_idempotencia_no_duplica_en_odoo(entorno):
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)
    odoo = _odoo()
    monkey_tenant(poller, odoo)

    poller.procesar_lote()

    # El cliente reinserta la MISMA factura (id_origen repetido) como PENDIENTE.
    _insertar_factura(poller_source, id_origen="F-1")
    creates_antes = _contar_creates(odoo)

    reads_antes = sum(
        1 for c in odoo.execute.call_args_list
        if c.args[0] == "account.move" and c.args[1] == "read"
    )

    res = poller.procesar_lote()

    assert res.procesadas == 1
    # No hubo un segundo create en Odoo: la idempotencia lo cortocircuito.
    assert _contar_creates(odoo) == creates_antes
    # Y TAMPOCO un read: la idempotencia promete no tocar Odoo. Leer el numero
    # fiscal en el reenvio idempotente rompia esa promesa (bug corregido).
    reads_despues = sum(
        1 for c in odoo.execute.call_args_list
        if c.args[0] == "account.move" and c.args[1] == "read"
    )
    assert reads_despues == reads_antes


def test_polling_deshabilitado_devuelve_lote_vacio(entorno, monkeypatch):
    poller, poller_source, state_store = entorno
    monkeypatch.setattr(poller_source, "SOURCE_DATABASE_URL", "")

    res = poller.procesar_lote()

    assert res.leidas == 0
    assert res.procesadas == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def monkey_tenant(poller, odoo):
    """Hace que get_tenant (resuelto dentro de poller) devuelva el mock."""
    poller.get_tenant = lambda *a, **k: odoo


def _contar_creates(odoo):
    return sum(
        1
        for c in odoo.execute.call_args_list
        if c.args[:2] == ("account.move", "create")
    )


# ---------------------------------------------------------------------------
# Compensacion automatica del descuadre
# ---------------------------------------------------------------------------

def _odoo_descuadrado(total_odoo=600.0):
    """
    Mock de Odoo que postea la factura pero devuelve un amount_total distinto
    del enviado por el origen -> dispara DescuadreError DESPUES del action_post.
    Registra las llamadas de cancelacion para poder afirmarlas.
    """
    odoo = MagicMock()
    odoo.acciones = []

    def execute(model, method, *args, **kwargs):
        if model == "res.partner" and method == "search_read":
            return [{"id": 7}]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "action_post":
            return True
        if model == "account.move" and method == "read":
            return [{"amount_total": total_odoo}]
        if model == "account.move" and method in ("button_draft", "button_cancel"):
            odoo.acciones.append(method)
            return True
        return None

    odoo.execute.side_effect = execute
    return odoo


def _encolar_descuadre(poller_source, id_origen="DESC-1"):
    """Encola una factura cuyo total (500) no cuadrara con Odoo (600)."""
    with poller_source.get_source_session() as s:
        s.add(poller_source.ColaSincronizacion(
            entidad="factura", id_origen=id_origen,
            payload={
                "factura_id": id_origen, "cliente_nif": "B1",
                "fecha": "2026-08-21", "total": 500.0,
                "lineas": [[0, 0, {"product_id": 1, "quantity": 1, "price_unit": 600.0}]],
            },
            estado="PENDIENTE",
        ))
        s.commit()


class TestCancelacionAutomatica:
    def test_cancela_la_factura_descuadrada_en_odoo(self, entorno, monkeypatch):
        """
        Una factura posteada pero descuadrada deja un asiento contable real que
        nadie dio por bueno: el poller debe cancelarlo (draft + cancel).
        """
        poller, poller_source, _ = entorno
        monkeypatch.setenv("POLLER_CANCELAR_DESCUADRE", "true")
        odoo = _odoo_descuadrado()
        monkeypatch.setattr(poller, "get_tenant", lambda _t: odoo)
        _encolar_descuadre(poller_source)

        res = poller.procesar_lote()

        assert res.con_error == 1
        assert odoo.acciones == ["button_draft", "button_cancel"], (
            "la factura descuadrada quedaria posteada en Odoo"
        )

    def test_la_fila_queda_en_error_y_lo_deja_dicho(self, entorno, monkeypatch):
        """No se reintenta sola (seria un bucle crear/cancelar) y consta el motivo."""
        poller, poller_source, _ = entorno
        monkeypatch.setenv("POLLER_CANCELAR_DESCUADRE", "true")
        monkeypatch.setattr(poller, "get_tenant", lambda _t: _odoo_descuadrado())
        _encolar_descuadre(poller_source, "DESC-2")

        poller.procesar_lote()

        with poller_source.get_source_session() as s:
            fila = s.query(poller_source.ColaSincronizacion).filter_by(
                id_origen="DESC-2").one()
        assert fila.estado == "ERROR"
        # "CANCELADO/A": el mensaje es comun a facturas y pagos.
        assert "CANCELAD" in (fila.error_detalle or "")

    def test_se_puede_desactivar_por_entorno(self, entorno, monkeypatch):
        """Con POLLER_CANCELAR_DESCUADRE=false la factura se deja para revision."""
        poller, poller_source, _ = entorno
        monkeypatch.setenv("POLLER_CANCELAR_DESCUADRE", "false")
        odoo = _odoo_descuadrado()
        monkeypatch.setattr(poller, "get_tenant", lambda _t: odoo)
        _encolar_descuadre(poller_source, "DESC-3")

        poller.procesar_lote()

        assert odoo.acciones == [], "no debia cancelar nada estando desactivado"

    def test_no_cancela_cuando_el_fallo_es_de_mapeo(self, entorno, monkeypatch):
        """
        Si falla el mapeo NO se creo nada en Odoo: no hay nada que cancelar y
        cancelar algo seria tocar un registro ajeno.
        """
        poller, poller_source, _ = entorno
        monkeypatch.setenv("POLLER_CANCELAR_DESCUADRE", "true")
        odoo = _odoo(existe_partner=False)   # el cliente no existe -> MapeoError
        odoo.acciones = []
        monkeypatch.setattr(poller, "get_tenant", lambda _t: odoo)
        with poller_source.get_source_session() as s:
            s.add(poller_source.ColaSincronizacion(
                entidad="factura", id_origen="SIN-CLIENTE",
                payload={"factura_id": "SIN-CLIENTE", "cliente_nif": "NADIE",
                         "fecha": "2026-08-21", "total": 10.0, "lineas": []},
                estado="PENDIENTE"))
            s.commit()

        res = poller.procesar_lote()

        assert res.con_error == 1
        llamadas = [c.args[1] for c in odoo.execute.call_args_list if len(c.args) > 1]
        assert "button_cancel" not in llamadas


class TestDespachoPorEntidad:
    """
    inventario y asientos NO viven en mappings.yaml (tienen esquema fijo), asi
    que sincronizar_entidad los rechaza con "sin mapeo definido". Sin un
    despacho explicito, el modo pull solo admitiria facturas y pagos.
    """

    def test_despacha_un_ajuste_de_stock(self, entorno, monkeypatch):
        poller, poller_source, _ = entorno
        llamadas = []
        monkeypatch.setattr(poller, "get_tenant", lambda _t: MagicMock())
        monkeypatch.setattr(
            poller, "ajustar_stock",
            lambda reg, odoo: llamadas.append(reg) or {"quant_id": 42},
        )
        with poller_source.get_source_session() as s:
            s.add(poller_source.ColaSincronizacion(
                entidad="ajuste_stock", id_origen="AJ-1",
                payload={"ajuste_id": "AJ-1", "producto_ref": "X", "cantidad": 5},
                estado="PENDIENTE"))
            s.commit()

        res = poller.procesar_lote()

        assert res.procesadas == 1 and res.con_error == 0
        assert llamadas, "no se llamo a ajustar_stock"

    def test_despacha_un_asiento(self, entorno, monkeypatch):
        poller, poller_source, _ = entorno
        llamadas = []
        monkeypatch.setattr(poller, "get_tenant", lambda _t: MagicMock())
        monkeypatch.setattr(
            poller, "crear_asiento",
            lambda reg, odoo: llamadas.append(reg) or {"id_odoo": 77},
        )
        with poller_source.get_source_session() as s:
            s.add(poller_source.ColaSincronizacion(
                entidad="asiento", id_origen="AS-1",
                payload={"asiento_id": "AS-1", "diario_codigo": "MISC", "lineas": []},
                estado="PENDIENTE"))
            s.commit()

        res = poller.procesar_lote()

        assert res.procesadas == 1 and res.con_error == 0
        assert llamadas, "no se llamo a crear_asiento"

    def test_un_error_de_inventario_aisla_la_fila(self, entorno, monkeypatch):
        """Stock insuficiente no debe abortar el lote entero."""
        from core.inventario import InventarioError
        poller, poller_source, _ = entorno
        monkeypatch.setattr(poller, "get_tenant", lambda _t: MagicMock())

        def falla(reg, odoo):
            raise InventarioError("Stock insuficiente")
        monkeypatch.setattr(poller, "ajustar_stock", falla)

        with poller_source.get_source_session() as s:
            s.add(poller_source.ColaSincronizacion(
                entidad="ajuste_stock", id_origen="AJ-MALO",
                payload={"ajuste_id": "AJ-MALO"}, estado="PENDIENTE"))
            s.commit()

        res = poller.procesar_lote()

        assert res.con_error == 1
        with poller_source.get_source_session() as s:
            fila = s.query(poller_source.ColaSincronizacion).filter_by(
                id_origen="AJ-MALO").one()
        assert fila.estado == "ERROR"
        assert "Stock insuficiente" in (fila.error_detalle or "")


class TestPasadasSimultaneas:
    """
    Dos pasadas del poller a la vez (p.ej. pulsar "Poller ahora" en el panel
    mientras Celery Beat dispara la suya) competian por las mismas filas y la
    perdedora salia con ReservaOcupada sin capturar -> HTTP 500 en la interfaz.
    No es un error: la fila la termina el otro proceso.
    """

    def test_una_reserva_ocupada_no_cuenta_como_error(self, entorno, monkeypatch):
        poller, poller_source, state_store = entorno

        def ocupada(entidad, payload, odoo):
            raise state_store.ReservaOcupada(entidad, "X", "PROCESANDO")

        monkeypatch.setattr(poller, "get_tenant", lambda _t: MagicMock())
        monkeypatch.setattr(poller, "sincronizar_entidad", ocupada)
        with poller_source.get_source_session() as s:
            s.add(poller_source.ColaSincronizacion(
                entidad="factura", id_origen="OCUPADA-1",
                payload={"factura_id": "OCUPADA-1"}, estado="PENDIENTE"))
            s.commit()

        res = poller.procesar_lote()

        assert res.con_error == 0, "una colision no es un fallo de datos"
        assert res.omitidas == 1

    def test_la_fila_ocupada_se_deja_pendiente(self, entorno, monkeypatch):
        """No se marca resultado: la termina quien la reservo."""
        poller, poller_source, state_store = entorno

        def ocupada(entidad, payload, odoo):
            raise state_store.ReservaOcupada(entidad, "Y", "PROCESANDO")

        monkeypatch.setattr(poller, "get_tenant", lambda _t: MagicMock())
        monkeypatch.setattr(poller, "sincronizar_entidad", ocupada)
        with poller_source.get_source_session() as s:
            s.add(poller_source.ColaSincronizacion(
                entidad="factura", id_origen="OCUPADA-2",
                payload={"factura_id": "OCUPADA-2"}, estado="PENDIENTE"))
            s.commit()

        poller.procesar_lote()

        with poller_source.get_source_session() as s:
            fila = s.query(poller_source.ColaSincronizacion).filter_by(
                id_origen="OCUPADA-2").one()
        assert fila.estado == "PENDIENTE"
        assert fila.error_detalle is None, "no debe marcarse como error"
