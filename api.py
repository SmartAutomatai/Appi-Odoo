"""
Middleware API-Odoo
Expone un endpoint universal para conectar agentes externos, Excel y scripts
con un servidor Odoo via JSON-RPC.
"""

import logging
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, field_validator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

# El .env se carga ANTES de importar los modulos del proyecto: core.state_store
# lee DATABASE_URL en tiempo de import (a nivel de modulo), asi que si se
# cargara despues se quedaria con el valor por defecto y el .env se ignoraria.
load_dotenv()

from odoo_universal import (  # noqa: E402
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
    register_tenant,
    get_tenant,
)
from core.state_store import buscar_mapeo, init_db  # noqa: E402
from core.seguridad import (  # noqa: E402
    LIMITE_LECTURA,
    LIMITE_NEGOCIO,
    error_interno,
    sanear_error_odoo,
    limiter,
    verify_api_key,
)

# ---------------------------------------------------------------------------
# Configuracion inicial
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("api-odoo")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

# Documentacion interactiva (/docs, /redoc, /openapi.json). Abierta revela toda
# la estructura de la API -esquemas, modelos, endpoints- a cualquier anonimo que
# llegue al puerto (auditoria, seccion 1). Se puede cerrar en produccion con
# EXPONER_DOCS=false sin tocar codigo; por defecto sigue abierta para no romper
# el flujo de desarrollo. Cerrada, esas rutas devuelven 404.
_exponer_docs = os.getenv("EXPONER_DOCS", "true").strip().lower() not in (
    "false", "0", "no",
)

app = FastAPI(
    title="API-Odoo Middleware",
    description="Middleware universal para conectar Odoo con agentes externos.",
    version="1.0.0",
    docs_url="/docs" if _exponer_docs else None,
    redoc_url="/redoc" if _exponer_docs else None,
    openapi_url="/openapi.json" if _exponer_docs else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Base de datos de control (state store). Crea las tablas si no existen.
init_db()
logger.info("Base de datos de control inicializada.")

# Modo pull: si hay SOURCE_DATABASE_URL, se provisiona la cola del cliente al
# arrancar (crea la tabla si falta, anade columnas nuevas si es antigua). En un
# despliegue donde la DB de origen la administre el cliente, create_all no falla
# si la tabla ya existe; si no hay permiso de DDL, se registra y se sigue.
if os.getenv("SOURCE_DATABASE_URL", ""):
    try:
        from core import poller_source  # noqa: E402
        poller_source.init_source_db()
        logger.info("Cola del poller (modo pull) inicializada.")
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo inicializar la cola del poller: %s", e)

# Routers de negocio (facturas, pagos). Endpoints con estado e idempotencia.
# panel: dashboard de observabilidad (lectura de sync_map/sync_log).
# asientos: crear/eliminar asientos contables (account.move tipo 'entry').
from routers import asientos, asistencia, conciliacion, facturas, inventario, pagos, panel, poller  # noqa: E402  (import tras crear la app)

app.include_router(facturas.router)
app.include_router(pagos.router)
app.include_router(conciliacion.router)
app.include_router(inventario.router)
app.include_router(asientos.router)
app.include_router(asistencia.router)  # /asistencias (fichajes hr.attendance)
app.include_router(poller.router)  # /poller/ejecutar (pasada manual, protegido)
app.include_router(panel.router)   # /panel (HTML)
app.include_router(panel.datos)    # /panel/api/* (JSON, protegido)

# ---------------------------------------------------------------------------
# Variables de entorno
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Modo abierto (sin API Key): permitido, pero nunca por accidente
# ---------------------------------------------------------------------------
#
# Sin API_KEY el servicio queda ENTERO sin autenticacion. Eso es comodo en
# desarrollo, pero antes ocurria en silencio: un .env mal desplegado o una
# variable que no llega al contenedor publicaba el middleware abierto y
# /health seguia diciendo "ok". El fallo iba en la direccion insegura.
#
# Ahora arrancar sin clave exige pedirlo a proposito con ENTORNO=desarrollo.
# Bajo pytest se permite sin mas: la suite prueba justamente ese modo.
# Ver auditoria H-4.

ENTORNO = os.getenv("ENTORNO", "produccion").strip().lower()
AUTENTICACION_ACTIVA = bool(os.getenv("API_KEY", ""))
_bajo_pytest = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules

if not AUTENTICACION_ACTIVA:
    if ENTORNO == "produccion" and not _bajo_pytest:
        raise RuntimeError(
            "API_KEY no configurada y ENTORNO=produccion. El servicio quedaria "
            "sin autenticacion. Define API_KEY, o arranca con ENTORNO=desarrollo "
            "si de verdad quieres el modo abierto."
        )
    logger.warning(
        "API_KEY no configurada - TODOS los endpoints estan SIN PROTEGER "
        "(ENTORNO=%s)", ENTORNO,
    )

_raw_models = os.getenv("ALLOWED_MODELS", "")
_raw_methods = os.getenv("ALLOWED_METHODS", "")

ALLOWED_MODELS: set[str] = {m.strip() for m in _raw_models.split(",") if m.strip()}
ALLOWED_METHODS: set[str] = {m.strip() for m in _raw_methods.split(",") if m.strip()}

# ---------------------------------------------------------------------------
# Conexion Odoo principal (tenant "default")
# ---------------------------------------------------------------------------

try:
    _default_odoo = OdooUniversalAPI(
        url=os.getenv("ODOO_URL", ""),
        db=os.getenv("ODOO_DB", ""),
        username=os.getenv("ODOO_USERNAME", ""),
        password=os.getenv("ODOO_PASSWORD", ""),
    )
    register_tenant("default", _default_odoo)
    logger.info("Conexion con Odoo establecida correctamente.")
except OdooConnectionError as e:
    logger.error("No se pudo conectar a Odoo al iniciar: %s", e)
    _default_odoo = None

# ---------------------------------------------------------------------------
# Seguridad: dependencia de API Key
# ---------------------------------------------------------------------------


# La verificacion vive en core/seguridad.py y se importa: tener dos copias de
# la misma comprobacion hacia que divergieran (esta usaba "!=", vulnerable a
# timing, y leia API_KEY congelada en el import). Ver auditoria H-1.


# ---------------------------------------------------------------------------
# Modelos de peticion / respuesta
# ---------------------------------------------------------------------------


class OdooRequest(BaseModel):
    model: str
    method: str
    args: list = []
    kwargs: dict = {}
    tenant: str = "default"

    # Los mensajes de rechazo NO enumeran la lista blanca: devolver
    # "Permitidos: [...]" en el 422 regalaba a cualquiera el mapa completo de
    # modelos/metodos accesibles -el mismo dato que se retiro de /health-. Se
    # dice que ESE modelo/metodo no esta permitido, sin listar el resto; quien
    # necesite la lista la consulta en /health/detalle (con API Key).
    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        if ALLOWED_MODELS and v not in ALLOWED_MODELS:
            raise ValueError(f"Modelo '{v}' no permitido.")
        return v

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if ALLOWED_METHODS and v not in ALLOWED_METHODS:
            raise ValueError(f"Metodo '{v}' no permitido.")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Sistema"])
@limiter.limit(LIMITE_LECTURA)
def health_check(request: Request):
    """
    Sonda de vida PUBLICA (sin API Key) para monitores y load balancers.

    Devuelve solo lo imprescindible para un chequeo de disponibilidad:
    si el servicio responde y si Odoo esta conectado. NO revela la lista
    blanca de modelos/metodos, la version de Odoo ni el estado de la
    autenticacion: un anonimo que enumera la API no debe obtener de aqui un
    mapa de la superficie interna. Ese detalle vive en /health/detalle, que
    exige X-Api-Key. Ver auditoria (hallazgo: fuga de informacion en /health).
    """
    odoo_ok = _default_odoo is not None and _default_odoo.uid is not None
    return {
        "status": "ok" if odoo_ok else "degradado",
        "odoo_conectado": odoo_ok,
    }


@app.get(
    "/health/detalle",
    tags=["Sistema"],
    dependencies=[Depends(verify_api_key)],
)
@limiter.limit(LIMITE_LECTURA)
def health_detalle(request: Request):
    """
    Detalle de salud PROTEGIDO (requiere X-Api-Key).

    Reune la informacion operativa que antes filtraba /health en abierto:
    version de Odoo, estado de la autenticacion y las listas blancas de
    modelos y metodos. Util para diagnostico interno sin exponer la
    arquitectura a cualquiera que llegue al puerto.
    """
    odoo_ok = _default_odoo is not None and _default_odoo.uid is not None
    return {
        "status": "ok" if odoo_ok else "degradado",
        "odoo_conectado": odoo_ok,
        # Version del servidor Odoo detectada en el arranque. Util para verificar
        # de un vistazo contra que version se esta hablando (17/18/19).
        "odoo_version": getattr(_default_odoo, "version", None) if odoo_ok else None,
        # Visible para monitorizacion: si esto dice DESACTIVADA en un despliegue
        # real, el servicio esta abierto a cualquiera. Ver auditoria H-4.
        "autenticacion": "activa" if os.getenv("API_KEY", "") else "DESACTIVADA",
        "modelos_permitidos": sorted(ALLOWED_MODELS) if ALLOWED_MODELS else "todos",
        "metodos_permitidos": sorted(ALLOWED_METHODS) if ALLOWED_METHODS else "todos",
    }


@app.get(
    "/estado/{entidad}/{id_origen}",
    tags=["Sistema"],
    dependencies=[Depends(verify_api_key)],
)
@limiter.limit(LIMITE_LECTURA)
def estado_sincronizacion(request: Request, entidad: str, id_origen: str):
    """
    Consulta el estado de sincronizacion de un registro de origen en la base de
    datos de control (state store). Util para seguimiento e idempotencia.

    Devuelve 404 si el registro nunca se ha intentado sincronizar.
    """
    mapa = buscar_mapeo(entidad, id_origen)
    if mapa is None:
        raise HTTPException(
            status_code=404,
            detail=f"Sin registro de sincronizacion para {entidad}/{id_origen}.",
        )
    return {
        "entidad": mapa.entidad,
        "id_origen": mapa.id_origen,
        "model_odoo": mapa.model_odoo,
        "id_odoo": mapa.id_odoo,
        "estado": mapa.estado,
        "error": mapa.error,
        "actualizado": mapa.actualizado.isoformat() if mapa.actualizado else None,
    }


@app.post("/odoo", tags=["Odoo"], dependencies=[Depends(verify_api_key)])
@limiter.limit(LIMITE_NEGOCIO)
def odoo_proxy(req: OdooRequest, request: Request):
    """
    Endpoint universal para ejecutar operaciones sobre modelos de Odoo.

    Requiere cabecera: X-Api-Key: <tu_clave>

    Ejemplo de peticion:
    ```json
    {
      "model": "account.move",
      "method": "search_read",
      "args": [[["state", "=", "posted"]]],
      "kwargs": {"fields": ["name", "amount_total"], "limit": 5}
    }
    ```
    """
    inicio = time.time()
    ip = request.client.host if request.client else "desconocida"

    logger.info(
        "PETICION | ip=%s tenant=%s model=%s method=%s",
        ip, req.tenant, req.model, req.method,
    )

    try:
        odoo = get_tenant(req.tenant)
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"Tenant '{req.tenant}' no configurado.",
        )

    try:
        result = odoo.execute(req.model, req.method, *req.args, **req.kwargs)
    except OdooConnectionError as e:
        logger.error("ERROR_CONEXION | model=%s method=%s error=%s", req.model, req.method, e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
    except OdooExecutionError as e:
        logger.warning("ERROR_ODOO | model=%s method=%s error=%s", req.model, req.method, e)
        # El texto se sanea: los errores de negocio de Odoo pasan intactos,
        # pero los que vienen de PostgreSQL llevan SQL y nombres de tablas
        # dentro y no deben salir del servidor. Ver auditoria H-5b.
        raise HTTPException(
            status_code=422, detail=f"Error de Odoo: {sanear_error_odoo(e)}"
        )
    except Exception as e:
        # El texto de la excepcion NO vuelve al cliente: puede llevar rutas,
        # nombres de tablas o cadenas de conexion. Ver auditoria H-5.
        raise error_interno(e, f"/odoo model={req.model} method={req.method}")

    duracion = round((time.time() - inicio) * 1000)
    logger.info(
        "OK | ip=%s model=%s method=%s duracion_ms=%d",
        ip, req.model, req.method, duracion,
    )
    return {"result": result}
