"""Celery application and beat schedule."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "uzlex",
    broker=str(settings.REDIS_DSN),
    backend=str(settings.REDIS_DSN),
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Tashkent",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=6 * 60 * 60,
    task_soft_time_limit=6 * 60 * 60 - 300,
    worker_max_tasks_per_child=50,  # sentence-transformers leaks over long runs
    worker_prefetch_multiplier=1,   # ingestion tasks are long; don't hoard them
    broker_connection_retry_on_startup=True,
    result_expires=60 * 60 * 24 * 7,
)

celery_app.conf.beat_schedule = {
    # Daily incremental sync of the seeded codes. Content hashing means an
    # unchanged act costs one HTTP request and nothing else.
    "sync-lexuz-daily": {
        "task": "app.workers.tasks.ingest_connector_task",
        "schedule": crontab(hour=3, minute=0),
        "kwargs": {
            "connector": "lexuz",
            "seeds": True,
            "languages": ["uz-Latn", "ru"],
        },
    },
    # Weekly wider crawl for newly published acts.
    "discover-new-acts-weekly": {
        "task": "app.workers.tasks.ingest_connector_task",
        "schedule": crontab(day_of_week=0, hour=4, minute=0),
        "kwargs": {
            "connector": "lexuz",
            "seeds": False,
            "search_terms": ["qonun", "farmon", "qaror", "kodeks"],
            "limit": 300,
        },
    },
    # Detects a lex.uz layout change before it silently empties the corpus.
    "connector-selfcheck-daily": {
        "task": "app.workers.tasks.connector_selfcheck_task",
        "schedule": crontab(hour=6, minute=30),
    },
    "resolve-crossrefs-daily": {
        "task": "app.workers.tasks.resolve_pending_crossrefs_task",
        "schedule": crontab(hour=5, minute=0),
    },
    "prune-logs-weekly": {
        "task": "app.workers.tasks.prune_logs_task",
        "schedule": crontab(day_of_week=1, hour=2, minute=0),
        "kwargs": {"days": 365},
    },
}
