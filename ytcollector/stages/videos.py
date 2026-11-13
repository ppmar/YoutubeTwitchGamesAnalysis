"""Step 2 — video statistics (SPEC section 5).

``videos.list`` costs one unit per 50 ids, so this stage is almost free next to
search. Every run appends a dated snapshot rather than overwriting, which is the
only way to build a real view series from a cumulative counter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ytcollector.client import QuotaExceededError, YouTubeClient
from ytcollector.derive import (
    SHORT_MAX_SECONDS,
    classify_video_type,
    duration_to_seconds,
    match_game,
    parse_statistic,
)
from ytcollector.models import Game, GameVideo, VideoSnapshot
from ytcollector.quota import QuotaExhausted
from ytcollector.stages.search import StageResult

logger = logging.getLogger("ytcollector.videos")

BATCH_SIZE = 50
DESCRIPTION_MAX_CHARS = 500


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def game_terms(session: Session, study: str) -> dict[str, tuple[list[str], list[str]]]:
    """Match and exclude terms per game id."""
    return {
        row.game_id: (row.match_terms or [], row.exclude_terms or [])
        for row in session.scalars(select(Game).where(Game.study == study))
    }


def apply_matches(session: Session, study: str, video_ids: list[str]) -> int:
    """Decide, for each (game, video) pair, whether the video is about that game.

    Held per pair rather than on the snapshot: computing one flag from the union
    of every linked game's terms made a single video count for two games at once
    (SPEC_FIXES 1.1).
    """
    if not video_ids:
        return 0

    terms = game_terms(session, study)
    snapshots = {
        snap.video_id: snap
        for snap in session.scalars(
            select(VideoSnapshot)
            .where(VideoSnapshot.video_id.in_(video_ids))
            .order_by(VideoSnapshot.collected_at)
        )
    }
    links = list(
        session.scalars(
            select(GameVideo).where(
                GameVideo.study == study, GameVideo.video_id.in_(video_ids)
            )
        )
    )

    decided = 0
    for link in links:
        snap = snapshots.get(link.video_id)
        if snap is None:
            continue
        match_terms, exclude_terms = terms.get(link.game_id, ([], []))
        matched, reason = match_game(
            match_terms, exclude_terms,
            title=snap.title, tags=snap.tags, description=snap.description,
        )
        link.title_match = matched
        link.match_reason = reason
        decided += 1
    session.flush()
    return decided


def pending_video_ids(session: Session, study: str, stale_before: datetime | None = None) -> list[str]:
    """Videos that have no snapshot yet, or none since ``stale_before``."""
    known = select(GameVideo.video_id).where(GameVideo.study == study).distinct()
    snapshotted = select(VideoSnapshot.video_id)
    if stale_before is not None:
        snapshotted = snapshotted.where(VideoSnapshot.collected_at >= stale_before)
    return list(session.scalars(known.where(GameVideo.video_id.notin_(snapshotted))))


def build_snapshot(item: dict, collected_at: datetime) -> VideoSnapshot:
    """Turn one videos.list item into a snapshot row."""
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    content = item.get("contentDetails") or {}
    live = item.get("liveStreamingDetails") or {}
    status = item.get("status") or {}
    topics = item.get("topicDetails") or {}

    description = snippet.get("description") or ""
    tags = snippet.get("tags") or []
    title = snippet.get("title") or ""
    duration = duration_to_seconds(content.get("duration"))

    return VideoSnapshot(
        video_id=item.get("id"),
        collected_at=collected_at,
        channel_id=snippet.get("channelId"),
        title=title,
        description=description[:DESCRIPTION_MAX_CHARS],
        tags=tags,
        published_at=_parse_time(snippet.get("publishedAt")),
        category_id=snippet.get("categoryId"),
        default_language=snippet.get("defaultLanguage"),
        default_audio_language=snippet.get("defaultAudioLanguage"),
        duration_seconds=duration,
        view_count=parse_statistic(statistics, "viewCount"),
        like_count=parse_statistic(statistics, "likeCount"),
        comment_count=parse_statistic(statistics, "commentCount"),
        topic_categories=topics.get("topicCategories"),
        live_broadcast_content=snippet.get("liveBroadcastContent"),
        actual_start_time=_parse_time(live.get("actualStartTime")),
        made_for_kids=status.get("madeForKids"),
        is_short_proxy=None if duration is None else duration <= SHORT_MAX_SECONDS,
        was_live=bool(live),
        # Matching happens per (game, video) pair in `apply_matches`.
        video_type=classify_video_type(title),
    )


def run(
    session: Session,
    client: YouTubeClient,
    study: str,
    video_ids: list[str] | None = None,
    dry_run: bool = False,
) -> StageResult:
    """Fetch statistics for every video that needs them, in batches of 50."""
    ids = video_ids if video_ids is not None else pending_video_ids(session, study)
    result = StageResult()
    if not ids:
        return result

    collected_at = datetime.now(UTC)

    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]
        if dry_run:
            logger.info("would fetch %d videos", len(batch))
            result.processed += len(batch)
            continue
        try:
            response = client.videos(ids=batch)
        except (QuotaExhausted, QuotaExceededError):
            result.stopped_on_quota = True
            break
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result.errors += 1
            logger.warning("videos batch failed: %s", exc)
            continue

        result.calls += 1
        seen: list[str] = []
        for item in response.get("items") or []:
            session.merge(build_snapshot(item, collected_at))
            seen.append(str(item.get("id")))
            result.items += 1
        session.flush()
        apply_matches(session, study, seen)
        result.processed += len(batch)
        session.commit()

    session.commit()
    return result
