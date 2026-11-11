"""
Configuracion de Celery (Fase 5).

La cola desacopla la sincronizacion contable del ciclo request/response: los
endpoints encolan una tarea y responden 202, y un worker la procesa en segundo
plano con reintentos.

Modo EAGER automatico: si no hay CELERY_BROKER_URL configurado (p.ej. en tests o
en desarrollo sin Redis), las tareas se ejecutan de forma sincrona en el mismo
proceso (task.delay() corre inline). Asi el codigo es identico con y sin broker.
"""

import os

from celery import Celery

BROKER_URL = os.getenv("CELERY_BROKER_URL", "")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)

# Sin broker -> modo eager (ejecucion sincrona inline, util para tests/dev).
_EAGER = not BROKER_URL

celery_app = Celery(
    "api_odoo",
    broker=BROKER_URL or None,
    backend=RESULT_BACKEND or None,
)

celery_app.conf.update(
    task_always_eager=_EAGER,
    task_eager_propagates=_EAGER,   # en eager, las excepciones se propagan
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,            # re-encola si el worker muere a mitad
)

# ---------------------------------------------------------------------------
# Celery Beat: poller periodico (modo pull desde la DB del cliente).
#
# Solo se programa si hay SOURCE_DATABASE_URL (modo polling activo); si no, no
# se registra ninguna tarea periodica. El intervalo (segundos) es configurable
# con POLLER_INTERVALO_SEG (por defecto 30). Requiere arrancar Celery Beat:
#   celery -A core.celery_app.celery_app beat --loglevel=info
# ---------------------------------------------------------------------------
if os.getenv("SOURCE_DATABASE_URL"):
    _POLLER_INTERVALO = float(os.getenv("POLLER_INTERVALO_SEG", "30"))
    _POLLER_TENANT = os.getenv("POLLER_TENANT", "default")
    _POLLER_LIMITE = int(os.getenv("POLLER_LIMITE", "50"))
    celery_app.conf.beat_schedule = {
        "poller-cola-cliente": {
            "task": "core.tasks.poller_task",
            "schedule": _POLLER_INTERVALO,
            "kwargs": {"tenant": _POLLER_TENANT, "limite": _POLLER_LIMITE},
        }
    }

# ---------------------------------------------------------------------------
# Celery Beat: poller de asistencia Fibex (modo pull desde la API de Fibex).
#
# Independiente del poller de cola (arriba): se programa solo si hay FIBEX_API_URL.
# Fibex recomienda "una vez al dia consultar el dia anterior", asi que por defecto
# corre a una hora fija diaria (FIBEX_POLLER_HORA, UTC) procesando el dia anterior.
# Requiere arrancar Celery Beat (mismo proceso que el poller de cola):
#   celery -A core.celery_app.celery_app beat --loglevel=info
# ---------------------------------------------------------------------------
if os.getenv("FIBEX_API_URL"):
    from celery.schedules import crontab

    _FIBEX_TENANT = os.getenv("FIBEX_POLLER_TENANT", "default")
    # Hora UTC a la que correr (0-23). Venezuela = UTC-4, asi que 7 UTC = 3 a.m.
    # hora local: el dia anterior ya esta cerrado. Configurable por despliegue.
    _FIBEX_HORA = int(os.getenv("FIBEX_POLLER_HORA", "7"))
    _FIBEX_MIN = int(os.getenv("FIBEX_POLLER_MINUTO", "0"))
    celery_app.conf.beat_schedule = {
        **getattr(celery_app.conf, "beat_schedule", {}),
        "fibex-asistencia-diaria": {
            "task": "core.tasks.fibex_asistencia_task",
            "schedule": crontab(hour=_FIBEX_HORA, minute=_FIBEX_MIN),
            "kwargs": {"tenant": _FIBEX_TENANT},
        },
    }


def en_modo_eager() -> bool:
    """True si Celery corre inline (sin broker). Util para la respuesta HTTP."""
    return _EAGER
