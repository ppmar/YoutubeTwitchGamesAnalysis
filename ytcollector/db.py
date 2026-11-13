"""Database engine and sessions.

PostgreSQL is the only supported target in production; SQLite is used by unit tests
(SPEC section 10). The advisory execution lock lands in milestone 2.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from ytcollector.config import database_url
from ytcollector.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine, _session_factory
    if url is not None:
        return create_engine(url, echo=echo, future=True)
    if _engine is None:
        _engine = create_engine(database_url(), echo=echo, future=True)
        _session_factory = sessionmaker(bind=_engine, future=True)
    return _engine


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Yield a session and commit it, rolling back on any exception.

    Every batch write is transactional (SPEC section 8).
    """
    engine = get_engine(url)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine) -> None:
    """Create every table. Tests use this; production goes through alembic."""
    Base.metadata.create_all(engine)
