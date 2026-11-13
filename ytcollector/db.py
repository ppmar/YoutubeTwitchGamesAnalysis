"""Database engine and sessions.

PostgreSQL is the only supported target in production; SQLite is used by unit tests
(SPEC section 10). The advisory execution lock lands in milestone 2.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
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


DEFAULT_LOCK_NAME = "ytc-daily"


class LockNotAcquired(RuntimeError):
    """Another run already holds the execution lock."""


def _lock_key(name: str) -> int:
    """Map a lock name to the signed 64-bit integer PostgreSQL expects."""
    digest = hashlib.sha256(name.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def execution_lock(session: Session, name: str = DEFAULT_LOCK_NAME) -> Iterator[None]:
    """Hold a PostgreSQL advisory lock for the duration of the block.

    Two runs at once would spend the quota twice, so a second one must stop rather
    than wait: the lock is tried, never blocked on. Railway already skips an
    overlapping cron run, but manual launches have no such protection
    (SPEC section 8).

    On SQLite the lock is a no-op; the real behaviour is only meaningful, and only
    tested, on PostgreSQL.
    """
    dialect = session.get_bind().dialect.name
    if dialect != "postgresql":
        yield
        return

    key = _lock_key(name)
    acquired = session.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
    ).scalar_one()
    if not acquired:
        raise LockNotAcquired(f"another run holds the {name!r} lock")
    try:
        yield
    finally:
        session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        session.commit()
