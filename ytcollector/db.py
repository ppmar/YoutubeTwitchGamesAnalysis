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

# Engines are pooled per URL rather than kept as a single global: caching one
# engine for the whole process means a later DATABASE_URL is silently ignored,
# which is wrong for any caller that connects to more than one database.
_engines: dict[str, Engine] = {}


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    """Return the engine for this URL, creating it on first use."""
    target = url or database_url()
    engine = _engines.get(target)
    if engine is None:
        engine = create_engine(target, echo=echo, future=True)
        _engines[target] = engine
    return engine


def dispose_engines() -> None:
    """Close every pooled engine. Used by tests and at shutdown."""
    while _engines:
        _, engine = _engines.popitem()
        engine.dispose()


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


def stale_lock_holders(session: Session, idle_minutes: int = 5) -> list[tuple[int, str]]:
    """Backends that hold an advisory lock and have been idle for too long.

    A container killed mid-run leaves its PostgreSQL session open until the server
    notices the dead connection, which can take hours. Until then its advisory lock
    blocks every following run, so the stale holders have to be found and dropped.
    Only sessions that are idle — never one doing work — are reported.
    """
    if session.get_bind().dialect.name != "postgresql":
        return []
    rows = session.execute(
        text(
            """
            SELECT a.pid, a.state
            FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory'
              AND l.pid <> pg_backend_pid()
              AND a.datname = current_database()
              AND a.state = 'idle'
              AND a.state_change < now() - make_interval(mins => :mins)
            """
        ),
        {"mins": idle_minutes},
    ).all()
    return [(int(pid), str(state)) for pid, state in rows]


def release_stale_locks(session: Session, idle_minutes: int = 5) -> int:
    """Terminate the idle backends still holding an advisory lock."""
    holders = stale_lock_holders(session, idle_minutes)
    for pid, _ in holders:
        session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    session.commit()
    return len(holders)


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
