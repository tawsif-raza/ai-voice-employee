import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Phase 12 (plan.md Step 12.3): reuse src/agent/db_models.py's declarative
# Base/metadata for autogenerate support, and src/agent/db.py's own
# load_database_config() for the connection URL -- one source of truth
# for "what DATABASE_URL means" shared with the running application,
# rather than a second, competing URL-resolution path living only in
# alembic.ini. alembic.ini's own sqlalchemy.url is left as a documented
# placeholder (never a real credential) and is only actually used if this
# import/override path fails for some reason.
_SRC_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
if _SRC_AGENT_DIR not in sys.path:
    sys.path.insert(0, _SRC_AGENT_DIR)

from db_models import Base  # noqa: E402

target_metadata = Base.metadata

try:
    from db import load_database_config  # noqa: E402

    config.set_main_option("sqlalchemy.url", load_database_config().url)
except Exception:
    # Configuration error (e.g. PERSISTENCE_MODE=production with no
    # DATABASE_URL) -- surface it the same way any other Alembic
    # misconfiguration would, rather than silently falling back to
    # alembic.ini's placeholder URL and migrating the wrong database.
    raise

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
