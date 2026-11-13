"""Execution lock (SPEC section 8, milestone 2).

Two simultaneous runs must never both spend the search quota. These tests need a
real PostgreSQL: SQLite has no advisory locks.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from ytcollector.db import LockNotAcquired, execution_lock


def test_a_single_run_takes_the_lock(pg_engine: Engine) -> None:
    factory = sessionmaker(bind=pg_engine, future=True)
    with factory() as session, execution_lock(session, "test-lock-single"):
        held = session.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        ).scalar_one()
        assert held >= 1


def _take_and_release(session: object, name: str) -> None:
    """Take the lock and give it straight back, so the caller only sees the outcome."""
    with execution_lock(session, name):  # type: ignore[arg-type]
        pass


def test_a_second_run_is_refused_not_queued(pg_engine: Engine) -> None:
    factory = sessionmaker(bind=pg_engine, future=True)
    first, second = factory(), factory()
    try:
        # The second run must fail fast, not block until the first one ends.
        with execution_lock(first, "test-lock-contended"), pytest.raises(LockNotAcquired):
            _take_and_release(second, "test-lock-contended")
    finally:
        first.close()
        second.close()


def test_the_lock_is_released_afterwards(pg_engine: Engine) -> None:
    factory = sessionmaker(bind=pg_engine, future=True)
    first, second = factory(), factory()
    try:
        with execution_lock(first, "test-lock-release"):
            pass
        with execution_lock(second, "test-lock-release"):
            pass  # the next run gets it without trouble
    finally:
        first.close()
        second.close()


def test_the_lock_is_released_after_a_crash(pg_engine: Engine) -> None:
    factory = sessionmaker(bind=pg_engine, future=True)
    first, second = factory(), factory()
    try:
        with pytest.raises(RuntimeError), execution_lock(first, "test-lock-crash"):
            raise RuntimeError("the run died mid-collection")
        with execution_lock(second, "test-lock-crash"):
            pass
    finally:
        first.close()
        second.close()


def test_different_names_do_not_collide(pg_engine: Engine) -> None:
    factory = sessionmaker(bind=pg_engine, future=True)
    first, second = factory(), factory()
    try:
        with execution_lock(first, "test-lock-a"), execution_lock(second, "test-lock-b"):
            pass
    finally:
        first.close()
        second.close()
