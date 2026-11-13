"""A read-only web view of the collection.

SPEC section 13 puts visualisation out of scope; this was added on request. It
reads the collection database and never writes to it, and it never calls the
YouTube API, so it cannot spend quota.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ytcollector import tables
from ytcollector.config import load_settings, load_study
from ytcollector.db import session_scope
from ytcollector.games import load_twitch_months
from ytcollector.models import (
    ChannelSnapshot,
    Comment,
    Game,
    GameMonth,
    GameVideo,
    VideoSnapshot,
)
from ytcollector.planning import day_estimate
from ytcollector.quota import PACIFIC, QuotaLimits, QuotaTracker
from ytcollector.stages import search as search_stage
from ytcollector.tasks import TaskQueue

HERE = Path(__file__).resolve().parent
LEADERS = ["fortnite", "call-of-duty-warzone", "apex-legends", "pubg"]

app = FastAPI(title="Collecteur Procom", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def fr(value: Any) -> str:
    """Group thousands with a narrow no-break space, as French typography does."""
    try:
        return f"{int(value):,}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return "—"


templates.env.filters["fr"] = fr


def current_study() -> str:
    return os.environ.get("YTC_STUDY", "battle_royale")


@lru_cache(maxsize=1)
def twitch_frame() -> Any:
    """The Twitch export, parsed once per process rather than per request."""
    return load_twitch_months()


def next_quota_reset(now: datetime | None = None) -> datetime:
    """The next midnight in Pacific time, which is when both counters refill."""
    now = (now or datetime.now(UTC)).astimezone(PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(UTC)


def _scalar(session: Session, column: Any, *where: Any) -> int:
    query = select(func.count()).select_from(column)
    for clause in where:
        query = query.where(clause)
    return int(session.scalar(query) or 0)


def collect_state(session: Session, study: str) -> dict[str, Any]:
    """Everything the page shows, in one pass over the database."""
    settings = load_settings()
    config = load_study(study)
    limits = QuotaLimits.from_settings(settings)
    quota = QuotaTracker(session, limits).summary()

    roster = list(session.scalars(select(Game).where(Game.study == study)))
    enabled = [g for g in roster if g.enabled]

    planned = search_stage.plan(
        session, study,
        slicing=settings.get("search", {}).get("slicing", "month"),
        before=config.months_before_first,
        after=config.months_after_last,
    )
    slices_by_game: dict[str, int] = {}
    for item in planned:
        slices_by_game[item.game_id] = slices_by_game.get(item.game_id, 0) + 1

    queue = TaskQueue(session, study)
    progress = queue.progress("search")
    total_slices = max(len(planned), sum(progress.values()))
    done = progress["done"]

    videos = _scalar(session, VideoSnapshot.__table__)
    distinct_videos = int(
        session.scalar(select(func.count(func.distinct(VideoSnapshot.video_id)))) or 0
    )
    matched = int(
        session.scalar(
            select(func.count(func.distinct(VideoSnapshot.video_id))).where(
                VideoSnapshot.title_match.is_(True)
            )
        )
        or 0
    )

    months = tables.twitch_game_month(session, study, twitch_frame())
    series = []
    if not months.empty:
        names = {g.game_id: g.game_name for g in roster}
        for game_id, group in months.groupby("game_id"):
            points = group.sort_values("year_month")
            series.append({
                "game_id": game_id,
                "name": names.get(game_id, game_id),
                "leader": game_id in LEADERS,
                "points": [
                    [row.year_month, int(row.hours_watched)]
                    for row in points.itertuples()
                ],
            })

    attention = [
        {
            "game_id": row.game_id,
            "year": row.year,
            "month": row.month,
            "index": row.attention_index,
            "share_a": row.share_method_a,
            "videos": row.n_videos_published,
        }
        for row in session.scalars(
            select(GameMonth)
            .where(GameMonth.study == study, GameMonth.attention_index.isnot(None))
            .order_by(GameMonth.game_id, GameMonth.year, GameMonth.month)
        )
    ]

    now = datetime.now(UTC)
    reset = next_quota_reset(now)
    hours_left, minutes_left = divmod(int((reset - now).total_seconds()) // 60, 60)

    return {
        "study": study,
        "title": config.title,
        "generatedAt": now.isoformat(),
        # Rendered server-side so the page reads without JavaScript; the script
        # only turns the countdown into a live one.
        "generatedLabel": now.astimezone(PACIFIC).strftime("%d/%m/%Y à %H:%M")
        + " heure du Pacifique",
        "stateLabel": (
            ("collecte en cours" if done else "en veille")
            + f" · {quota['search_remaining']} appels restants"
            + f" sur {limits.usable_search_calls} aujourd'hui"
        ),
        "quota": {
            **quota,
            "searchPerDay": limits.usable_search_calls,
            "unitsPerDay": limits.usable_units,
            "resetsAt": reset.isoformat(),
            "resetsInLabel": f"{hours_left} h {minutes_left:02d}",
        },
        "progress": {
            "totalSlices": total_slices,
            "done": done,
            "pending": progress["pending"],
            "running": progress["running"],
            "failed": progress["failed"],
            "daysTotal": day_estimate(total_slices, limits.usable_search_calls),
            "daysRemaining": day_estimate(
                total_slices - done, limits.usable_search_calls
            ),
            "perDay": limits.usable_search_calls,
        },
        "counts": {
            "games": len(enabled),
            "gamesTotal": len(roster),
            "links": _scalar(session, GameVideo.__table__, GameVideo.study == study),
            "videos": distinct_videos,
            "snapshots": videos,
            "matched": matched,
            "channels": int(
                session.scalar(select(func.count(func.distinct(ChannelSnapshot.channel_id)))) or 0
            ),
            "comments": _scalar(session, Comment.__table__),
        },
        "games": [
            {
                "id": g.game_id,
                "name": g.game_name,
                "role": g.role,
                "enabled": g.enabled,
                "months": g.months_in_top200,
                "first": g.first_month,
                "last": g.last_month,
                "peak": g.peak_month,
                "rank": g.best_rank,
                "hours": g.hours_watched_total,
                "slices": slices_by_game.get(g.game_id, 0),
                "validFrom": g.valid_from,
            }
            for g in sorted(roster, key=lambda g: -(g.hours_watched_total or 0))
        ],
        "series": series,
        "attention": attention,
    }


@app.get("/api/state")
def api_state() -> JSONResponse:
    with session_scope() as session:
        return JSONResponse(collect_state(session, current_study()))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index(request: Request):  # type: ignore[no-untyped-def]
    with session_scope() as session:
        state = collect_state(session, current_study())
    return templates.TemplateResponse(request, "index.html", {"state": state})


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Run the server. Railway supplies the port in ``PORT``."""
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port or int(os.environ.get("PORT", "8000")),
        log_level="info",
        access_log=False,
    )
