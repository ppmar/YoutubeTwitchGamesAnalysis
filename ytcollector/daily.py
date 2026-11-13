"""The daily run (SPEC section 9).

One pass of the whole pipeline, bounded by the day's quota. Three properties
matter more than what it collects:

- **It always terminates.** A Railway cron service that never returns blocks every
  following run.
- **It never runs twice at once.** The advisory lock makes a second launch stop
  instead of spending the quota again.
- **It exits 0 unless the configuration or the database is broken.** An exhausted
  quota is the normal end of a day, not a failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ytcollector import attention as attention_module
from ytcollector import tables
from ytcollector.client import YouTubeClient
from ytcollector.config import StudyConfig
from ytcollector.models import GameVideo, VideoSnapshot
from ytcollector.planning import day_estimate
from ytcollector.stages import channels as channels_stage
from ytcollector.stages import comments as comments_stage
from ytcollector.stages import search as search_stage
from ytcollector.stages import videos as videos_stage
from ytcollector.tasks import TaskQueue

logger = logging.getLogger("ytcollector.daily")

REFRESH_AFTER_DAYS = 7


@dataclass
class DailySummary:
    """What the run did, printed as the last line of the logs."""

    study: str
    search_calls: int = 0
    search_slices: int = 0
    videos: int = 0
    channels: int = 0
    comments: int = 0
    refreshed: int = 0
    game_months: int = 0
    attention_rows: int = 0
    slices_remaining: int = 0
    days_remaining: int = 0
    quota: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    def as_line(self) -> str:
        mode = " [dry run]" if self.dry_run else ""
        return (
            f"daily{mode} study={self.study} "
            f"search_calls={self.search_calls} slices={self.search_slices} "
            f"videos={self.videos} channels={self.channels} comments={self.comments} "
            f"refreshed={self.refreshed} game_months={self.game_months} "
            f"attention={self.attention_rows} "
            f"slices_remaining={self.slices_remaining} days_remaining={self.days_remaining}"
        )


def needs_refresh(session: Session, after_days: int = REFRESH_AFTER_DAYS) -> bool:
    """Whether the newest snapshot is old enough to justify a new one."""
    latest = session.scalar(select(func.max(VideoSnapshot.collected_at)))
    if latest is None:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return datetime.now(UTC) - latest >= timedelta(days=after_days)


def run(
    session: Session,
    client: YouTubeClient,
    study: StudyConfig,
    settings: dict[str, Any],
    dry_run: bool = False,
) -> DailySummary:
    """Run the pipeline once and return what happened."""
    name = study.study
    summary = DailySummary(study=name, dry_run=dry_run)
    queue = TaskQueue(session, name)

    # A container killed mid-task leaves rows claimed; free them first.
    recovered = queue.recover_orphans()
    if recovered:
        logger.info("recovered %d orphaned tasks", recovered)
    session.commit()

    # Step 1 - search, until the search counter is empty.
    slices = search_stage.plan(
        session, name,
        slicing=settings.get("search", {}).get("slicing", "month"),
        before=study.months_before_first,
        after=study.months_after_last,
    )
    search_stage.enqueue(
        queue, slices, pages_per_slice=int(settings.get("search", {}).get("pages_per_slice", 1))
    )
    session.commit()

    search_result = search_stage.run(session, client, name, dry_run=dry_run)
    summary.search_calls = search_result.calls
    summary.search_slices = search_result.processed

    # Steps 2 and 3 - cheap in units, always worth doing.
    summary.videos = videos_stage.run(session, client, name, dry_run=dry_run).items
    summary.channels = channels_stage.run(session, client, name, dry_run=dry_run).items

    # Weekly snapshot, which is what later calibrates the attention score.
    if not dry_run and needs_refresh(session):
        ids = list(
            session.scalars(select(GameVideo.video_id).where(GameVideo.study == name).distinct())
        )
        summary.refreshed = videos_stage.run(session, client, name, video_ids=ids).items

    # Step 4 - comments, down to the unit reserve.
    summary.comments = comments_stage.run(
        session, client, name,
        min_comments=int(settings.get("comments", {}).get("min_comments", 30)),
        max_threads_per_video=int(
            settings.get("comments", {}).get("max_threads_per_video", 50_000)
        ),
        reserve=int(settings.get("quota", {}).get("units_reserve", 1_000)),
        dry_run=dry_run,
    ).items

    # Rebuild, which needs no network at all.
    summary.game_months = tables.build(session, name)["yt_game_month"]
    summary.attention_rows = attention_module.build(session, name)

    progress = queue.progress("search")
    summary.slices_remaining = progress["pending"] + progress["running"]
    summary.days_remaining = day_estimate(
        summary.slices_remaining, client.tracker.limits.usable_search_calls
    )
    summary.quota = client.tracker.summary()

    session.commit()
    return summary
