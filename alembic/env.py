from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.config import settings
from app.database import Base
from app import models  # noqa: F401


config = context.config
configured_url = config.get_main_option("sqlalchemy.url")
database_url = (
    settings().database_url
    if os.getenv("DATABASE_URL") or not configured_url
    else configured_url
)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_postgresql = connection.dialect.name == "postgresql"
        if is_postgresql:
            connection.execute(
                text(
                    "SELECT pg_advisory_lock("
                    "hashtext('xnamai_bi_alembic_migrations'))"
                )
            )
            connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_postgresql:
                connection.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtext('xnamai_bi_alembic_migrations'))"
                    )
                )
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
