"""Engine and session plumbing, shared by the web app and the CLI importer.

One place decides where the data lives, so nothing else has to care whether
it is running against SQLite on a laptop or Postgres on Railway.
"""

from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_SQLITE_URL = "sqlite:///pine.db"


def normalize_url(url: str) -> str:
    """Make a provider-supplied URL usable by SQLAlchemy 2 + psycopg 3.

    Railway and Heroku both hand out ``postgres://``, which SQLAlchemy dropped,
    and ``postgresql://`` defaults to psycopg2, which we don't install.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def database_url(explicit: Optional[str] = None) -> str:
    return normalize_url(explicit or os.environ.get("DATABASE_URL") or DEFAULT_SQLITE_URL)


def make_engine(url: Optional[str] = None, echo: bool = False) -> Engine:
    resolved = database_url(url)
    kwargs: dict = {"echo": echo, "future": True}
    if resolved.startswith("postgresql"):
        # Railway's proxy drops idle connections; recycle before it bites.
        kwargs.update(pool_pre_ping=True, pool_recycle=280)
    return create_engine(resolved, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create any missing tables.

    Fine for the pilot. Once Wei's real data is in Postgres, switch to Alembic
    so column changes don't mean dropping the database.
    """
    Base.metadata.create_all(engine)
