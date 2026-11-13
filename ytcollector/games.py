"""Step 0 — build the `games` table from a study file and the Twitch export.

No API call is made here (SPEC section 4). The Twitch export needs repairing before
anything can be grouped: broken accents, case duplicates and one nameless row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ytcollector.config import TWITCH_CSV_ENCODING, TWITCH_GAME_CSV, GameConfig, StudyConfig
from ytcollector.models import Game, utcnow
from ytcollector.textnorm import grouping_key, normalize_name

# Set by hand once the first results come back; never overwritten afterwards.
MANUAL_FIELDS = ("yt_query", "match_terms", "enabled")


@dataclass
class PrepareResult:
    """What one `prepare-games` run did."""

    study: str
    created: int = 0
    updated: int = 0
    enabled: int = 0
    unmatched: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unmatched is None:
            self.unmatched = []

    @property
    def total(self) -> int:
        return self.created + self.updated


def load_twitch_months(csv_path: Path | None = None) -> pd.DataFrame:
    """Read the Twitch export into one row per (label, month), ready for grouping.

    Returns the columns ``game`` (raw label), ``game_name`` (repaired), ``key``
    (case-insensitive grouping key), ``month`` (``YYYY-MM``), ``rank`` and
    ``hours_watched``.
    """
    path = Path(csv_path or TWITCH_GAME_CSV)
    frame = pd.read_csv(path, encoding=TWITCH_CSV_ENCODING)

    # One row of the export (2019-02, rank 162) has no game name at all.
    frame = frame[frame["Game"].notna()].copy()

    frame["game"] = frame["Game"]
    frame["game_name"] = frame["Game"].map(normalize_name)
    frame["key"] = frame["Game"].map(grouping_key)
    frame["month"] = (
        frame["Year"].astype(int).astype(str).str.zfill(4)
        + "-"
        + frame["Month"].astype(int).astype(str).str.zfill(2)
    )
    frame["rank"] = frame["Rank"].astype(int)
    frame["hours_watched"] = frame["Hours_watched"].astype("int64")

    return frame[["game", "game_name", "key", "month", "rank", "hours_watched"]].reset_index(drop=True)


def aggregate_game(twitch: pd.DataFrame, game: GameConfig) -> dict[str, Any]:
    """Aggregate every Twitch label of one game into a single set of figures.

    Labels are matched case-insensitively, so listing one casing in the study file
    catches the others. Months before ``valid_from`` are dropped: they belong to a
    different game that shared the Twitch category (Warzone / Modern Warfare).

    A game absent from a month is *not* a game with zero audience — it is below the
    200th rank (SPEC section 3). Missing months stay missing; they are never zero-filled.
    """
    keys = {grouping_key(name) for name in game.twitch_names}
    rows = twitch[twitch["key"].isin(keys)]
    if game.valid_from:
        rows = rows[rows["month"] >= game.valid_from]

    if rows.empty:
        return {
            "hours_watched_total": None,
            "months_in_top200": 0,
            "first_month": None,
            "last_month": None,
            "peak_month": None,
            "best_rank": None,
        }

    by_month = rows.groupby("month", as_index=False)["hours_watched"].sum()
    return {
        "hours_watched_total": int(rows["hours_watched"].sum()),
        "months_in_top200": int(rows["month"].nunique()),
        "first_month": str(rows["month"].min()),
        "last_month": str(rows["month"].max()),
        "peak_month": str(by_month.loc[by_month["hours_watched"].idxmax(), "month"]),
        "best_rank": int(rows["rank"].min()),
    }


def prepare_games(
    session: Session,
    study: StudyConfig,
    csv_path: Path | None = None,
) -> PrepareResult:
    """Upsert the study's games, merging on ``(study, game_id)``.

    Aggregates are recomputed on every run. ``yt_query``, ``match_terms`` and
    ``enabled`` are left alone once a row exists, so hand tuning survives.
    """
    twitch = load_twitch_months(csv_path)
    existing = {
        row.game_id: row
        for row in session.scalars(select(Game).where(Game.study == study.study))
    }

    result = PrepareResult(study=study.study)
    for config in study.games:
        aggregates = aggregate_game(twitch, config)
        row = existing.get(config.game_id)

        if row is None:
            row = Game(
                study=study.study,
                game_id=config.game_id,
                yt_query=config.yt_query,
                match_terms=list(config.match_terms),
                enabled=config.enabled,
            )
            session.add(row)
            result.created += 1
        else:
            result.updated += 1

        row.game_name = config.game_name
        row.twitch_names = list(config.twitch_names)
        row.role = config.role
        row.release_date = config.release_date
        row.valid_from = config.valid_from
        row.notes = config.notes
        for field, value in aggregates.items():
            setattr(row, field, value)
        row.updated_at = utcnow()

        if row.enabled:
            result.enabled += 1
            if not aggregates["months_in_top200"]:
                result.unmatched.append(config.game_id)

    session.commit()
    return result
