"""
Router de ASISTENCIA biometrica: registra fichajes (hr.attendance).

  POST /asistencias   -> crea un fichaje, o completa la salida de uno abierto
                         (idempotente por marcaje_id)

A diferencia de facturas/pagos, el fichaje no pasa por el sincronizador generico:
tiene su propio flujo en core/asistencia.py (resuelve el empleado por un dato de
negocio -por defecto 'barcode'- y empareja entrada/salida por marcaje_id).

Ver docs/INFORME-ASISTENCIA-BIOMETRICA.md.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.asistencia import AsistenciaError, registrar_asistencia
from core.seguridad import LIMITE_NEGOCIO, limiter, resolver_tenant, verify_api_key
from core.state_store import ReservaOcupada
from odoo_universal import OdooConnectionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Asistencia"], dependencies=[Depends(verify_api_key)])


class AsistenciaRequest(BaseModel):
    """Registro de fichaje biometrico de la base de datos de origen."""
    registro: dict = Field(
        ...,
        description=(
            "Datos del marcaje: marcaje_id (idempotencia), empleado_codigo "
            "(p.ej. barcode), check_in (ISO 8601 UTC), check_out (opcional, "
            "puede llegar despues con el mismo marcaje_id), biometrico_id "
            "(dispositivo/sede, opcional)."
        ),
    )
    tenant: str = "default"


@router.post("/asistencias")
@limiter.limit(LIMITE_NEGOCIO)
def registrar(req: AsistenciaRequest, request: Request):
    """
    Registra un fichaje en Odoo (hr.attendance).

    Si el marcaje llega con `check_in` y `check_out`, crea el tramo cerrado. Si
    llega solo con `check_in`, crea el fichaje abierto; un segundo mensaje con el
    MISMO `marcaje_id` y el `check_out` completa la salida sin duplicar.
    Idempotente por `marcaje_id`: reenviar el mismo marcaje no lo crea dos veces.
    """
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)
    try:
        return registrar_asistencia(req.registro, odoo)
    except ReservaOcupada as e:
        # Otra peticion con el mismo marcaje_id esta en curso -> 409.
        logger.info("RESERVA_OCUPADA | %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except AsistenciaError as e:
        logger.warning("ASISTENCIA_ERROR | %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        logger.error("ASISTENCIA_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
