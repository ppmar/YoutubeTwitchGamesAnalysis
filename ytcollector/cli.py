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
) -> None:
    """Show pending slices, cost per quota counter and the estimated number of days."""
    _todo("plan", 3)


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
    _todo("search", 3)


@app.command()
def videos(study: Study = DEFAULT_STUDY, dry_run: DryRun = False) -> None:
    """Step 2: fetch video statistics."""
    _todo("videos", 3)


@app.command()
def channels(study: Study = DEFAULT_STUDY, dry_run: DryRun = False) -> None:
    """Step 3: fetch channel statistics."""
    _todo("channels", 3)


@app.command()
def comments(
    study: Study = DEFAULT_STUDY,
    games: Games = None,
    min_comments: Annotated[int, typer.Option("--min-comments")] = 30,
    max_threads_per_video: Annotated[int, typer.Option("--max-threads-per-video")] = 50_000,
    dry_run: DryRun = False,
) -> None:
    """Step 4: collect comment timestamps (dates only, no author and no text)."""
    _todo("comments", 4)


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
    _todo("refresh-stats", 3)


@app.command("build-tables")
def build_tables(study: Study = DEFAULT_STUDY) -> None:
    """Rebuild analysis tables from cached raw responses, with no network calls."""
    _todo("build-tables", 3)


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
    _todo("export", 3)


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
