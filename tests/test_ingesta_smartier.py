"""
Tests de la ingesta desde Smartier.

Cubren la traduccion de una nota de entrega al formato de mappings.yaml, el
filtrado por estado, el antiduplicado al encolar y el manejo del rate limit del
cliente HTTP. La API externa se simula: no se llama a Smartier de verdad.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest

# --- Datos de ejemplo con la forma REAL de la API (verificada contra Smartier)


def _cliente(con_rif=True):
    return {
        "Id": 8,
        "Nombre": "LISBETH SANCHEZ",
        "Tipo": "Contacto",
        "Estado": "Habilitado",
        # Tipo 30 = RIF Venezolano (el 6 es generico y no cuenta como RIF).
        "Documento": {"Tipo": 30, "Contenido": "J-12345678-9" if con_rif else None},
        "RazonSocial": "TURICOPY IMPRESOS C.A." if con_rif else None,
    }


def _producto():
    return {
        "Id": 348, "Nombre": "Fondo Negro", "Estado": "Disponible",
        "Tipo": "Simple", "PorcentajeIVA": 16, "Exento": False,
    }


def _nota(estado="Facturado", con_rif=True, nota_id=9001):
    return {
        "Id": nota_id,
        "Estado": estado,
        "Tipo": "Entrega",
        "Cantidad": 10,
        "Descuento": 5.0,
        "FechaEntregaReal": "2026-08-21T10:30:00",
        "PrecioUnitario": {"Moneda": "Nacional", "Monto": 250.0},
        "Orden": {
            "Id": 501, "Numero": "ORD-501",
            "Cliente": _cliente(con_rif), "Producto": _producto(),
        },
    }


class TestTraduccion:
    """Smartier -> registro del middleware (formato de mappings.yaml)."""

    def test_nota_completa_produce_registro_valido(self):
        from core.ingesta_smartier import nota_a_registro

        r = nota_a_registro(_nota())

        assert r["factura_id"] == "NE-9001"
        assert r["cliente_nif"] == "J-12345678-9"
        assert r["fecha"] == "2026-08-21"
        assert r["moneda_iso"] == "VES"          # Nacional -> VES
        assert "ORD-501" in r["referencia"]

        linea = r["lineas"][0][2]
        assert linea["name"] == "Fondo Negro"
        assert linea["quantity"] == 10
        assert linea["price_unit"] == 250.0
        assert linea["discount"] == 5.0

    def test_moneda_extranjera_se_traduce_a_usd(self):
        from core.ingesta_smartier import nota_a_registro

        nota = _nota()
        nota["PrecioUnitario"]["Moneda"] = "Extranjera"
        assert nota_a_registro(nota)["moneda_iso"] == "USD"

    def test_sin_moneda_no_se_fuerza_ninguna(self):
        """Sin moneda, Odoo emite en la de la compania: no se inventa una."""
        from core.ingesta_smartier import nota_a_registro

        nota = _nota()
        nota["PrecioUnitario"]["Moneda"] = None
        assert "moneda_iso" not in nota_a_registro(nota)

    def test_cliente_sin_rif_deja_el_nif_vacio(self):
        """
        Los clientes de Smartier vienen hoy con Documento.Contenido=null. La
        nota se traduce igualmente (con nif None) para que el fallo sea visible
        en el panel, en vez de descartarla en silencio.
        """
        from core.ingesta_smartier import nota_a_registro

        assert nota_a_registro(_nota(con_rif=False))["cliente_nif"] is None

    def test_conserva_metadatos_de_origen(self):
        """El payload guarda el IVA y los ids de Smartier para trazabilidad."""
        from core.ingesta_smartier import nota_a_registro

        meta = nota_a_registro(_nota())["_smartier"]
        assert meta["nota_id"] == 9001
        assert meta["cliente_id"] == 8
        assert meta["producto_iva"] == 16
        assert meta["estado"] == "Facturado"


class TestIngesta:
    """Pasada completa: leer de Smartier y encolar."""

    @pytest.fixture()
    def entorno(self, tmp_path, monkeypatch):
        """Cola y state store aislados en SQLite temporal."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
        monkeypatch.setenv("SOURCE_DATABASE_URL", f"sqlite:///{tmp_path / 'cola.db'}")
        monkeypatch.setenv("SMARTIER_BASE_URL", "https://falso.test/api/v2")
        monkeypatch.setenv("SMARTIER_API_KEY", "clave-de-prueba")
        # Fijado aqui a proposito: sin esto el test lee SMARTIER_ESTADOS_FACTURAR
        # del .env de la maquina y pasa o falla segun contra que instancia este
        # apuntando quien lo ejecute.
        monkeypatch.setenv("SMARTIER_ESTADOS_FACTURAR", "Facturado")

        import core.state_store as state_store
        importlib.reload(state_store)
        state_store.init_db()

        import core.poller_source as ps
        importlib.reload(ps)
        ps.init_source_db()

        import core.smartier_client as sc
        importlib.reload(sc)
        import core.ingesta_smartier as ing
        importlib.reload(ing)
        return ing, ps

    def _cliente_falso(self, notas):
        """SmartierClient simulado que devuelve las notas indicadas."""
        cli = MagicMock()
        cli.paginar.return_value = iter(notas)
        return cli

    def test_encola_las_notas_facturadas(self, entorno):
        ing, ps = entorno
        cli = self._cliente_falso([_nota(nota_id=1), _nota(nota_id=2)])

        r = ing.ingerir_notas_entrega(cliente=cli)

        assert r.leidas == 2
        assert r.encoladas == 2
        with ps.get_source_session() as s:
            filas = s.query(ps.ColaSincronizacion).all()
            assert {f.id_origen for f in filas} == {"NE-1", "NE-2"}
            assert all(f.estado == "PENDIENTE" for f in filas)
            assert all(f.entidad == "factura" for f in filas)

    def test_omite_las_notas_en_otro_estado(self, entorno):
        """Solo se factura lo que Smartier marca como Facturado."""
        ing, cli_ps = entorno
        cli = self._cliente_falso([
            _nota(estado="Pendiente", nota_id=1),
            _nota(estado="Facturado", nota_id=2),
            _nota(estado="EnTransito", nota_id=3),
        ])

        r = ing.ingerir_notas_entrega(cliente=cli)

        assert r.leidas == 3
        assert r.encoladas == 1

    def test_no_duplica_una_nota_ya_encolada(self, entorno):
        """Reejecutar la ingesta no vuelve a encolar lo mismo."""
        ing, ps = entorno

        ing.ingerir_notas_entrega(cliente=self._cliente_falso([_nota(nota_id=1)]))
        r2 = ing.ingerir_notas_entrega(cliente=self._cliente_falso([_nota(nota_id=1)]))

        assert r2.encoladas == 0
        assert r2.omitidas == 1
        with ps.get_source_session() as s:
            assert s.query(ps.ColaSincronizacion).count() == 1

    def test_guarda_la_marca_de_agua(self, entorno):
        """Tras la pasada queda registrado hasta que fecha se leyo."""
        ing, _ = entorno
        r = ing.ingerir_notas_entrega(cliente=self._cliente_falso([_nota()]))
        assert r.marca_agua is not None
        assert ing._leer_marca(ing.RUTA_NOTAS) == r.marca_agua

    def test_sin_cola_configurada_falla_con_mensaje_claro(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMARTIER_BASE_URL", "https://falso.test/api/v2")
        monkeypatch.setenv("SMARTIER_API_KEY", "clave")
        import core.poller_source as ps
        monkeypatch.setattr(ps, "SOURCE_DATABASE_URL", "")

        import core.ingesta_smartier as ing
        with pytest.raises(ing.IngestaError, match="cola de destino"):
            ing.ingerir_notas_entrega(cliente=MagicMock())


class TestClienteHttp:
    """Comportamiento del cliente HTTP frente a la API."""

    def test_extrae_el_sobre_data_count(self):
        """La API real responde {"Data": [...], "Count": N}."""
        from core.smartier_client import SmartierClient

        filas, total = SmartierClient._extraer_datos(
            {"Data": [{"Id": 1}, {"Id": 2}], "Count": 7}
        )
        assert len(filas) == 2
        assert total == 7

    def test_reintenta_ante_429(self, monkeypatch):
        """Un 429 se reintenta respetando Retry-After, sin propagar el error."""
        monkeypatch.setenv("SMARTIER_BASE_URL", "https://falso.test/api/v2")
        monkeypatch.setenv("SMARTIER_API_KEY", "clave")
        import core.smartier_client as sc
        importlib.reload(sc)

        limitada = MagicMock(status_code=429, headers={"Retry-After": "0"}, ok=False)
        buena = MagicMock(status_code=200, headers={}, ok=True)
        buena.json.return_value = {"Data": [], "Count": 0}

        cli = sc.SmartierClient()
        with patch.object(cli._sesion, "get", side_effect=[limitada, buena]):
            with patch("time.sleep"):
                assert cli.get("/external/notas-entrega") == {"Data": [], "Count": 0}

    def test_401_no_se_reintenta_y_avisa_de_la_key(self, monkeypatch):
        monkeypatch.setenv("SMARTIER_BASE_URL", "https://falso.test/api/v2")
        monkeypatch.setenv("SMARTIER_API_KEY", "clave")
        import core.smartier_client as sc
        importlib.reload(sc)

        r401 = MagicMock(status_code=401, headers={}, ok=False, text="")
        cli = sc.SmartierClient()
        with patch.object(cli._sesion, "get", return_value=r401) as mock_get:
            with pytest.raises(sc.SmartierError, match="API Key"):
                cli.get("/external/clientes")
            assert mock_get.call_count == 1  # no reintenta

    def test_page_size_se_limita_a_200(self, monkeypatch):
        """La API rechaza PageSize > 200; el cliente lo recorta."""
        monkeypatch.setenv("SMARTIER_BASE_URL", "https://falso.test/api/v2")
        monkeypatch.setenv("SMARTIER_API_KEY", "clave")
        import core.smartier_client as sc
        importlib.reload(sc)

        cli = sc.SmartierClient()
        with patch.object(cli, "get", return_value={"Data": [], "Count": 0}) as mock:
            cli.listar("/external/notas-entrega", page_size=500)
            assert mock.call_args[0][1]["PageSize"] == 200


class TestFiltroDeFecha:
    """
    No se envia un filtro de fecha inventado.

    Se enviaba "FechaDesde", un nombre supuesto. Comprobado contra la API real
    (2026-09-09): no existe. Y Smartier confirmo que un filtro no reconocido se
    DESCARTA en silencio -responde 200 con el listado completo-, asi que el
    error no se veia: la pasada se traia el historico entero creyendo acotar.
    """

    def test_sin_configurar_no_se_envia_filtro_de_fecha(self, monkeypatch):
        import importlib
        import core.ingesta_smartier as ing
        monkeypatch.delenv("SMARTIER_FILTRO_FECHA", raising=False)
        importlib.reload(ing)
        assert ing.FILTRO_FECHA == ""

    def test_el_nombre_se_configura_por_entorno(self, monkeypatch):
        # Cuando Smartier confirme el nombre real, se activa sin tocar codigo.
        import importlib
        import core.ingesta_smartier as ing
        monkeypatch.setenv("SMARTIER_FILTRO_FECHA", "CreadoUtcDesde")
        importlib.reload(ing)
        assert ing.FILTRO_FECHA == "CreadoUtcDesde"
        monkeypatch.delenv("SMARTIER_FILTRO_FECHA")
        importlib.reload(ing)

    def test_no_queda_FechaDesde_escrito_en_el_codigo(self):
        # Fija el hallazgo: ese nombre concreto se probo y no existe.
        import inspect
        import core.ingesta_smartier as ing
        codigo = inspect.getsource(ing)
        cuerpo = codigo.split("FILTRO_FECHA = os.getenv")[1]
        assert '"FechaDesde"' not in cuerpo
