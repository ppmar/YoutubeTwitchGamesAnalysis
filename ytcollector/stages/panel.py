"""Step 5 — fixed channel panel (SPEC section 5, phase 2).

``playlistItems.list`` walks a channel's uploads at one unit per 50 videos and
never touches the search counter. That makes it the cheap way to answer question
2 — do big creators start a trend or amplify one — because it gives a *complete*
upload history for a fixed set of channels, instead of whatever search chose to
return.

Two outputs come out of it: monthly video counts per game on a stable panel, and
the date of each creator's first video about each game.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ytcollector.client import QuotaExceededError, YouTubeClient
from ytcollector.config import CONFIG_DIR, read_yaml
from ytcollector.derive import matches_terms
from ytcollector.models import ChannelSnapshot, Game, GameVideo, VideoSnapshot
from ytcollector.quota import QuotaExhausted
from ytcollector.stages.search import StageResult
from ytcollector.stages.videos import run as fetch_videos

logger = logging.getLogger("ytcollector.panel")

KIND = "panel"
DEFAULT_MIN_SUBSCRIBERS = 100_000
MAX_PAGES_PER_CHANNEL = 200


def pinned_channels() -> list[str]:
    """Channel ids the team wants in the panel regardless of size."""
    data = read_yaml(CONFIG_DIR / "channel_panel.yml")
    return [str(c["channel_id"]) for c in (data.get("channels") or []) if c.get("channel_id")]


def panel_channels(
    session: Session, min_subscribers: int = DEFAULT_MIN_SUBSCRIBERS
) -> list[tuple[str, str | None]]:
    """``(channel_id, uploads_playlist_id)`` for every channel in the panel."""
    newest = (
        select(
            ChannelSnapshot.channel_id,
            func.max(ChannelSnapshot.collected_at).label("collected_at"),
        )
        .group_by(ChannelSnapshot.channel_id)
        .subquery()
    )
    rows = session.execute(
        select(
            ChannelSnapshot.channel_id,
            ChannelSnapshot.uploads_playlist_id,
            ChannelSnapshot.subscriber_count,
        ).join(
            newest,
            (ChannelSnapshot.channel_id == newest.c.channel_id)
            & (ChannelSnapshot.collected_at == newest.c.collected_at),
        )
    ).all()

    pinned = set(pinned_channels())
    return [
        (channel_id, playlist)
        for channel_id, playlist, subscribers in rows
        if channel_id in pinned or (subscribers or 0) >= min_subscribers
    ]


def collect_uploads(
    session: Session,
    client: YouTubeClient,
    playlist_id: str,
    max_pages: int = MAX_PAGES_PER_CHANNEL,
) -> list[str]:
    """Every video id in an uploads playlist. One unit per 50."""
    found: list[str] = []
    page_token: str | None = None
    for _ in range(max_pages):
        response = client.playlist_items(playlist_id, page_token=page_token)
        for item in response.get("items") or []:
            video_id = ((item.get("contentDetails") or {}).get("videoId"))
            if video_id:
                found.append(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return found


def attach_to_games(session: Session, study: str, video_ids: list[str]) -> int:
    """Link panel videos to games by matching terms locally — no API call.

    Search decided which videos it would show us; this does not. That is the whole
    point of the panel: a game's absence from a creator's uploads is then real.
    """
    games = list(session.scalars(select(Game).where(Game.study == study, Game.enabled)))
    snapshots = {
        snap.video_id: snap
        for snap in session.scalars(
            select(VideoSnapshot).where(VideoSnapshot.video_id.in_(video_ids))
        )
    }

    linked = 0
    for video_id, snap in snapshots.items():
        for game in games:
            if not matches_terms(
                game.match_terms or [], title=snap.title, tags=snap.tags,
                description=snap.description,
            ):
                continue
            period = (
                snap.published_at.strftime("%Y-%m-01")
                if snap.published_at
                else datetime.now(UTC).strftime("%Y-%m-01")
            )
            existing = session.get(
                GameVideo, (study, game.game_id, video_id, period)
            )
            if existing is None:
                session.add(
                    GameVideo(
                        study=study, game_id=game.game_id, video_id=video_id,
                        period_start=period, source="panel",
                    )
                )
                linked += 1
    return linked


@dataclass
class FirstVideo:
    """When a creator first covered a game — the heart of question 2."""

    game_id: str
    channel_id: str
    first_published_at: datetime
    subscriber_count: int | None


def first_videos_per_channel(session: Session, study: str) -> list[FirstVideo]:
    """For each (game, channel), the earliest matched video."""
    newest_channel = (
        select(
            ChannelSnapshot.channel_id,
            func.max(ChannelSnapshot.collected_at).label("collected_at"),
        )
        .group_by(ChannelSnapshot.channel_id)
        .subquery()
    )
    rows = session.execute(
        select(
            GameVideo.game_id,
            VideoSnapshot.channel_id,
            func.min(VideoSnapshot.published_at),
            func.max(ChannelSnapshot.subscriber_count),
        )
        .join(VideoSnapshot, VideoSnapshot.video_id == GameVideo.video_id)
        .outerjoin(
            newest_channel, newest_channel.c.channel_id == VideoSnapshot.channel_id
        )
        .outerjoin(
            ChannelSnapshot,
            (ChannelSnapshot.channel_id == newest_channel.c.channel_id)
            & (ChannelSnapshot.collected_at == newest_channel.c.collected_at),
        )
        .where(
            GameVideo.study == study,
            VideoSnapshot.title_match.is_(True),
            VideoSnapshot.published_at.isnot(None),
        )
        .group_by(GameVideo.game_id, VideoSnapshot.channel_id)
    ).all()
    return [
        FirstVideo(game_id, channel_id, published, subscribers)
        for game_id, channel_id, published, subscribers in rows
        if channel_id and published
    ]


def monthly_counts_per_game(session: Session, study: str) -> dict[tuple[str, str], int]:
    """Videos per ``(game_id, YYYY-MM)`` across the panel, on a fixed set of channels."""
    rows = session.execute(
        select(GameVideo.game_id, VideoSnapshot.published_at)
        .join(VideoSnapshot, VideoSnapshot.video_id == GameVideo.video_id)
        .where(
            GameVideo.study == study,
            GameVideo.source == "panel",
            VideoSnapshot.title_match.is_(True),
            VideoSnapshot.published_at.isnot(None),
        )
    ).all()
    counts: dict[tuple[str, str], int] = {}
    for game_id, published in rows:
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        key = (game_id, published.astimezone(UTC).strftime("%Y-%m"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def run(
    session: Session,
    client: YouTubeClient,
    study: str,
    min_subscribers: int = DEFAULT_MIN_SUBSCRIBERS,
    dry_run: bool = False,
) -> StageResult:
    """Walk the panel's uploads, snapshot them, and attach them to games."""
    channels = panel_channels(session, min_subscribers)
    result = StageResult()
    if not channels:
        return result

    if dry_run:
        logger.info("would walk uploads for %d channels", len(channels))
        result.processed = len(channels)
        return result

    for channel_id, playlist_id in channels:
        if not playlist_id:
            logger.info("channel %s has no uploads playlist, skipping", channel_id)
            continue
        try:
            video_ids = collect_uploads(session, client, playlist_id)
        except (QuotaExhausted, QuotaExceededError):
            result.stopped_on_quota = True
            break
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result.errors += 1
            logger.warning("panel %s failed: %s", channel_id, exc)
            continue

        result.processed += 1
        if not video_ids:
            continue

        # Fetch statistics so the local match has titles, tags and descriptions.
        fetch_videos(session, client, study, video_ids=video_ids)
        result.items += attach_to_games(session, study, video_ids)
        session.commit()

    session.commit()
    return result
