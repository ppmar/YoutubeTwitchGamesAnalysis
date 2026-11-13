"""SQLAlchemy ORM models (SPEC section 7).

Only the `games` table lands in milestone 1; the collection tables follow in
milestone 2.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
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


class QuotaLedger(Base):
    """One row per quota day (SPEC section 8).

    YouTube resets quotas at midnight Pacific time, so the day key is a Pacific
    date, never a UTC one. ``search.list`` has its own counter; everything else
    shares the unit budget.
    """

    __tablename__ = "quota_ledger"

    quota_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    search_calls: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)
    # Set when the API itself says the budget is gone, before our own counter does.
    search_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    units_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:
        return f"<QuotaLedger {self.quota_date} search={self.search_calls} units={self.units}>"


class Task(Base):
    """A unit of collection work (SPEC section 8).

    ``payload`` carries whatever the stage needs; ``cursor`` keeps a page token so a
    long video's comments resume where they stopped instead of restarting tomorrow.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("study", "kind", "task_key", name="uq_tasks_identity"),
        Index("ix_tasks_pending", "study", "kind", "status", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    study: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    task_key: Mapped[str] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(16), default="pending")
    # Lower runs first: long-running games first, then outwards from the peak month.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Task {self.kind}:{self.task_key} {self.status}>"


class RawResponse(Base):
    """A cached API response, so `build-tables` can rebuild without the network.

    The stored request never contains the API key: it is stripped before hashing
    and before storage (SPEC sections 2 and 8).
    """

    __tablename__ = "raw_responses"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column(Integer)
    body_gzip: Mapped[bytes] = mapped_column(LargeBinary)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:
        return f"<RawResponse {self.endpoint} {self.request_hash[:12]}>"
