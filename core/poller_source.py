"""
Acceso a la base de datos de ORIGEN del cliente (modo polling).

En este modo el cliente NO llama a la API del middleware: deja sus registros en
SU propia base de datos (p.ej. un RDS PostgreSQL de su cuenta AWS) y el
middleware la sondea. El cliente es dueno de sus datos; a este modulo solo se le
concede lectura sobre la cola y escritura sobre las columnas de resultado
(estado / error / procesado_en).

Contrato de tabla acordado con el cliente (una sola tabla generica):

    CREATE TABLE cola_sincronizacion (
        id             BIGSERIAL PRIMARY KEY,
        entidad        VARCHAR   NOT NULL,     -- clave en mappings.yaml (factura, pago...)
        id_origen      VARCHAR   NOT NULL,     -- id de negocio (idempotencia)
        payload        JSONB     NOT NULL,     -- registro en el formato de mappings.yaml
        estado         VARCHAR   NOT NULL DEFAULT 'PENDIENTE',  -- PENDIENTE|PROCESADO|ERROR
        error_detalle  TEXT,
        creado_en      TIMESTAMP NOT NULL DEFAULT now(),
        procesado_en   TIMESTAMP
    );

La verdad de la idempotencia sigue siendo el sync_map del middleware; esta tabla
es solo la BANDEJA DE ENTRADA (que hay que enviar) y el ESPEJO DEL RESULTADO
(que el cliente lee sin llamarnos). Un registro en 'PENDIENTE' aqui pero ya
PROCESADO en sync_map no se duplica: sincronizar_entidad lo detecta.

La conexion se toma de SOURCE_DATABASE_URL (independiente de DATABASE_URL, que
es la base de CONTROL del middleware).
"""

import logging
import os
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# JSON portable: JSONB en PostgreSQL, JSON/TEXT en SQLite (para tests).
from sqlalchemy.types import JSON

from core.models_db import _ahora

logger = logging.getLogger("api-odoo")

# Base propia: estas tablas viven en la DB del CLIENTE, no en la de control.
SourceBase = declarative_base()


class ColaSincronizacion(SourceBase):
    """
    Fila de la bandeja de entrada del cliente. El middleware la lee y escribe de
    vuelta el resultado (estado/error/procesado_en) para que el cliente lo vea.
    """
    __tablename__ = "cola_sincronizacion"

    # BIGINT en PostgreSQL; INTEGER (autoincrement por ROWID) en SQLite (tests).
    id = Column(
        Integer().with_variant(BigInteger, "postgresql"),
        primary_key=True,
        autoincrement=True,
    )
    entidad = Column(String(50), nullable=False)        # clave en mappings.yaml
    id_origen = Column(String(100), nullable=False)      # id de negocio (idempotencia)
    payload = Column(JSON, nullable=False)               # registro para el mapper
    # Respuesta ORIGINAL del sistema de origen, tal cual llego, sin traducir.
    # 'payload' guarda solo lo que el mapper necesita; si el origen anade manana
    # un campo que hoy ignoramos, no quedaria rastro. Nullable a proposito: las
    # filas encoladas a mano (pruebas, o el cliente escribiendo directo) no
    # tienen original que guardar y no deben fallar por ello.
    payload_original = Column(JSON, nullable=True)
    estado = Column(String(20), nullable=False, default="PENDIENTE")
    # Resultado en Odoo, escrito de vuelta al procesar. id_odoo es la llave
    # tecnica; numero_odoo es el numero fiscal ("INV/2026/00001"), por el que
    # pregunta cualquiera que reclame una factura. Se guardan AQUI, en la base
    # de origen, para que el cliente cruce su registro con la factura sin entrar
    # a Odoo ni consultar la base de control del middleware.
    id_odoo = Column(Integer, nullable=True)
    numero_odoo = Column(String(64), nullable=True)
    error_detalle = Column(Text, nullable=True)
    creado_en = Column(DateTime(timezone=True), default=_ahora, nullable=False)
    procesado_en = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ColaSincronizacion {self.entidad}/{self.id_origen} "
            f"[{self.estado}] #{self.id}>"
        )


# ---------------------------------------------------------------------------
# Engine / Session (separado del engine de la base de CONTROL)
# ---------------------------------------------------------------------------

SOURCE_DATABASE_URL = os.getenv("SOURCE_DATABASE_URL", "")

# El engine se crea de forma perezosa: si no hay SOURCE_DATABASE_URL, el modo
# polling esta desactivado y no se toca ninguna conexion.
_engine = None
_SourceSession = None


def _init_engine():
    """Inicializa (una vez) el engine hacia la DB del cliente. Idempotente."""
    global _engine, _SourceSession
    if _engine is not None:
        return
    if not SOURCE_DATABASE_URL:
        raise RuntimeError(
            "Modo polling no configurado: define SOURCE_DATABASE_URL con la "
            "cadena de conexion a la base de datos del cliente."
        )
    connect_args = (
        {"check_same_thread": False}
        if SOURCE_DATABASE_URL.startswith("sqlite")
        else {}
    )
    _engine = create_engine(SOURCE_DATABASE_URL, connect_args=connect_args, future=True)
    _SourceSession = sessionmaker(
        bind=_engine, autoflush=False, expire_on_commit=False, future=True
    )


def polling_habilitado() -> bool:
    """True si hay una DB de origen configurada (modo polling activo)."""
    return bool(SOURCE_DATABASE_URL)


@contextmanager
def get_source_session() -> Session:
    """Context manager transaccional hacia la DB del cliente."""
    _init_engine()
    session = _SourceSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_source_db() -> None:
    """
    Crea la tabla de cola si no existe. Util para tests y para el arranque
    inicial en entornos donde el middleware provisiona el esquema; en produccion
    la tabla suele crearla el cliente.
    """
    _init_engine()
    SourceBase.metadata.create_all(bind=_engine)
    _migrar_columnas_resultado()


# Columnas anadidas despues de la version inicial de la tabla, con su tipo por
# dialecto: (nombre, tipo PostgreSQL, tipo SQLite).
_COLUMNAS_NUEVAS = (
    # SQLite no tiene JSON nativo, pero acepta el mismo contenido en TEXT y
    # SQLAlchemy lo serializa igual.
    ("payload_original", "JSON", "TEXT"),
    ("id_odoo", "INTEGER", "INTEGER"),
    ("numero_odoo", "VARCHAR(64)", "VARCHAR(64)"),
)


def _migrar_columnas_resultado() -> None:
    """
    Anade a una cola ya existente las columnas incorporadas despues.

    create_all() crea tablas que faltan, pero NUNCA altera una existente: en una
    base ya en marcha la columna nueva no aparece y todo insert fallaria. Se hace
    aqui, y no con Alembic, porque son columnas nullable y anadir una herramienta
    de migraciones por esto seria desproporcionado. Si el esquema acumula cambios
    mas complejos (renombrar, cambiar tipos, migrar datos), este es el punto
    donde toca adoptar migraciones de verdad.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(_engine)
    if not inspector.has_table("cola_sincronizacion"):
        return
    existentes = {c["name"] for c in inspector.get_columns("cola_sincronizacion")}
    es_postgres = _engine.dialect.name == "postgresql"

    for nombre, tipo_pg, tipo_sqlite in _COLUMNAS_NUEVAS:
        if nombre in existentes:
            continue
        tipo = tipo_pg if es_postgres else tipo_sqlite
        with _engine.begin() as con:
            con.execute(text(
                f"ALTER TABLE cola_sincronizacion ADD COLUMN {nombre} {tipo}"
            ))
        logger.info("Cola: anadida la columna %s.", nombre)


# ---------------------------------------------------------------------------
# Lectura del lote pendiente
# ---------------------------------------------------------------------------

# SKIP LOCKED evita que dos workers cojan la misma fila; solo lo soportan
# PostgreSQL/MySQL. En SQLite (tests) se degrada a un SELECT normal.
def _soporta_skip_locked() -> bool:
    return not SOURCE_DATABASE_URL.startswith("sqlite")


def tomar_lote(limite: int = 50) -> list[dict]:
    """
    Lee hasta 'limite' filas PENDIENTE y las bloquea con FOR UPDATE SKIP LOCKED
    (donde el motor lo soporte), devolviendo una copia desacoplada de la sesion.

    Devuelve una lista de dicts {id, entidad, id_origen, payload} para procesar
    fuera de la transaccion de bloqueo. El resultado se escribe luego con
    marcar_resultado().
    """
    with get_source_session() as session:
        consulta = (
            session.query(ColaSincronizacion)
            .filter(ColaSincronizacion.estado == "PENDIENTE")
            .order_by(ColaSincronizacion.id)
            .limit(limite)
        )
        if _soporta_skip_locked():
            consulta = consulta.with_for_update(skip_locked=True)

        filas = consulta.all()
        # Copiamos los datos antes de cerrar la sesion (expire_on_commit=False
        # ya evita el detach, pero devolver dicts desacopla al llamador de la ORM).
        return [
            {
                "id": f.id,
                "entidad": f.entidad,
                "id_origen": f.id_origen,
                "payload": f.payload,
            }
            for f in filas
        ]


def marcar_resultado(
    fila_id: int,
    estado: str,
    error_detalle: Optional[str] = None,
    id_odoo: Optional[int] = None,
    numero_odoo: Optional[str] = None,
) -> None:
    """
    Escribe el resultado de una fila de vuelta en la DB del cliente: estado
    (PROCESADO|ERROR), error si aplica, la marca de tiempo de procesado y -si
    vienen- el id_odoo y el numero fiscal de la factura creada.
    Es el unico permiso de ESCRITURA que necesita el middleware en la DB origen.

    id_odoo / numero_odoo solo se escriben si vienen con valor: un fallo
    posterior al create (por ejemplo al postear) deja la fila en ERROR pero con
    el id ya asignado, y perderlo obligaria a buscar a mano que quedo a medias.
    """
    with get_source_session() as session:
        fila = session.get(ColaSincronizacion, fila_id)
        if fila is None:
            # La fila desaparecio (el cliente la borro): nada que marcar.
            return
        fila.estado = estado
        if id_odoo is not None:
            fila.id_odoo = id_odoo
        if numero_odoo:
            fila.numero_odoo = numero_odoo
        fila.error_detalle = error_detalle if estado == "ERROR" else None
        fila.procesado_en = _ahora()
