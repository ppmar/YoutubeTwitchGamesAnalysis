"""Shared fixtures. No test ever reaches the YouTube API (SPEC section 0)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ytcollector.config import TWITCH_GAME_CSV
from ytcollector.db import create_all


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
