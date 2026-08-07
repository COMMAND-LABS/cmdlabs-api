from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
from src.db.models import Base # NOTE: for integrating SQLAlchemy schema with Alembic for autogeneration
# Imported for their SIDE EFFECT of registering on Base.metadata, not for any
# name they export — hence the noqa. Feedback and Waitlist are declared outside
# models.py, so without these imports autogenerate cannot see them, concludes
# their models were deleted, and emits `op.drop_table` for both. Do not remove.
from src.db import feedback as _feedback_models  # noqa: F401
from src.db import waitlist as _waitlist_models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
# target_metadata = None
target_metadata = Base.metadata # NOTE: for integrating SQLAlchemy schema with Alembic for autogeneration

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
    # url = config.get_main_option("sqlalchemy.url")
    url = config.set_main_option('sqlalchemy.url', os.getenv("POSTGRES_URL"))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# Tables that exist in the database but deliberately have no SQLAlchemy model.
#
# Without this, autogenerate sees "table present, model absent", concludes the
# model was deleted, and proposes `op.drop_table` — buried among dozens of
# other operations in a file someone reviews quickly.
#
#   json_schemas - created by 151b744acb62_create_json_schemas_table.py. No
#                  model, and nothing in either service reads or writes it.
#                  Left in place pending a decision: either give it a model or
#                  drop it in an explicit, deliberate migration. Do NOT let
#                  autogenerate make that decision by accident.
#
# Keep this list SHORT and each entry justified. It suppresses a real signal,
# so anything added here should be something a human has actually looked at.
_TABLES_WITHOUT_MODELS = {"json_schemas"}


def include_object(object_, name, type_, reflected, compare_to):
    """Hide known model-less tables from autogenerate's drop detection."""
    if type_ == "table" and name in _TABLES_WITHOUT_MODELS:
        return False
    # An index belonging to one of those tables comes through separately.
    if type_ == "index" and getattr(object_, "table", None) is not None:
        if object_.table.name in _TABLES_WITHOUT_MODELS:
            return False
    return True


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    config.set_main_option('sqlalchemy.url', os.getenv("POSTGRES_URL"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            # One transaction PER MIGRATION rather than one for the whole run.
            #
            # Several migrations do `ALTER TYPE ... ADD VALUE` on a Postgres
            # enum and a later migration then writes that value. Postgres
            # refuses to use a new enum value in the transaction that added it
            # ("unsafe use of new value ... of enum type"), so with a single
            # wrapping transaction `alembic upgrade head` fails on any database
            # built from empty.
            #
            # It never surfaced in production because migrations were applied a
            # few at a time, which incidentally gave each enum change its own
            # transaction. This makes that guarantee explicit rather than
            # incidental, and is what allows the chain to be replayed in CI.
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
