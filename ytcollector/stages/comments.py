"""Step 4 — comment timestamps (SPEC section 5).

Comments are the only natively dated signal YouTube exposes, which makes them the
backbone of the attention score. Three constraints shape this stage:

- **The history must be complete.** ``order=time`` returns the newest first, so a
  cap amputates the *beginning* of a video's life — exactly the part that matters.
  A video that hits the cap is marked truncated and falls back to method B.
- **Minimise what is kept.** Only the timestamp, the like and reply counts. No
  author, no author id, no text. The comment id is hashed so duplicates can be
  spotted without storing a reversible identifier.
- **Resume mid-video.** A popular video can outlive the day's budget; the page
  token is stored so tomorrow continues instead of starting over.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from ytcollector.client import ApiError, QuotaExceededError, YouTubeClient
from ytcollector.models import Comment, GameVideo, VideoCommentState, VideoSnapshot
from ytcollector.quota import QuotaExhausted
from ytcollector.stages.search import StageResult
from ytcollector.stages.videos import _parse_time
from ytcollector.tasks import TaskQueue

logger = logging.getLogger("ytcollector.comments")

KIND = "comments"
PAGE_SIZE = 100


def hash_comment_id(comment_id: str) -> str:
    """One-way hash: enough to deduplicate, useless for re-identification."""
    return hashlib.sha256(comment_id.encode()).hexdigest()


def eligible_videos(
    session: Session, study: str, min_comments: int = 30
) -> list[tuple[str, str, int]]:
    """Videos worth collecting, as ``(game_id, video_id, view_count)``.

    Only matched videos with enough comments for the distribution to mean
    anything. Ordered by views inside each game, then interleaved across games so
    no single game eats the whole budget.
    """
    newest = (
        select(VideoSnapshot.video_id, func.max(VideoSnapshot.collected_at).label("collected_at"))
        .group_by(VideoSnapshot.video_id)
        .subquery()
    )
    rows = session.execute(
        select(GameVideo.game_id, VideoSnapshot.video_id, VideoSnapshot.view_count)
        .join(newest, VideoSnapshot.video_id == newest.c.video_id)
        .join(GameVideo, GameVideo.video_id == VideoSnapshot.video_id)
        .where(
            VideoSnapshot.collected_at == newest.c.collected_at,
            GameVideo.study == study,
            VideoSnapshot.title_match.is_(True),
            VideoSnapshot.comment_count >= min_comments,
            # Collection state lives on the video, never on a snapshot.
            VideoSnapshot.video_id.notin_(
                select(VideoCommentState.video_id).where(VideoCommentState.disabled.is_(True))
            ),
        )
        .distinct()
    ).all()

    by_game: dict[str, list[tuple[str, str, int]]] = {}
    for game_id, video_id, views in rows:
        by_game.setdefault(game_id, []).append((game_id, video_id, views or 0))
    for items in by_game.values():
        items.sort(key=lambda t: -t[2])

    # Round-robin across games: one video each, then the next, and so on.
    interleaved: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for index in range(max((len(v) for v in by_game.values()), default=0)):
        for game_id in sorted(by_game):
            items = by_game[game_id]
            if index < len(items) and items[index][1] not in seen:
                seen.add(items[index][1])
                interleaved.append(items[index])
    return interleaved


def enqueue(queue: TaskQueue, videos: list[tuple[str, str, int]]) -> int:
    created = 0
    for priority, (game_id, video_id, views) in enumerate(videos):
        before = queue.count(KIND)
        queue.enqueue(
            KIND,
            f"video:{video_id}",
            payload={"game_id": game_id, "video_id": video_id, "view_count": views},
            priority=priority,
        )
        created += queue.count(KIND) - before
    return created


def _store_page(session: Session, video_id: str, response: dict) -> int:
    """Persist one page of threads. Timestamps only.

    Written as one bulk insert that ignores duplicates, never a merge per row: a
    popular video carries tens of thousands of comments, and merging each one
    would issue a SELECT apiece and keep every object in the session until the
    process ran out of memory.
    """
    now = datetime.now(UTC)
    rows = []
    for item in response.get("items") or []:
        top = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
        published = _parse_time(top.get("publishedAt"))
        if published is None:
            continue
        rows.append(
            {
                "video_id": video_id,
                "comment_id_hash": hash_comment_id(str(item.get("id"))),
                "comment_published_at": published,
                "like_count": top.get("likeCount"),
                "reply_count": (item.get("snippet") or {}).get("totalReplyCount"),
                "collected_at": now,
            }
        )
    if not rows:
        return 0

    # Deduplicate inside the page too: a repeated id would break the insert.
    unique: dict[tuple[str, str], dict] = {
        (row["video_id"], row["comment_id_hash"]): row for row in rows
    }
    session.execute(_insert_ignoring_duplicates(session), list(unique.values()))
    return len(unique)


def _insert_ignoring_duplicates(session: Session) -> Any:
    """An INSERT that skips rows already stored, for the session's dialect."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        statement = pg_insert(Comment)
        return statement.on_conflict_do_nothing(
            index_elements=["video_id", "comment_id_hash"]
        )
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = sqlite_insert(Comment)
        return statement.on_conflict_do_nothing(
            index_elements=["video_id", "comment_id_hash"]
        )
    return insert(Comment)


def mark_state(
    session: Session,
    video_id: str,
    *,
    disabled: bool | None = None,
    truncated: bool | None = None,
    complete: bool | None = None,
    stored: int | None = None,
) -> VideoCommentState:
    """Record how far the comment collection got, on the video itself.

    Kept out of ``video_snapshots`` on purpose: a later snapshot would reset it
    and silently hand an amputated history back to method A
    (SPEC_CALIBRATION 3.1).
    """
    state = session.get(VideoCommentState, video_id)
    if state is None:
        state = VideoCommentState(video_id=video_id)
        session.add(state)

    if disabled is not None:
        state.disabled = disabled
    if truncated is not None:
        state.truncated = truncated
        if truncated:
            # A truncated history is not a complete one, whatever came before.
            state.complete = False
    if complete is not None:
        state.complete = complete
    if stored:
        state.n_stored = (state.n_stored or 0) + stored

    bounds = session.execute(
        select(func.min(Comment.comment_published_at), func.max(Comment.comment_published_at))
        .where(Comment.video_id == video_id)
    ).one()
    state.oldest_comment_at, state.newest_comment_at = bounds
    state.last_fetched_at = datetime.now(UTC)
    session.flush()
    return state


def collect_video(
    session: Session,
    client: YouTubeClient,
    video_id: str,
    page_token: str | None = None,
    max_threads: int = 50_000,
    reserve: int = 1_000,
) -> tuple[int, str | None, str]:
    """Walk a video's comment pages.

    Returns ``(stored, next_page_token, outcome)`` where outcome is ``complete``,
    ``paused``, ``truncated`` or ``disabled``.
    """
    stored = 0
    pages = max_threads // PAGE_SIZE

    for _ in range(pages):
        try:
            response = client.comment_threads(
                video_id=video_id, page_token=page_token, max_results=PAGE_SIZE, reserve=reserve
            )
        except ApiError as exc:
            if exc.status == 403:
                # Comments are off for this video: flag it and move on.
                mark_state(session, video_id, disabled=True)
                return stored, None, "disabled"
            raise

        stored += _store_page(session, video_id, response)
        # Commit and let go of the page: holding a whole video's comments in the
        # session is what made the container run out of memory.
        session.commit()
        session.expunge_all()
        page_token = response.get("nextPageToken")
        if not page_token:
            mark_state(session, video_id, complete=True, stored=stored)
            return stored, None, "complete"

    # The cap was reached, so the earliest comments are missing. The video can no
    # longer carry method A and is handed to the decay kernel instead.
    mark_state(session, video_id, truncated=True, stored=stored)
    return stored, page_token, "truncated"


def run(
    session: Session,
    client: YouTubeClient,
    study: str,
    min_comments: int = 30,
    max_threads_per_video: int = 50_000,
    games: list[str] | None = None,
    reserve: int = 1_000,
    limit: int | None = None,
    dry_run: bool = False,
) -> StageResult:
    queue = TaskQueue(session, study)
    videos = eligible_videos(session, study, min_comments)
    if games:
        videos = [v for v in videos if v[0] in set(games)]
    enqueue(queue, videos)
    session.commit()

    result = StageResult()
    if dry_run:
        result.processed = queue.count(KIND, status="pending")
        logger.info("would collect comments for %d videos", result.processed)
        return result

    while limit is None or result.processed < limit:
        if client.tracker.remaining_units(reserve=reserve) <= 0:
            result.stopped_on_quota = True
            break

        task = queue.claim(KIND)
        if task is None:
            break

        video_id = task.payload["video_id"]
        try:
            stored, token, outcome = collect_video(
                session, client, video_id, page_token=task.cursor,
                max_threads=max_threads_per_video, reserve=reserve,
            )
            result.items += stored
            result.processed += 1
            if outcome == "paused":
                queue.pause(task, cursor=token)
            else:
                queue.complete(task)
        except (QuotaExhausted, QuotaExceededError):
            # Keep the page token: tomorrow resumes this video mid-way.
            queue.pause(task, cursor=task.cursor)
            result.stopped_on_quota = True
            break
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            queue.fail(task, str(exc))
            result.errors += 1
            logger.warning("comments %s failed: %s", video_id, exc)

        session.commit()

    session.commit()
    return result


def monthly_counts(session: Session, video_id: str) -> dict[str, int]:
    """Comments per ``YYYY-MM`` for one video: the input to the attention score."""
    rows = session.scalars(
        select(Comment.comment_published_at).where(Comment.video_id == video_id)
    )
    counts: dict[str, int] = {}
    for published in rows:
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        key = published.astimezone(UTC).strftime("%Y-%m")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _now() -> datetime:
    return datetime.now(UTC)
