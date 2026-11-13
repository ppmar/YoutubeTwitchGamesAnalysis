"""Shared fixtures. No test ever reaches the YouTube API (SPEC section 0).

Most tests run on in-memory SQLite. The ones that exercise PostgreSQL-only behaviour
(advisory locks, concurrent runs) need a real server and are skipped unless
``TEST_DATABASE_URL`` is set.

Safety: those tests build their tables in a throwaway schema that is dropped
afterwards, and refuse to run against the collector's own database.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator

import pytest
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from ytcollector.config import PROJECT_ROOT, TWITCH_GAME_CSV
from ytcollector.db import create_all
from ytcollector.models import Base

# Pick up TEST_DATABASE_URL from .env without clobbering an explicit environment.
load_dotenv(PROJECT_ROOT / ".env", override=False)


@pytest.fixture
def session() -> Iterator[Session]:
    """An in-memory SQLite database with the full schema."""
    engine = create_engine("sqlite://", future=True)
    create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        yield session


@pytest.fixture(scope="session")
def twitch_csv() -> object:
    """Path to the real Twitch export, which ships with the repo."""
    if not TWITCH_GAME_CSV.is_file():
        pytest.skip(f"{TWITCH_GAME_CSV} is missing")
    return TWITCH_GAME_CSV


def _test_database_url() -> str:
    """Return TEST_DATABASE_URL, or skip, refusing the collector's own database."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; PostgreSQL-only tests are skipped")
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail(
            "TEST_DATABASE_URL points at the collector's own database. "
            "Use a separate PostgreSQL service for tests."
        )
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, got {url.split('://')[0]}://")
    return url


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """A PostgreSQL engine whose tables live in a schema dropped at the end.

    Working in a throwaway schema means a misconfigured URL cannot damage the
    collection tables, which live in ``public``.
    """
    url = _test_database_url()
    schema = f"test_{secrets.token_hex(6)}"
    engine = create_engine(url, future=True, connect_args={"options": f"-csearch_path={schema}"})

    with create_engine(url, future=True).connect() as setup:
        setup.execute(text(f'CREATE SCHEMA "{schema}"'))
        setup.commit()
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with create_engine(url, future=True).connect() as teardown:
            teardown.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            teardown.commit()


@pytest.fixture
def pg_session(pg_engine: Engine) -> Iterator[Session]:
    """A session on the PostgreSQL test schema, rolled back after each test."""
    factory = sessionmaker(bind=pg_engine, future=True)
    with factory() as session:
        yield session
        session.rollback()
