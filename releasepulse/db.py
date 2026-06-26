"""Declarative base and shared database helpers.

Everything schema-related hangs off the single `Base.metadata` defined here:
the models register on it, and Alembic reads it to diff against a live database.
"""

from __future__ import annotations

import os

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

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
