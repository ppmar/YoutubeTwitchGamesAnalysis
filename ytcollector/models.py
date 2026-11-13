"""SQLAlchemy ORM models (SPEC section 7).

Only the `games` table lands in milestone 1; the collection tables follow in
milestone 2.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Game(Base):
    """A game of a study, with its Twitch aggregates.

    ``yt_query``, ``match_terms`` and ``enabled`` are meant to be tuned by hand once
    the first search results come back; `prepare-games` never overwrites them on an
    existing row (SPEC section 4).
    """

    __tablename__ = "games"

    study: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_id: Mapped[str] = mapped_column(String(128), primary_key=True)

    game_name: Mapped[str] = mapped_column(String(256))
    twitch_names: Mapped[list[str]] = mapped_column(JSON)
    yt_query: Mapped[str] = mapped_column(Text)
    match_terms: Mapped[list[str]] = mapped_column(JSON)
    role: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    release_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Twitch months before this one belong to another game or another mode.
    valid_from: Mapped[str | None] = mapped_column(String(7), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Aggregates recomputed from the Twitch CSV on every run.
    # Fortnite alone exceeds 6e9 hours: a 32-bit integer overflows on PostgreSQL.
    hours_watched_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    months_in_top200: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    last_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    peak_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    best_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:
        return f"<Game {self.study}/{self.game_id} months={self.months_in_top200}>"
