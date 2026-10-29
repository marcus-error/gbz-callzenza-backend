from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    broker_connection_timeout=1.0,
    broker_connection_retry=False,
    broker_connection_retry_on_startup=False,
)
