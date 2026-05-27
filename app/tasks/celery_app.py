from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "bts",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "poll-email-every-30s": {
            "task": "app.tasks.tasks.poll_email_inbox",
            "schedule": settings.EMAIL_POLL_INTERVAL_SECONDS,
        },
    },
)
