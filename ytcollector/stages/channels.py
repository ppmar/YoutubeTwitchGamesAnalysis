"""Step 3 — channel statistics (SPEC section 5).

One unit per 50 ids. Subscriber counts can be hidden, in which case they are
stored as NULL rather than zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ytcollector.client import QuotaExceededError, YouTubeClient
from ytcollector.derive import parse_statistic
from ytcollector.models import ChannelSnapshot, VideoSnapshot
from ytcollector.quota import QuotaExhausted
from ytcollector.stages.search import StageResult
from ytcollector.stages.videos import _parse_time

logger = logging.getLogger("ytcollector.channels")

BATCH_SIZE = 50


def pending_channel_ids(session: Session, stale_before: datetime | None = None) -> list[str]:
    """Channels seen in a video snapshot that have no snapshot of their own yet."""
    known = select(VideoSnapshot.channel_id).where(VideoSnapshot.channel_id.isnot(None)).distinct()
    snapshotted = select(ChannelSnapshot.channel_id)
    if stale_before is not None:
        snapshotted = snapshotted.where(ChannelSnapshot.collected_at >= stale_before)
    return list(session.scalars(known.where(VideoSnapshot.channel_id.notin_(snapshotted))))


def build_snapshot(item: dict, collected_at: datetime) -> ChannelSnapshot:
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    related = ((item.get("contentDetails") or {}).get("relatedPlaylists")) or {}

    return ChannelSnapshot(
        channel_id=item.get("id"),
        collected_at=collected_at,
        title=snippet.get("title"),
        country=snippet.get("country"),
        published_at=_parse_time(snippet.get("publishedAt")),
        # hiddenSubscriberCount means the field is absent: NULL, not 0.
        subscriber_count=parse_statistic(statistics, "subscriberCount"),
        view_count=parse_statistic(statistics, "viewCount"),
        video_count=parse_statistic(statistics, "videoCount"),
        uploads_playlist_id=related.get("uploads"),
    )


def run(
    session: Session,
    client: YouTubeClient,
    study: str,
    channel_ids: list[str] | None = None,
    dry_run: bool = False,
) -> StageResult:
    ids = channel_ids if channel_ids is not None else pending_channel_ids(session)
    result = StageResult()
    if not ids:
        return result

    collected_at = datetime.now(UTC)
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]
        if dry_run:
            logger.info("would fetch %d channels", len(batch))
            result.processed += len(batch)
            continue
        try:
            response = client.channels(ids=batch)
        except (QuotaExhausted, QuotaExceededError):
            result.stopped_on_quota = True
            break
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result.errors += 1
            logger.warning("channels batch failed: %s", exc)
            continue

        result.calls += 1
        for item in response.get("items") or []:
            session.merge(build_snapshot(item, collected_at))
            result.items += 1
        result.processed += len(batch)
        session.commit()

    session.commit()
    return result
