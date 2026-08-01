from logging.config import fileConfig

# noinspection PyPackageRequirements
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy import pool

from mediaforce.core.db_migrations import SQLITE_BUSY_TIMEOUT_MS
from mediaforce.core.db_migrations import register_database_identity_guards
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
    identity_guard = config.attributes.get("database_identity_guard")
    connection_factory = config.attributes.get(
        "database_identity_connection_factory"
    )
    if identity_guard is not None:
        identity_guard()
    connect_args = {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000}
    if connection_factory is not None:
        connect_args["factory"] = connection_factory
    connectable = create_engine(
        migration_url(),
        connect_args=connect_args,
        poolclass=pool.NullPool,
    )
    register_database_identity_guards(connectable, identity_guard)

    with connectable.connect() as connection:
        if identity_guard is not None:
            identity_guard()
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()
        if identity_guard is not None:
            identity_guard()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
