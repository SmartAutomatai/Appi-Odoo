"""
Dependencias compartidas de seguridad y resolucion de tenant.

Se extraen a un modulo neutral para que los routers de negocio (facturas, pagos)
puedan reutilizarlas sin importar api.py (evita imports circulares).

La API Key se lee del entorno igual que en api.py: si no esta configurada, el
acceso pasa sin validar (modo desarrollo), coherente con el resto del servicio.
"""

import hmac
import logging
import os
import uuid
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address

from odoo_universal import OdooUniversalAPI, get_tenant

api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)

# ---------------------------------------------------------------------------
# Limitador de tasa (compartido por api.py y por los routers de negocio)
# ---------------------------------------------------------------------------
#
# Vive aqui, y no en api.py, porque los routers necesitan decorar sus endpoints
# con el MISMO objeto Limiter: importarlo desde api.py crearia un ciclo, ya que
# api.py importa los routers. Es la razon por la que existe este modulo neutral.
#
# Los limites se leen del entorno para poder ajustarlos por despliegue sin tocar
# codigo. Los valores por defecto son holgados frente al uso normal medido en
# las pruebas de carga (5 usuarios concurrentes) y estrechos frente a un abuso.

logger = logging.getLogger("api-odoo")

limiter = Limiter(key_func=get_remote_address)

# Endpoints de negocio: crean o modifican datos en Odoo.
LIMITE_NEGOCIO = os.getenv("RATE_LIMIT_NEGOCIO", "120/minute")

# Solo lectura (consultas de stock, panel): mas baratos, limite mas alto.
LIMITE_LECTURA = os.getenv("RATE_LIMIT_LECTURA", "240/minute")

# El poller es sincrono y pesado: una pasada de 50 filas tardo ~31,7 s en las
# pruebas. Varias a la vez agotan el pool de conexiones a Odoo, asi que su
# limite es deliberadamente bajo.
LIMITE_POLLER = os.getenv("RATE_LIMIT_POLLER", "6/minute")



def verify_api_key(x_api_key: Optional[str] = Depends(api_key_header)) -> None:
    """
    Verifica la cabecera X-Api-Key contra API_KEY del entorno.
    Sin API_KEY configurada, no valida (modo desarrollo).

    La comparacion usa hmac.compare_digest, que tarda lo mismo acierte el
    primer byte o el ultimo. El "!=" de Python corta en cuanto encuentra una
    diferencia, asi que el tiempo de respuesta delataba cuantos bytes
    iniciales eran correctos y la clave se podia reconstruir byte a byte en
    vez de por fuerza bruta sobre los 64 caracteres. Ver auditoria H-1.
    """
    api_key = os.getenv("API_KEY", "")
    if not api_key:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="API Key invalida o ausente.")


def tenants_permitidos() -> tuple:
    """
    Lista blanca de tenants que la API acepta, leida del entorno en cada
    llamada (como API_KEY) para poder cerrarla sin reiniciar.

    Vacia = solo "default". Es el valor seguro: hoy hay una sola API Key, asi
    que quien la tenga podria pedir cualquier tenant registrado con solo
    cambiar un campo del JSON. Mientras exista un unico inquilino el riesgo es
    teorico, pero se cierra ahora para que no se cuele al anadir el segundo.
    """
    crudo = os.getenv("TENANTS_PERMITIDOS", "").strip()
    if not crudo:
        return ("default",)
    return tuple(t.strip() for t in crudo.split(",") if t.strip())


def resolver_tenant(tenant: str = "default") -> OdooUniversalAPI:
    """
    Devuelve el conector Odoo del tenant indicado o lanza 400 si no existe.

    El nombre viaja en el JSON de la peticion, asi que antes de usarlo se
    contrasta con la lista blanca: sin eso, el valor que mande el cliente
    decidiria contra que instancia de Odoo se escribe.
    """
    if tenant not in tenants_permitidos():
        logger.warning("Rechazado tenant no permitido: %r", tenant)
        raise HTTPException(
            status_code=400, detail=f"Tenant '{tenant}' no permitido."
        )

    try:
        return get_tenant(tenant)
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"Tenant '{tenant}' no configurado."
        )


# ---------------------------------------------------------------------------
# Errores internos: se registran completos, se devuelven sin detalle
# ---------------------------------------------------------------------------


def error_interno(exc: Exception, contexto: str) -> HTTPException:
    """
    Registra una excepcion no controlada y devuelve un 500 SIN su texto.

    El mensaje de una excepcion puede llevar rutas del sistema, nombres de
    tablas, fragmentos de SQL de SQLAlchemy o cadenas de conexion; devolverlo
    al cliente le regala trabajo de reconocimiento a un atacante. En su lugar
    se genera un identificador corto que va en la respuesta Y en el log, para
    poder cruzar la queja del cliente con la traza sin exponer nada.

    Los errores de DATOS (422) conservan su mensaje: forman parte del contrato
    de la API y el cliente los necesita para corregir el registro en origen.
    Esto es solo para el 500 generico. Ver auditoria H-5.
    """
    incidencia = uuid.uuid4().hex[:12]
    logger.exception("ERROR_INTERNO | incidencia=%s | %s | %s",
                     incidencia, contexto, exc)
    return HTTPException(
        status_code=500,
        detail=f"Error interno del servicio. Referencia: {incidencia}",
    )


# Marcas de que el texto de un error de Odoo viene en realidad de PostgreSQL:
# Odoo reenvia el error del driver tal cual, con SQL, nombres de tablas y
# columnas dentro. Ver auditoria H-5b.
_RASTROS_SQL = (
    "LINE ", "SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM",
    "psycopg", "sqlalchemy", "relation ", "column ", "syntax for type",
    "constraint", "Traceback",
)


def sanear_error_odoo(exc: Exception) -> str:
    """
    Texto de un OdooExecutionError apto para devolver al cliente.

    Los errores de NEGOCIO de Odoo ("El asiento debe ser un borrador", "No se
    puede validar...") se devuelven intactos: el cliente los necesita para
    corregir el registro en origen, y son parte del contrato de la API.

    Los que en realidad vienen de PostgreSQL se sustituyen por un mensaje
    generico, porque llevan SQL, nombres de tablas y columnas: un mapa gratis
    del esquema para quien sondee el endpoint. El texto completo se registra.
    Ver auditoria H-5b.
    """
    texto = str(exc)
    if any(r.lower() in texto.lower() for r in _RASTROS_SQL):
        incidencia = uuid.uuid4().hex[:12]
        logger.warning("ERROR_ODOO_SQL | incidencia=%s | %s", incidencia, texto)
        return (
            "Odoo rechazo la operacion por un error de bajo nivel "
            f"(datos o tipos invalidos). Referencia: {incidencia}"
        )
    return texto
