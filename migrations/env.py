import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.config import settings
from app.database import Base

# Import all active models so their tables register on Base.metadata.
# DO NOT import app.models.models — it uses a separate Base with conflicting schemas.
from app.models.user import User  # noqa: F401
from app.models.role import Role  # noqa: F401
from app.models.shift import Shift  # noqa: F401
from app.models.agent import Agent  # noqa: F401
from app.models.booking import Booking, BookingEvent  # noqa: F401
from app.models.attendance import Attendance  # noqa: F401
from app.models.allocation import AllocationLog  # noqa: F401
from app.models.pending_queue import PendingQueue  # noqa: F401
from app.models.notification import Notification  # noqa: F401
from app.models.email_template import EmailTemplate  # noqa: F401
from app.models.email_message import EmailMessage, EmailAttachment  # noqa: F401
from app.models.booking_config import BookingConfig  # noqa: F401
from app.models.processed_email import ProcessedEmail  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
