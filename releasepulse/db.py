"""Declarative base and shared database helpers.

Everything schema-related hangs off the single `Base.metadata` defined here:
the models register on it, and Alembic reads it to diff against a live database.
"""

from __future__ import annotations

import os

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Deterministic names for every constraint and index. Without this, SQLAlchemy
# lets Postgres auto-name things (e.g. unnamed CHECK/UNIQUE), which makes Alembic
# downgrades brittle - you cannot reliably DROP a constraint you cannot name.
# These templates give every object a stable, predictable name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_database_url() -> str:
    """Read the SQLAlchemy URL from the environment.

    Kept here so both Alembic's env.py and (later) application code share one
    source of truth instead of hardcoding the URL in alembic.ini.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it (see .env.example), e.g. "
            "postgresql+psycopg://postgres:dev@localhost:5432/releasepulse"
        )
    return url


# The engine (connection pool) and session factory are built lazily on first
# use, so importing this module never requires DATABASE_URL to be set - which
# keeps unit tests that don't touch the database cheap to import.
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        # pool_pre_ping guards against stale connections (e.g. Postgres restarted).
        _engine = create_engine(get_database_url(), pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=Session
        )
    return _session_factory
