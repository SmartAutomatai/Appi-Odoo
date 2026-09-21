"""
Dependencias compartidas de seguridad y resolucion de tenant.

Se extraen a un modulo neutral para que los routers de negocio (facturas, pagos)
puedan reutilizarlas sin importar api.py (evita imports circulares).

Politica de autenticacion (FAIL-CLOSED)
--------------------------------------
Si API_KEY no esta configurada, el servicio RECHAZA las peticiones con 503 en
vez de dejarlas pasar. Antes ocurria lo contrario (fail-open): un .env no
montado en Docker o una variable no propagada dejaba la API completamente
abierta, y solo lo delataba una linea de WARNING en el arranque.

Para desarrollo local sin clave existe PERMITIR_SIN_API_KEY=true, que es una
decision explicita y visible en la configuracion, no un descuido.

La comparacion de la clave usa secrets.compare_digest (tiempo constante): una
comparacion normal termina en el primer byte distinto, y esa diferencia de
tiempo es medible y permite reconstruir el secreto byte a byte.
"""

import logging
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from odoo_universal import OdooUniversalAPI, get_tenant

logger = logging.getLogger("api-odoo")

api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


def _modo_sin_clave() -> bool:
    """
    True si se ha autorizado EXPLICITAMENTE operar sin API Key.

    Se lee en cada llamada (no al importar) para que un cambio en el entorno
    surta efecto sin reiniciar el proceso.
    """
    return os.getenv("PERMITIR_SIN_API_KEY", "").strip().lower() == "true"


def verify_api_key(x_api_key: Optional[str] = Depends(api_key_header)) -> None:
    """
    Verifica la cabecera X-Api-Key contra API_KEY del entorno.

    - Sin API_KEY y sin PERMITIR_SIN_API_KEY -> 503 (configuracion incompleta).
    - Sin API_KEY pero con PERMITIR_SIN_API_KEY=true -> pasa (modo desarrollo).
    - Con API_KEY -> compara en tiempo constante; 401 si no coincide.
    """
    api_key = os.getenv("API_KEY", "").strip()

    if not api_key:
        if _modo_sin_clave():
            return
        logger.error(
            "API_KEY no configurada y PERMITIR_SIN_API_KEY no activo: "
            "se rechaza la peticion."
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Servicio mal configurado: falta API_KEY. "
                "Para desarrollo sin clave, define PERMITIR_SIN_API_KEY=true."
            ),
        )

    # compare_digest evita el timing attack de una comparacion normal.
    if not x_api_key or not secrets.compare_digest(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="API Key invalida o ausente.")


def tenants_permitidos() -> tuple:
    """
    Lista blanca de tenants que la API acepta, leida del entorno en cada
    llamada (como API_KEY) para poder cerrarla sin reiniciar.

    Vacia = solo "default". Es el valor seguro: hoy solo hay una API Key, asi
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
    contrasta con la lista blanca: sin eso, el valor del cliente decidiria
    contra que instancia de Odoo se escribe.
    """
    permitidos = tenants_permitidos()
    if tenant not in permitidos:
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
