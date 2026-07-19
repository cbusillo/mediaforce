from logging.config import fileConfig

# noinspection PyPackageRequirements
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy import pool

from mediaforce.core.db_migrations import SQLITE_BUSY_TIMEOUT_MS
from mediaforce.core.db_tables import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def migration_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("Alembic sqlalchemy.url is not configured")
    return url


def run_migrations_offline() -> None:
    url = migration_url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        migration_url(),
        connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
