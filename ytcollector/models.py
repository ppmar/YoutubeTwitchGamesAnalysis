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
    Float,
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
    # A match needs a match term and no exclude term: without this, "PUBG"
    # catches PUBG Mobile (SPEC_FIXES 1.2).
    exclude_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
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


class SearchSlice(Base):
    """One search.list page for one (game, period) slice (SPEC section 5, step 1).

    The exact parameters are kept because YouTube's ranking is not reproducible: a
    later run of the same query returns a different set.
    """

    __tablename__ = "search_slices"

    study: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    period_start: Mapped[str] = mapped_column(String(10), primary_key=True)
    page: Mapped[int] = mapped_column(Integer, primary_key=True)

    period_end: Mapped[str] = mapped_column(String(10))
    params: Mapped[dict] = mapped_column(JSON)
    # Google calls this an approximation and caps it at 1e6: a coarse indicator only.
    approx_total_results: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    n_returned: Mapped[int] = mapped_column(Integer, default=0)
    has_next_page: Mapped[bool] = mapped_column(Boolean, default=False)
    # The query this slice was collected with, so a later change can be told apart.
    query_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameVideo(Base):
    """Link between a game and a video, with where it was found."""

    __tablename__ = "game_videos"

    study: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_start: Mapped[str] = mapped_column(String(10), primary_key=True)

    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 'search', 'panel' or 'cohort'.
    source: Mapped[str] = mapped_column(String(16), default="search")

    # Whether this video is about *this* game. Held per pair, not on the snapshot:
    # a snapshot-level flag was computed from every linked game's terms at once,
    # so one video counted for two games (SPEC_FIXES 1.1).
    title_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The term that decided, prefixed with '-' when an exclusion blocked it.
    match_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VideoSnapshot(Base):
    """Video statistics at one point in time (SPEC section 5, step 2).

    The API gives cumulative counters with no history, so `refresh-stats` adds a new
    snapshot instead of overwriting: that is the only way to build a real view
    series from now on. Hidden statistics are stored as NULL, never as 0.
    """

    __tablename__ = "video_snapshots"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    category_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    default_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_audio_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    like_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    topic_categories: Mapped[list | None] = mapped_column(JSON, nullable=True)
    live_broadcast_content: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actual_start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    made_for_kids: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Derived.
    is_short_proxy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    was_live: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # `title_match` now lives on `game_videos`, per (game, video) pair.
    video_type: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Comment collection state lives in `video_comment_state`, keyed by video:
    # a snapshot column would be reset by the next refresh.


class ChannelSnapshot(Base):
    """Channel statistics at one point in time (SPEC section 5, step 3)."""

    __tablename__ = "channel_snapshots"

    channel_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Channels may hide this; NULL means hidden, not zero.
    subscriber_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    video_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Comment(Base):
    """A comment's timestamp — and nothing else (SPEC section 5, step 4).

    No author, no author id, no text. The comment id is hashed so duplicates can be
    detected without keeping a reversible identifier.
    """

    __tablename__ = "comments"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    comment_id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)

    comment_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameMonth(Base):
    """Monthly aggregates per game, joinable to Twitch on (game_id, year, month).

    ``attention_*`` is a relative index, never a view count (SPEC section 6).
    """

    __tablename__ = "yt_game_month"

    study: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Attention score.
    attention_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    attention_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    share_method_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_comments_dated: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sample_basis: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Publication-month aggregates.
    n_videos_published: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_channels_publishing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views_sum: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    views_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    likes_sum: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    comments_sum: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagement_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_channel_view_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    channel_hhi: Mapped[float | None] = mapped_column(Float, nullable=True)
    share_shorts: Mapped[float | None] = mapped_column(Float, nullable=True)
    share_live: Mapped[float | None] = mapped_column(Float, nullable=True)
    share_by_video_type: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    median_subscribers: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_big_channel_video_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_video_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trailer_views_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VideoCommentState(Base):
    """How far the comment collection got for one video (SPEC_CALIBRATION 3.1).

    This used to be two columns on ``video_snapshots``, which was wrong: every
    ``refresh-stats`` writes a *new* snapshot, with those columns back at their
    default. A truncated video then looked complete again, and its history —
    amputated at exactly the early months that matter — went back to method A.

    Collection state belongs to the video, not to one observation of it.
    """

    __tablename__ = "video_comment_state"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    # Comments are off for this video; there is nothing to collect.
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # The per-video cap was reached, so the oldest comments are missing.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # The whole history was walked to its end.
    complete: Mapped[bool] = mapped_column(Boolean, default=False)

    n_stored: Mapped[int] = mapped_column(Integer, default=0)
    oldest_comment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    newest_comment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<VideoCommentState {self.video_id} stored={self.n_stored} "
            f"complete={self.complete} truncated={self.truncated}>"
        )
