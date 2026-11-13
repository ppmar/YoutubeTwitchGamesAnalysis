"""Step 1 — discover videos with search.list (SPEC section 5).

One call per slice and per page, out of 100 a day. Every call is planned, ordered
and recorded so an interrupted run resumes instead of restarting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ytcollector.client import QuotaExceededError, YouTubeClient
from ytcollector.models import Game, GameVideo, SearchSlice, Task
from ytcollector.planning import Slice, plan_slices, slice_order
from ytcollector.quota import QuotaExhausted
from ytcollector.tasks import PENDING, TaskQueue

logger = logging.getLogger("ytcollector.search")

KIND = "search"


@dataclass
class StageResult:
    """What one stage run did, for the daily log line."""

    processed: int = 0
    items: int = 0
    calls: int = 0
    stopped_on_quota: bool = False
    errors: int = 0


def enabled_games(session: Session, study: str, only: list[str] | None = None) -> list[Game]:
    query = select(Game).where(Game.study == study, Game.enabled)
    if only:
        query = query.where(Game.game_id.in_(only))
    return list(session.scalars(query))


def plan(
    session: Session,
    study: str,
    slicing: str = "month",
    before: int = 12,
    after: int = 6,
    games: list[str] | None = None,
) -> list[Slice]:
    """Every slice the study needs, in the order they should be collected."""
    roster = enabled_games(session, study, games)
    return slice_order(plan_slices(roster, slicing=slicing, before=before, after=after))


def enqueue(queue: TaskQueue, slices: list[Slice], pages_per_slice: int = 1) -> int:
    """Put the planned slices on the queue, skipping anything already finished."""
    created = 0
    for priority, item in enumerate(slices):
        before = queue.count(KIND)
        queue.enqueue(
            KIND,
            item.task_key,
            payload={
                "game_id": item.game_id,
                "period_start": item.period_start,
                "period_end": item.period_end,
                "published_after": item.published_after,
                "published_before": item.published_before,
                "yt_query": item.yt_query,
                "pages": pages_per_slice,
            },
            priority=priority,
        )
        created += queue.count(KIND) - before
    return created


def _store(
    session: Session,
    study: str,
    payload: dict,
    page: int,
    response: dict,
    params: dict,
) -> int:
    """Record the slice and its videos. Returns how many items came back."""
    items = response.get("items") or []
    page_info = response.get("pageInfo") or {}

    session.merge(
        SearchSlice(
            study=study,
            game_id=payload["game_id"],
            period_start=payload["period_start"],
            page=page,
            period_end=payload["period_end"],
            params=params,
            # Which query brought this slice back, so a later correction can be
            # told apart (SPEC_FIXES 1.3).
            query_version=payload.get("yt_query"),
            approx_total_results=page_info.get("totalResults"),
            n_returned=len(items),
            has_next_page=bool(response.get("nextPageToken")),
        )
    )

    for rank, item in enumerate(items, start=1 + (page - 1) * 50):
        video_id = (item.get("id") or {}).get("videoId")
        if not video_id:
            continue
        session.merge(
            GameVideo(
                study=study,
                game_id=payload["game_id"],
                video_id=video_id,
                period_start=payload["period_start"],
                rank=rank,
                source="search",
            )
        )
    return len(items)


def run(
    session: Session,
    client: YouTubeClient,
    study: str,
    limit: int | None = None,
    dry_run: bool = False,
) -> StageResult:
    """Work the search queue until the budget, or the limit, runs out."""
    queue = TaskQueue(session, study)
    result = StageResult()

    if dry_run:
        # Only report. Claiming and releasing each task would hand the same one
        # back on the next turn of the loop, forever.
        pending = session.scalars(
            select(Task)
            .where(Task.study == study, Task.kind == KIND, Task.status == PENDING)
            .order_by(Task.priority, Task.id)
            .limit(limit if limit is not None else 100_000)
        ).all()
        for task in pending:
            logger.debug(
                "would search %s %s", task.payload["game_id"], task.payload["period_start"]
            )
        # One summary line: a line per slice would be 1600 lines of Railway logs.
        by_game: dict[str, int] = {}
        for task in pending:
            game_id = task.payload["game_id"]
            by_game[game_id] = by_game.get(game_id, 0) + 1
        logger.info(
            "would search %d slices across %d games (largest: %s)",
            len(pending),
            len(by_game),
            ", ".join(f"{g}={n}" for g, n in sorted(by_game.items(), key=lambda kv: -kv[1])[:3]),
        )
        result.processed = len(pending)
        return result

    while limit is None or result.processed < limit:
        if not client.tracker.can_search():
            result.stopped_on_quota = True
            break

        task = queue.claim(KIND)
        if task is None:
            break

        payload = task.payload
        try:
            page_token = task.cursor
            pages = int(payload.get("pages", 1))
            for page in range(1, pages + 1):
                params = {
                    "q": payload["yt_query"],
                    "published_after": payload["published_after"],
                    "published_before": payload["published_before"],
                    "page_token": page_token,
                }
                response = client.search(**params)
                result.calls += 1
                result.items += _store(session, study, payload, page, response, params)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            queue.complete(task)
            result.processed += 1
        except (QuotaExhausted, QuotaExceededError):
            # Hand the task back untouched: tomorrow picks it up again.
            queue.pause(task, cursor=task.cursor)
            result.stopped_on_quota = True
            break
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            queue.fail(task, str(exc))
            result.errors += 1
            logger.warning("search %s failed: %s", task.task_key, exc)

        session.commit()

    session.commit()
    return result
