"""Releases per genre, per month and per year — SPEC_GENRES section 8.

Supply is counted twice, because the two answers say different things:

* ``n_releases_weighted`` sums the membership shares, so a game that is a third
  of one genre and two thirds of another counts as a third and two thirds;
* ``n_releases_dominant`` counts whole games under their heaviest genre, which
  is what a reader expects from "how many games came out".

A game whose release date cannot be read is counted apart and never imputed: a
missing date is not a date in the middle of the dataset.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytcollector.genres.load import display

log = logging.getLogger(__name__)

SUPPLY_MONTH_FILE = "genre_supply_month.parquet"
SUPPLY_YEAR_FILE = "genre_supply_year.parquet"
# Steam dates arrive in several shapes: "2019-06-04", "Jun 4, 2019", "2019".
_ISO = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")
_YEAR_ONLY = re.compile(r"^(\d{4})$")
_TEXTUAL = re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})$")
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9})\s+(\d{4})$")
# The export carries a date for every game, so "missing" is written as a
# sentinel: 9999 on 46,892 rows of the November 2026 export (22% of the
# catalogue, games with no announced date), and the Unix epoch on a handful.
# Treating either as a release would invent a supply spike out of nothing.
MIN_YEAR = 1971
MAX_YEAR = 2100
MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


def parse_release(value: Any) -> tuple[int, int] | None:
    """``(year, month)`` from a Steam release date, or None when unreadable.

    A year with no month is not usable for a monthly series, so it is reported
    as unreadable rather than placed in January. So is a year outside
    ``MIN_YEAR..MAX_YEAR``, which is how the export spells "no date".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat", "tbа", "tba", "coming soon"}:
        return None

    match = _ISO.match(text)
    if match:
        return _checked(int(match.group(1)), int(match.group(2)))
    match = _TEXTUAL.match(text) or _MONTH_YEAR.match(text)
    if match:
        name = match.group(1)[:3].lower()
        year = int(match.group(3) if match.lastindex == 3 else match.group(2))
        return _checked(year, MONTHS.get(name, 0))
    return None


def _checked(year: int, month: int) -> tuple[int, int] | None:
    if not 1 <= month <= 12:
        return None
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    return year, month


@dataclass
class Supply:
    """The two tables, plus what could not be dated."""

    by_month: Any
    by_year: Any
    games_dated: int
    games_undated: int
    undated_weight: float


def genre_supply(model: Any) -> Supply:
    """Spread each game's release over the genres it belongs to."""
    import numpy as np
    import pandas as pd

    shares = np.asarray(model.shares)
    parsed = [parse_release(v) for v in model.rows["release_date"]]
    dated = np.array([p is not None for p in parsed])
    years = np.array([p[0] if p else 0 for p in parsed], dtype="int64")
    months = np.array([p[1] if p else 0 for p in parsed], dtype="int64")

    carries = shares.sum(axis=1) > 0
    dominant = shares.argmax(axis=1)

    rows, genres = np.nonzero(shares > 0)
    keep = dated[rows]
    frame = pd.DataFrame(
        {
            "genre_id": genres[keep].astype("int32"),
            "year": years[rows[keep]],
            "month": months[rows[keep]],
            "weight": shares[rows[keep], genres[keep]],
        }
    )
    weighted = (
        frame.groupby(["genre_id", "year", "month"], as_index=False)["weight"].sum()
        .rename(columns={"weight": "n_releases_weighted"})
    )

    whole = pd.DataFrame(
        {
            "genre_id": dominant[dated & carries].astype("int32"),
            "year": years[dated & carries],
            "month": months[dated & carries],
            "n_releases_dominant": 1,
        }
    )
    counted = whole.groupby(["genre_id", "year", "month"], as_index=False)[
        "n_releases_dominant"
    ].sum()

    by_month = (
        weighted.merge(counted, on=["genre_id", "year", "month"], how="outer")
        .fillna({"n_releases_weighted": 0.0, "n_releases_dominant": 0})
        .astype({"n_releases_dominant": "int64"})
        .sort_values(["genre_id", "year", "month"])
        .reset_index(drop=True)
    )
    by_month["n_releases_weighted"] = by_month["n_releases_weighted"].round(6)

    by_year = (
        by_month.groupby(["genre_id", "year"], as_index=False)[
            ["n_releases_weighted", "n_releases_dominant"]
        ]
        .sum()
        .sort_values(["genre_id", "year"])
        .reset_index(drop=True)
    )
    by_year["n_releases_weighted"] = by_year["n_releases_weighted"].round(6)

    undated = int((~dated).sum())
    log.info(
        "supply: %s game(s) dated, %s without a usable release date (never imputed)",
        f"{int(dated.sum()):,}", f"{undated:,}",
    )
    return Supply(
        by_month=by_month,
        by_year=by_year,
        games_dated=int(dated.sum()),
        games_undated=undated,
        undated_weight=float(shares[~dated].sum()),
    )


def write_supply(supply: Supply, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "month": output_dir / SUPPLY_MONTH_FILE,
        "year": output_dir / SUPPLY_YEAR_FILE,
    }
    supply.by_month.to_parquet(paths["month"], index=False)
    supply.by_year.to_parquet(paths["year"], index=False)
    log.info("written to %s: %s, %s", display(output_dir), SUPPLY_MONTH_FILE, SUPPLY_YEAR_FILE)
    return paths
