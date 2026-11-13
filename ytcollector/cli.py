"""Command-line entry point. Command surface follows SPEC section 9.

Every command is a stub at milestone 0; each is filled in by its own milestone.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

app = typer.Typer(
    name="ytc",
    help="YouTube collector for the Procom Steam/Twitch trend study.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_STUDY = os.environ.get("YTC_STUDY", "battle_royale")

# Stages that put work on the task queue, in pipeline order.
STAGE_KINDS = ("search", "videos", "channels", "comments", "panel")

def _runtime(study: str):
    """Open a session and build the client, tracker and settings the stages need."""
    from ytcollector.client import YouTubeClient
    from ytcollector.config import load_settings, youtube_api_key
    from ytcollector.db import session_scope
    from ytcollector.quota import QuotaLimits, QuotaTracker

    settings = load_settings()
    scope = session_scope()
    session = scope.__enter__()
    tracker = QuotaTracker(session, QuotaLimits.from_settings(settings))
    client = YouTubeClient(
        session=session,
        tracker=tracker,
        api_key=youtube_api_key(),
        timeout=int(settings.get("http", {}).get("timeout_seconds", 30)),
        max_retries=int(settings.get("http", {}).get("max_retries", 5)),
        backoff_factor=float(settings.get("http", {}).get("backoff_factor", 2.0)),
    )
    return scope, session, client, settings


def _ids(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None



Study = Annotated[str, typer.Option("--study", help="Study configuration to use.")]
Games = Annotated[str | None, typer.Option("--games", help="Comma-separated game ids.")]
Limit = Annotated[int | None, typer.Option("--limit", help="Stop after N tasks.")]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Emit no request; report what would run.")]


def _todo(command: str, milestone: int) -> None:
    raise NotImplementedError(f"`ytc {command}` lands in milestone {milestone} (see docs/SPEC.md).")


@app.command("prepare-games")
def prepare_games(study: Study = DEFAULT_STUDY) -> None:
    """Step 0: build the `games` table from the study file and the Twitch CSV."""
    from ytcollector.config import load_study
    from ytcollector.db import session_scope
    from ytcollector.games import prepare_games as run

    config = load_study(study)
    with session_scope() as session:
        result = run(session, config)

    typer.echo(
        f"{result.study}: {result.total} games "
        f"({result.created} created, {result.updated} updated), "
        f"{result.enabled} enabled"
    )
    if result.unmatched:
        typer.echo(f"warning: no Twitch rows for {', '.join(result.unmatched)}", err=True)


@app.command()
def plan(
    study: Study = DEFAULT_STUDY,
    slicing: Annotated[str, typer.Option("--slicing")] = "month",
    games: Games = None,
    limit: Limit = None,
    pages_per_slice: Annotated[int, typer.Option("--pages-per-slice")] = 1,
) -> None:
    """Show pending slices, cost per quota counter and the estimated number of days."""
    from ytcollector.config import load_settings, load_study
    from ytcollector.db import session_scope
    from ytcollector.planning import day_estimate
    from ytcollector.quota import QuotaLimits
    from ytcollector.stages import search as search_stage
    from ytcollector.tasks import TaskQueue

    config = load_study(study)
    limits = QuotaLimits.from_settings(load_settings())

    with session_scope() as session:
        slices = search_stage.plan(
            session, study, slicing=slicing,
            before=config.months_before_first, after=config.months_after_last,
            games=_ids(games),
        )
        if limit:
            slices = slices[:limit]
        done = TaskQueue(session, study).count("search", status="done")

    calls = len(slices) * pages_per_slice
    remaining = max(0, calls - done)
    typer.echo(f"study: {study}  slicing: {slicing}  pages per slice: {pages_per_slice}")
    typer.echo(f"slices: {len(slices)}   search calls: {calls}   already done: {done}")
    typer.echo(
        f"remaining: {remaining} search calls  "
        f"= {day_estimate(remaining, limits.usable_search_calls)} days "
        f"at {limits.usable_search_calls}/day"
    )
    typer.echo(
        "units: videos+channels are about "
        f"{-(-len(slices) * 50 // 50)} units, well inside {limits.usable_units}/day"
    )


@app.command()
def search(
    study: Study = DEFAULT_STUDY,
    slicing: Annotated[str, typer.Option("--slicing")] = "month",
    games: Games = None,
    limit: Limit = None,
    pages_per_slice: Annotated[int, typer.Option("--pages-per-slice")] = 1,
    dry_run: DryRun = False,
) -> None:
    """Step 1: discover videos with search.list."""
    from ytcollector.config import load_study
    from ytcollector.stages import search as search_stage
    from ytcollector.tasks import TaskQueue

    config = load_study(study)
    scope, session, client, _ = _runtime(study)
    try:
        queue = TaskQueue(session, study)
        queue.recover_orphans("search")
        slices = search_stage.plan(
            session, study, slicing=slicing,
            before=config.months_before_first, after=config.months_after_last,
            games=_ids(games),
        )
        search_stage.enqueue(queue, slices, pages_per_slice=pages_per_slice)
        session.commit()
        result = search_stage.run(session, client, study, limit=limit, dry_run=dry_run)
    finally:
        scope.__exit__(None, None, None)

    typer.echo(
        f"search: {result.processed} slices, {result.items} videos, {result.calls} calls"
        + (" (quota exhausted)" if result.stopped_on_quota else "")
    )


@app.command()
def videos(study: Study = DEFAULT_STUDY, dry_run: DryRun = False) -> None:
    """Step 2: fetch video statistics."""
    from ytcollector.stages import videos as videos_stage

    scope, session, client, _ = _runtime(study)
    try:
        result = videos_stage.run(session, client, study, dry_run=dry_run)
    finally:
        scope.__exit__(None, None, None)
    typer.echo(f"videos: {result.items} snapshots, {result.calls} calls")


@app.command()
def channels(study: Study = DEFAULT_STUDY, dry_run: DryRun = False) -> None:
    """Step 3: fetch channel statistics."""
    from ytcollector.stages import channels as channels_stage

    scope, session, client, _ = _runtime(study)
    try:
        result = channels_stage.run(session, client, study, dry_run=dry_run)
    finally:
        scope.__exit__(None, None, None)
    typer.echo(f"channels: {result.items} snapshots, {result.calls} calls")


@app.command()
def comments(
    study: Study = DEFAULT_STUDY,
    games: Games = None,
    min_comments: Annotated[int, typer.Option("--min-comments")] = 30,
    max_threads_per_video: Annotated[int, typer.Option("--max-threads-per-video")] = 50_000,
    dry_run: DryRun = False,
) -> None:
    """Step 4: collect comment timestamps (dates only, no author and no text)."""
    from ytcollector.stages import comments as comments_stage
    from ytcollector.tasks import TaskQueue

    scope, session, client, settings = _runtime(study)
    try:
        TaskQueue(session, study).recover_orphans("comments")
        result = comments_stage.run(
            session, client, study,
            min_comments=min_comments,
            max_threads_per_video=max_threads_per_video,
            games=_ids(games),
            reserve=int(settings.get("quota", {}).get("units_reserve", 1000)),
            dry_run=dry_run,
        )
    finally:
        scope.__exit__(None, None, None)

    typer.echo(
        f"comments: {result.processed} videos, {result.items} timestamps"
        + (" (unit reserve reached)" if result.stopped_on_quota else "")
    )


@app.command("channel-panel")
def channel_panel(
    study: Study = DEFAULT_STUDY,
    min_subscribers: Annotated[int, typer.Option("--min-subscribers")] = 100_000,
    dry_run: DryRun = False,
) -> None:
    """Step 5 (phase 2): collect uploads of the fixed channel panel."""
    _todo("channel-panel", 7)


@app.command("refresh-stats")
def refresh_stats(study: Study = DEFAULT_STUDY) -> None:
    """Append a dated snapshot of statistics for every known video."""
    from sqlalchemy import select

    from ytcollector.models import GameVideo
    from ytcollector.stages import videos as videos_stage

    scope, session, client, _ = _runtime(study)
    try:
        ids = list(
            session.scalars(
                select(GameVideo.video_id).where(GameVideo.study == study).distinct()
            )
        )
        result = videos_stage.run(session, client, study, video_ids=ids)
    finally:
        scope.__exit__(None, None, None)
    typer.echo(f"refresh-stats: {result.items} new snapshots over {len(ids)} videos")


@app.command("build-tables")
def build_tables(study: Study = DEFAULT_STUDY) -> None:
    """Rebuild analysis tables from cached raw responses, with no network calls."""
    from ytcollector import tables
    from ytcollector.db import session_scope

    with session_scope() as session:
        counts = tables.build(session, study)
        conflicts = tables.join_key_conflicts(session, study)

    for table, rows in counts.items():
        typer.echo(f"{table}: {rows} rows")
    if conflicts:
        typer.echo(f"error: {conflicts} duplicate (game_id, year, month) keys", err=True)
        raise typer.Exit(1)


@app.command("build-attention")
def build_attention(
    study: Study = DEFAULT_STUDY,
    calibrated: Annotated[bool, typer.Option("--calibrated/--no-calibrated")] = False,
) -> None:
    """Compute the monthly attention score per game (SPEC section 6)."""
    _todo("build-attention", 5)


@app.command()
def export(
    study: Study = DEFAULT_STUDY,
    format: Annotated[str, typer.Option("--format")] = "parquet",
    out: Annotated[str, typer.Option("--out")] = "exports",
) -> None:
    """Export the analysis tables to Parquet or CSV."""
    from pathlib import Path

    import pandas as pd
    from sqlalchemy import select

    from ytcollector import tables
    from ytcollector.db import session_scope
    from ytcollector.models import GameMonth

    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)

    with session_scope() as session:
        frames = {
            "games": tables.games_frame(session, study),
            "yt_game_month": pd.DataFrame(
                [
                    {c.name: getattr(r, c.name) for c in GameMonth.__table__.columns}
                    for r in session.scalars(select(GameMonth).where(GameMonth.study == study))
                ]
            ),
        }

    for name, frame in frames.items():
        path = directory / f"{study}_{name}.{'parquet' if format == 'parquet' else 'csv'}"
        if format == "parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False)
        typer.echo(f"{path}: {len(frame)} rows")


@app.command()
def status(study: Study = DEFAULT_STUDY) -> None:
    """Show remaining quota and progress per stage."""
    from sqlalchemy import func, select

    from ytcollector.config import load_settings
    from ytcollector.db import session_scope
    from ytcollector.models import Game
    from ytcollector.quota import QuotaLimits, QuotaTracker
    from ytcollector.tasks import TaskQueue

    limits = QuotaLimits.from_settings(load_settings())
    with session_scope() as session:
        quota = QuotaTracker(session, limits).summary()
        games = session.scalar(
            select(func.count()).select_from(Game).where(Game.study == study, Game.enabled)
        )
        queue = TaskQueue(session, study)
        stages = {kind: queue.progress(kind) for kind in STAGE_KINDS}

    typer.echo(f"study: {study}  enabled games: {games or 0}")
    typer.echo(
        f"quota {quota['quota_date']} (Pacific): "
        f"search {quota['search_remaining']} left of {quota['search_calls'] + quota['search_remaining']}, "
        f"units {quota['units_remaining']} left"
    )
    for kind, counts in stages.items():
        if any(counts.values()):
            typer.echo(
                f"  {kind:<14} pending {counts['pending']:>5}  running {counts['running']:>3}"
                f"  done {counts['done']:>6}  failed {counts['failed']:>4}"
            )


@app.command()
def daily(study: Study = DEFAULT_STUDY, dry_run: DryRun = False) -> None:
    """Full daily run, launched by the Railway cron service."""
    _todo("daily", 6)


if __name__ == "__main__":
    app()
