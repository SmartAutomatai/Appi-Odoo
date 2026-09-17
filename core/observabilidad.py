"""
Consultas de OBSERVABILIDAD sobre la base de datos de control (state store).

Capa de solo lectura para el panel de monitoreo: resume y lista los registros de
sync_map (estado de cada sincronizacion) y sync_log (bitacora de auditoria), con
filtros. NO modifica nada: es el reverso de state_store, pensado para observar.

Se apoya en la misma sesion/engine que state_store, asi que respeta DATABASE_URL.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from core.models_db import SyncLog, SyncMap
from core.state_store import get_session


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Serializa un datetime a ISO 8601 (o None)."""
    return dt.isoformat() if dt else None


def _map_a_dict(m: SyncMap) -> dict:
    """Convierte un SyncMap a dict serializable para el panel."""
    return {
        "id": m.id,
        "entidad": m.entidad,
        "id_origen": m.id_origen,
        "model_odoo": m.model_odoo,
        "id_odoo": m.id_odoo,
        "estado": m.estado,
        "error": m.error,
        "creado": _iso(m.creado),
        "actualizado": _iso(m.actualizado),
    }


def _log_a_dict(l: SyncLog) -> dict:
    """Convierte un SyncLog a dict serializable para el panel."""
    return {
        "id": l.id,
        "entidad": l.entidad,
        "id_origen": l.id_origen,
        "accion": l.accion,
        "resultado": l.resultado,
        "detalle": l.detalle,
        "timestamp": _iso(l.timestamp),
    }


def resumen() -> dict:
    """
    Devuelve un resumen agregado para las tarjetas del panel:
      - totales por estado (PROCESADO, ERROR, PROCESANDO, PENDIENTE)
      - totales por entidad
      - conteo de logs y de errores en la bitacora
    """
    with get_session() as session:
        por_estado = dict(
            session.query(SyncMap.estado, func.count(SyncMap.id))
            .group_by(SyncMap.estado)
            .all()
        )
        por_entidad = dict(
            session.query(SyncMap.entidad, func.count(SyncMap.id))
            .group_by(SyncMap.entidad)
            .all()
        )
        total_map = session.query(func.count(SyncMap.id)).scalar() or 0
        total_log = session.query(func.count(SyncLog.id)).scalar() or 0
        total_errores_log = (
            session.query(func.count(SyncLog.id))
            .filter(SyncLog.resultado == "ERROR")
            .scalar()
            or 0
        )

    return {
        "total": total_map,
        "por_estado": {
            "PROCESADO": por_estado.get("PROCESADO", 0),
            "ERROR": por_estado.get("ERROR", 0),
            "PROCESANDO": por_estado.get("PROCESANDO", 0),
            "PENDIENTE": por_estado.get("PENDIENTE", 0),
            "ELIMINADO": por_estado.get("ELIMINADO", 0),
        },
        "por_entidad": por_entidad,
        "logs": {"total": total_log, "errores": total_errores_log},
    }


def listar_sincronizaciones(
    estado: Optional[str] = None,
    entidad: Optional[str] = None,
    id_origen: Optional[str] = None,
    limite: int = 100,
    offset: int = 0,
) -> dict:
    """
    Lista los registros de sync_map, mas recientes primero, con filtros opcionales
    por estado, entidad e id_origen (coincidencia parcial). Devuelve {total, items}.
    """
    limite = max(1, min(limite, 500))
    with get_session() as session:
        q = session.query(SyncMap)
        if estado:
            q = q.filter(SyncMap.estado == estado)
        if entidad:
            q = q.filter(SyncMap.entidad == entidad)
        if id_origen:
            q = q.filter(SyncMap.id_origen.like(f"%{id_origen}%"))

        total = q.with_entities(func.count(SyncMap.id)).scalar() or 0
        items = (
            q.order_by(SyncMap.actualizado.desc())
            .offset(max(0, offset))
            .limit(limite)
            .all()
        )
    return {"total": total, "items": [_map_a_dict(m) for m in items]}


def listar_logs(
    entidad: Optional[str] = None,
    id_origen: Optional[str] = None,
    resultado: Optional[str] = None,
    limite: int = 100,
    offset: int = 0,
) -> dict:
    """
    Lista la bitacora sync_log, mas reciente primero, con filtros opcionales.
    Devuelve {total, items}.
    """
    limite = max(1, min(limite, 500))
    with get_session() as session:
        q = session.query(SyncLog)
        if entidad:
            q = q.filter(SyncLog.entidad == entidad)
        if id_origen:
            q = q.filter(SyncLog.id_origen.like(f"%{id_origen}%"))
        if resultado:
            q = q.filter(SyncLog.resultado == resultado)

        total = q.with_entities(func.count(SyncLog.id)).scalar() or 0
        items = (
            q.order_by(SyncLog.timestamp.desc())
            .offset(max(0, offset))
            .limit(limite)
            .all()
        )
    return {"total": total, "items": [_log_a_dict(l) for l in items]}


def detalle_registro(entidad: str, id_origen: str) -> dict:
    """
    Devuelve el mapeo de un registro concreto junto con toda su bitacora,
    para la vista de detalle del panel. {mapeo, logs}.
    """
    with get_session() as session:
        mapa = (
            session.query(SyncMap)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .one_or_none()
        )
        logs = (
            session.query(SyncLog)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .order_by(SyncLog.timestamp.asc())
            .all()
        )
    return {
        "mapeo": _map_a_dict(mapa) if mapa else None,
        "logs": [_log_a_dict(l) for l in logs],
    }


# ---------------------------------------------------------------------------
# Cola del POLLER (modo pull)
#
# Vive en la base de datos de ORIGEN del cliente, no en la de control, asi que
# se consulta por su propio engine (core.poller_source). Es lectura pura: el
# panel muestra la bandeja de entrada tal como la ve el cliente.
#
# La tabla cola_sincronizacion lleva id_odoo, numero_odoo y payload_original:
# el poller escribe de vuelta el id y el numero fiscal de la factura, y guarda
# el documento original del sistema de origen. init_source_db las anade a una
# cola antigua que no las tuviera (ver poller_source._migrar_columnas_resultado).
# ---------------------------------------------------------------------------


def cola_poller(limite: int = 100, estado: Optional[str] = None) -> dict:
    """
    Resume y lista la cola de sincronizacion del cliente.

    Devuelve {habilitado, totales, filas}. Si el modo pull no esta configurado
    (sin SOURCE_DATABASE_URL), devuelve habilitado=False sin tocar ninguna
    conexion, para que el panel lo indique en vez de fallar.
    """
    from core import poller_source

    if not poller_source.polling_habilitado():
        return {"habilitado": False, "totales": {}, "filas": []}

    try:
        with poller_source.get_source_session() as session:
            cola = poller_source.ColaSincronizacion

            totales = dict(
                session.query(cola.estado, func.count(cola.id))
                .group_by(cola.estado)
                .all()
            )

            consulta = session.query(cola)
            if estado:
                consulta = consulta.filter(cola.estado == estado.upper())
            filas = consulta.order_by(cola.id.desc()).limit(limite).all()

            # Los dicts se construyen DENTRO del with: al cerrarse la sesion los
            # objetos quedan detached y acceder a sus atributos fuera del bloque
            # es fragil (funciona en SQLite con expire_on_commit=False, pero no
            # hay que confiar en ello contra PostgreSQL).
            items = [
                {
                    "id": f.id,
                    "entidad": f.entidad,
                    "id_origen": f.id_origen,
                    "estado": f.estado,
                    "id_odoo": f.id_odoo,
                    "numero_odoo": f.numero_odoo,
                    "error_detalle": f.error_detalle,
                    "creado_en": _iso(f.creado_en),
                    "procesado_en": _iso(f.procesado_en),
                    # Solo se indica SI hay original, no su contenido: el listado
                    # devuelve hasta 100 filas y meter el documento entero en
                    # cada una haria la respuesta inmanejable. Ver detalle_cola.
                    "tiene_original": f.payload_original is not None,
                }
                for f in filas
            ]
    except OperationalError:
        # SOURCE_DATABASE_URL esta configurada pero la tabla aun no se ha
        # creado (init_source_db no se ha llamado): la cola esta vacia, no
        # es un fallo. El panel lo muestra como deshabilitada en vez de 500.
        return {"habilitado": False, "totales": {}, "filas": []}

    return {"habilitado": True, "totales": totales, "filas": items}


def detalle_cola(fila_id: int) -> dict:
    """
    Devuelve una fila de la cola con su payload y el ORIGINAL de origen.

    Es el unico sitio donde se consulta el documento tal como lo emitio el
    sistema de origen: el listado (cola_poller) solo dice si existe. Sirve para
    auditar una factura hacia atras cuando el dato traducido no basta.
    """
    from core import poller_source

    if not poller_source.polling_habilitado():
        return {"habilitado": False}

    try:
        with poller_source.get_source_session() as session:
            f = session.get(poller_source.ColaSincronizacion, fila_id)
            if f is None:
                return {"habilitado": True, "encontrado": False}
            return _fila_cola_a_dict(f)
    except OperationalError:
        return {"habilitado": False}


def _fila_cola_a_dict(f) -> dict:
    return {
        "habilitado": True,
        "encontrado": True,
        "id": f.id,
        "entidad": f.entidad,
        "id_origen": f.id_origen,
        "estado": f.estado,
        "id_odoo": f.id_odoo,
        "numero_odoo": f.numero_odoo,
        "error_detalle": f.error_detalle,
        "creado_en": _iso(f.creado_en),
        "procesado_en": _iso(f.procesado_en),
        "payload": f.payload,
        "payload_original": f.payload_original,
    }
