from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _build_db_url() -> str:
    db_name = os.getenv("NUTRITION_DB", os.getenv("POSTGRES_DB", "rag"))
    db_user = os.getenv("NUTRITION_USER", os.getenv("POSTGRES_USER", "rag"))
    db_password = os.getenv("NUTRITION_PASSWORD", os.getenv("POSTGRES_PASSWORD", "rag"))
    db_host = os.getenv("NUTRITION_HOST", os.getenv("POSTGRES_HOST", "localhost"))
    db_port = os.getenv("NUTRITION_PORT", os.getenv("POSTGRES_PORT", "5432"))
    return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


config.set_main_option("sqlalchemy.url", _build_db_url())


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
