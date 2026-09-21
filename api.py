"""
Middleware API-Odoo
Expone un endpoint universal para conectar agentes externos, Excel y scripts
con un servidor Odoo via JSON-RPC.
"""

import hmac
import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from core.tenants import registrar_tenant_por_defecto
from odoo_universal import (
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
    register_tenant,
    get_tenant,
)

# ---------------------------------------------------------------------------
# Configuracion inicial
# ---------------------------------------------------------------------------

# load_dotenv() va ANTES de importar core.state_store: ese modulo lee
# DATABASE_URL al importarse y crea el engine con lo que encuentre. Si el .env
# no se ha cargado todavia, cae en el valor por defecto (control.db) y toda la
# aplicacion acaba escribiendo en una base distinta de la configurada.
load_dotenv()

from core.state_store import buscar_mapeo, init_db  # noqa: E402  (tras load_dotenv)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("api-odoo")

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

from core.limites import limiter  # limitador compartido con los routers

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


# La documentacion interactiva publica el esquema completo de la API: rutas,
# modelos y campos. Es comoda en desarrollo y un mapa regalado en produccion,
# asi que por defecto queda CERRADA y hay que pedirla con EXPONER_DOCS=true.
_DOCS_ABIERTOS = os.getenv("EXPONER_DOCS", "").strip().lower() == "true"

app = FastAPI(
    title="API-Odoo Middleware",
    description="Middleware universal para conectar Odoo con agentes externos.",
    version="1.0.0",
    docs_url="/docs" if _DOCS_ABIERTOS else None,
    redoc_url="/redoc" if _DOCS_ABIERTOS else None,
    openapi_url="/openapi.json" if _DOCS_ABIERTOS else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Sin este middleware, el default_limits del limitador NO se aplica: solo
# limitan los endpoints con decorador propio, y el resto (/facturas, /pagos,
# /panel/api/*, /health...) quedaba sin limite, con la API Key expuesta a
# fuerza bruta. Comprobado: con RATE_LIMIT_GLOBAL=5/minute, nueve peticiones
# seguidas a /health devolvian nueve 200.
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def _refrescar_whitelists(request: Request, call_next):
    """
    Relee las whitelists del entorno en cada peticion.

    Antes se cacheaban al importar el modulo: restringir la lista tras un
    incidente no surtia efecto hasta reiniciar el proceso, justo cuando mas
    urgente es que el cambio se aplique ya.
    """
    recargar_whitelists()
    return await call_next(request)

# Base de datos de control (state store). Crea las tablas si no existen.
init_db()
logger.info("Base de datos de control inicializada.")

# Routers de negocio (facturas, pagos). Endpoints con estado e idempotencia.
from core.seguridad import verify_api_key  # noqa: E402  (unica implementacion de auth)
from routers import conciliacion, facturas, inventario, pagos, panel, poller, smartier  # noqa: E402  (import tras crear la app)

app.include_router(facturas.router)
app.include_router(pagos.router)
app.include_router(conciliacion.router)
app.include_router(inventario.router)
app.include_router(poller.router)  # /poller/ejecutar (pasada manual, protegido)
app.include_router(smartier.router)  # /smartier/* (ingesta desde la API del cliente)
app.include_router(panel.router)   # /panel (HTML)
app.include_router(panel.datos)    # /panel/api/* (JSON, protegido)

# ---------------------------------------------------------------------------
# Variables de entorno
# ---------------------------------------------------------------------------

if not os.getenv("API_KEY", "").strip():
    if os.getenv("PERMITIR_SIN_API_KEY", "").strip().lower() == "true":
        logger.warning(
            "API_KEY no configurada y PERMITIR_SIN_API_KEY=true: "
            "el servicio acepta peticiones SIN autenticar (solo desarrollo)."
        )
    else:
        logger.error(
            "API_KEY no configurada: el servicio rechazara las peticiones con 503."
        )


def _lista_permitidos(variable: str) -> set[str]:
    """
    Lee una whitelist del entorno EN CADA LLAMADA.

    Antes se cacheaban al importar el modulo, asi que restringir la lista tras
    un incidente no surtia efecto hasta reiniciar el proceso -- justo cuando
    mas falta hace que el cambio sea inmediato.
    """
    return {x.strip() for x in os.getenv(variable, "").split(",") if x.strip()}


# Se conservan como nombres de modulo porque /health y los tests los consultan;
# la validacion real usa _lista_permitidos() para releer el entorno.
ALLOWED_MODELS: set[str] = _lista_permitidos("ALLOWED_MODELS")
ALLOWED_METHODS: set[str] = _lista_permitidos("ALLOWED_METHODS")

# ---------------------------------------------------------------------------
# Conexion Odoo principal (tenant "default")
# ---------------------------------------------------------------------------

# El registro vive en core/tenants.py y no aqui: el worker de Celery corre en
# su propio contenedor y no importa api.py, asi que necesitaba la misma logica
# sin arrastrar FastAPI. Un fallo de conexion deja _default_odoo a None y
# /health responde "degradado", en vez de impedir el arranque.
_default_odoo = registrar_tenant_por_defecto()

# ---------------------------------------------------------------------------
# Seguridad: dependencia de API Key
# ---------------------------------------------------------------------------


# verify_api_key vive en core/seguridad.py y se importa mas abajo, junto con los
# routers: una sola implementacion para toda la app evita que una copia quede
# desactualizada respecto a la otra (fue el caso: aqui habia una version que
# comparaba con != y dejaba pasar si API_KEY estaba vacia).


def _whitelist_vigente(variable: str, _por_defecto: set[str]) -> set[str]:
    """
    Whitelist a aplicar ahora mismo.

    Se lee del modulo, no del entorno, porque `recargar_whitelists()` mantiene
    ambos sincronizados y asi un monkeypatch en los tests sigue funcionando.
    El refresco desde el entorno lo hace el middleware HTTP en cada peticion,
    de modo que editar el .env surte efecto sin reiniciar el proceso.
    """
    import sys

    return getattr(sys.modules[__name__], variable, _por_defecto)


def recargar_whitelists() -> None:
    """Relee ALLOWED_MODELS / ALLOWED_METHODS del entorno."""
    global ALLOWED_MODELS, ALLOWED_METHODS
    ALLOWED_MODELS = _lista_permitidos("ALLOWED_MODELS")
    ALLOWED_METHODS = _lista_permitidos("ALLOWED_METHODS")


# ---------------------------------------------------------------------------
# Modelos de peticion / respuesta
# ---------------------------------------------------------------------------


class OdooRequest(BaseModel):
    model: str
    method: str
    args: list = []
    kwargs: dict = {}
    tenant: str = "default"

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        permitidos = _whitelist_vigente("ALLOWED_MODELS", ALLOWED_MODELS)
        if permitidos and v not in permitidos:
            raise ValueError(
                f"Modelo '{v}' no permitido. Permitidos: {sorted(permitidos)}"
            )
        return v

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        permitidos = _whitelist_vigente("ALLOWED_METHODS", ALLOWED_METHODS)
        if permitidos and v not in permitidos:
            raise ValueError(
                f"Metodo '{v}' no permitido. Permitidos: {sorted(permitidos)}"
            )
        return v


def _detalle_health_permitido(request: Request) -> bool:
    """
    Decide si /health puede incluir las whitelists.

    Se publican solo a quien ya esta autenticado. La cabecera se lee a mano
    porque /health no puede depender de verify_api_key: un monitor tiene que
    recibir 200 sin clave, no un 401.
    """
    clave = os.getenv("API_KEY", "").strip()
    if not clave:
        # Sin API_KEY el servicio ya esta abierto (modo desarrollo): ocultar la
        # lista no aporta nada y estorba para depurar.
        return True
    enviada = (request.headers.get("x-api-key") or "").strip()
    return bool(enviada) and hmac.compare_digest(enviada, clave)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Sistema"])
def health_check(request: Request):
    """
    Verifica el estado del servicio y la conexion con Odoo.
    No requiere API Key - pensado para monitores y load balancers.
    """
    odoo_ok = _default_odoo is not None and _default_odoo.uid is not None
    status = "ok" if odoo_ok else "degradado"
    cuerpo = {
        "status": status,
        "odoo_conectado": odoo_ok,
    }

    # Las whitelists NO se publican sin autenticar: decian a cualquiera que
    # alcanzase el puerto que modelos y metodos acepta /odoo, que es el mapa
    # que necesita quien busca por donde entrar. Un monitor solo necesita
    # saber si el servicio responde.
    if _detalle_health_permitido(request):
        cuerpo["modelos_permitidos"] = (
            sorted(ALLOWED_MODELS) if ALLOWED_MODELS else "todos"
        )
        cuerpo["metodos_permitidos"] = (
            sorted(ALLOWED_METHODS) if ALLOWED_METHODS else "todos"
        )
    return cuerpo


@app.get(
    "/estado/{entidad}/{id_origen}",
    tags=["Sistema"],
    dependencies=[Depends(verify_api_key)],
)
def estado_sincronizacion(entidad: str, id_origen: str):
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
@limiter.limit("60/minute")
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
        raise HTTPException(status_code=422, detail=f"Error de Odoo: {e}")
    except Exception:
        # El detalle va al log, no a la respuesta: el texto de la excepcion
        # puede llevar rutas, nombres de tablas o trozos de la cadena de
        # conexion, que son material de reconocimiento para un atacante.
        logger.exception("ERROR_INESPERADO | model=%s method=%s", req.model, req.method)
        raise HTTPException(status_code=500, detail="Error interno del servicio.")

    duracion = round((time.time() - inicio) * 1000)
    logger.info(
        "OK | ip=%s model=%s method=%s duracion_ms=%d",
        ip, req.model, req.method, duracion,
    )
    return {"result": result}
