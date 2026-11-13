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
    matches_terms,
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


def match_terms_by_video(session: Session, study: str) -> dict[str, list[str]]:
    """Map each video to the match terms of every game it was found for.

    A video can be returned by several games' queries; it counts as a match if it
    matches any of them.
    """
    rows = session.execute(
        select(GameVideo.video_id, Game.match_terms)
        .join(Game, (Game.study == GameVideo.study) & (Game.game_id == GameVideo.game_id))
        .where(GameVideo.study == study)
    ).all()
    terms: dict[str, list[str]] = {}
    for video_id, match_terms in rows:
        terms.setdefault(video_id, []).extend(match_terms or [])
    return terms


def pending_video_ids(session: Session, study: str, stale_before: datetime | None = None) -> list[str]:
    """Videos that have no snapshot yet, or none since ``stale_before``."""
    known = select(GameVideo.video_id).where(GameVideo.study == study).distinct()
    snapshotted = select(VideoSnapshot.video_id)
    if stale_before is not None:
        snapshotted = snapshotted.where(VideoSnapshot.collected_at >= stale_before)
    return list(session.scalars(known.where(GameVideo.video_id.notin_(snapshotted))))


def build_snapshot(item: dict, terms: list[str], collected_at: datetime) -> VideoSnapshot:
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
        # The full description is used for matching, only the prefix is stored.
        title_match=matches_terms(terms, title=title, tags=tags, description=description),
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

    terms_by_video = match_terms_by_video(session, study)
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
        for item in response.get("items") or []:
            session.merge(build_snapshot(item, terms_by_video.get(item.get("id"), []), collected_at))
            result.items += 1
        result.processed += len(batch)
        session.commit()

    session.commit()
    return result
