"""
Router del POLLER: dispara una pasada bajo demanda (modo pull).

  POST /poller/ejecutar  -> lee un lote de la cola del cliente y lo sincroniza ya.

Complementa al schedule automatico de Celery Beat: util para forzar una pasada
desde el panel o para pruebas, sin esperar al siguiente tick. La logica es la
misma (core.poller.procesar_lote); aqui solo se envuelve con auth y el mapeo de
errores HTTP habitual.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.fibex_poller import procesar_asistencias, procesar_dia_anterior
from core.fibex_source import FibexSourceError
from core.fibex_source import polling_habilitado as fibex_habilitado
from core.poller import procesar_lote
from core.poller_source import polling_habilitado
from core.seguridad import (
    LIMITE_POLLER,
    error_interno,
    limiter,
    resolver_tenant,
    verify_api_key,
)
from odoo_universal import OdooConnectionError

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Poller"], dependencies=[Depends(verify_api_key)])


class EjecutarPollerRequest(BaseModel):
    """Parametros de una pasada manual del poller."""
    tenant: str = "default"
    limite: int = Field(50, ge=1, le=500, description="Maximo de filas a procesar.")


@router.post("/poller/ejecutar")
@limiter.limit(LIMITE_POLLER)
def ejecutar(req: EjecutarPollerRequest, request: Request):
    """
    Ejecuta una pasada del poller de forma sincrona y devuelve el resumen
    (leidas, procesadas, con_error).

    Requiere el modo pull configurado (SOURCE_DATABASE_URL); si no, responde 400.
    """
    if not polling_habilitado():
        raise HTTPException(
            status_code=400,
            detail=(
                "Modo pull no configurado: define SOURCE_DATABASE_URL con la "
                "base de datos del cliente."
            ),
        )
    # Valida el tenant (400 si no existe) antes de tocar la cola.
    resolver_tenant(req.tenant)
    try:
        resultado = procesar_lote(tenant=req.tenant, limite=req.limite)
        return asdict(resultado)
    except OdooConnectionError as e:
        logger.error("POLLER_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
    except Exception as e:
        # Red de seguridad: una pasada del poller recorre muchas filas y toca
        # varios modulos, asi que cualquier fallo inesperado saldria como un 500
        # desnudo ("Internal Server Error") sin decir que paso.
        #
        # La traza completa va al log; al cliente solo le llega una referencia
        # de incidencia, no el texto de la excepcion (auditoria H-5). El panel
        # muestra esa referencia, que basta para cruzarla con el log.
        raise error_interno(e, f"/poller/ejecutar tenant={req.tenant}")


class EjecutarFibexRequest(BaseModel):
    """Parametros de una pasada manual del poller de asistencia Fibex."""
    tenant: str = "default"
    desde: str | None = Field(
        None, description="Dia inicial AAAA-MM-DD. Sin desde/hasta: el dia anterior."
    )
    hasta: str | None = Field(
        None, description="Dia final AAAA-MM-DD (max 31 dias desde 'desde')."
    )


@router.post("/poller/asistencia")
@limiter.limit(LIMITE_POLLER)
def ejecutar_asistencia(req: EjecutarFibexRequest, request: Request):
    """
    Fuerza una pasada del poller de asistencia Fibex (modo pull) de forma
    sincrona y devuelve el resumen (leidas, creadas, idempotentes, con_error).

    Sin `desde`/`hasta` procesa el dia anterior (lo recomendado por Fibex). Con
    rango explicito, ese rango (para reprocesos). Requiere FIBEX_API_URL (400 si
    no); 503 si Odoo o la API de Fibex no responden (p.ej. 403 de lista blanca).
    """
    if not fibex_habilitado():
        raise HTTPException(
            status_code=400,
            detail=(
                "Modo pull de asistencia no configurado: define FIBEX_API_URL "
                "con la base de la API de Fibex."
            ),
        )
    resolver_tenant(req.tenant)
    try:
        if req.desde and req.hasta:
            resultado = procesar_asistencias(req.desde, req.hasta, tenant=req.tenant)
        else:
            resultado = procesar_dia_anterior(tenant=req.tenant)
        return asdict(resultado)
    except FibexSourceError as e:
        # Fallo de la API de Fibex (red, 403 de lista blanca de IP, rango
        # invalido). Es un 503: el middleware esta bien, la fuente no responde.
        logger.error("FIBEX_SOURCE | %s", e)
        raise HTTPException(status_code=503, detail=f"Error al consultar Fibex: {e}")
    except OdooConnectionError as e:
        logger.error("FIBEX_POLLER_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
    except Exception as e:
        raise error_interno(e, f"/poller/asistencia tenant={req.tenant}")
