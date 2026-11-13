"""Turn the game roster into search slices (SPEC section 5, step 1).

A slice is one ``(game, period)`` pair, and one search.list call per page. With a
budget of 100 calls a day, planning is what decides how long the study takes, so
the ordering matters: if the collection is cut short, what was already gathered
must still be the useful part.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from ytcollector.models import Game

Slicing = str  # "none" | "year" | "month"


def shift_month(month: str, delta: int) -> str:
    """Shift a ``YYYY-MM`` string by a number of months."""
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(start: str, end: str) -> list[str]:
    """Every ``YYYY-MM`` from start to end, inclusive. Empty if inverted."""
    months: list[str] = []
    current = start
    while current <= end:
        months.append(current)
        current = shift_month(current, 1)
    return months


def _iso(month: str) -> str:
    return f"{month}-01T00:00:00Z"


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


@dataclass(frozen=True)
class Slice:
    """One unit of search work."""

    game_id: str
    period_start: str  # YYYY-MM-DD
    period_end: str  # YYYY-MM-DD, exclusive
    published_after: str
    published_before: str
    yt_query: str
    # Used only for ordering.
    months_in_top200: int = 0
    distance_from_peak: int = 0

    @property
    def task_key(self) -> str:
        return f"{self.game_id}:{self.period_start}"

    @staticmethod
    def total_calls(slices: Iterable[Slice], pages_per_slice: int = 1) -> int:
        return sum(1 for _ in slices) * pages_per_slice


def _periods(first: str, last: str, slicing: Slicing) -> list[tuple[str, str]]:
    """Return (start_month, end_month_exclusive) pairs for the chosen granularity."""
    if slicing == "none":
        return [(first, shift_month(last, 1))]
    if slicing == "year":
        years = range(int(first[:4]), int(last[:4]) + 1)
        return [(f"{y}-01", f"{y + 1}-01") for y in years]
    return [(m, shift_month(m, 1)) for m in month_range(first, last)]


def plan_slices(
    games: Iterable[Game],
    slicing: Slicing = "month",
    before: int = 12,
    after: int = 6,
    today: str | None = None,
) -> list[Slice]:
    """Build every slice the study needs.

    The window runs from ``first_month - before`` to ``last_month + after``: the
    lead-in is what makes the lead/lag question answerable, the tail is what shows
    the decline. Nothing is planned past the current month, since there is nothing
    there to find.
    """
    today = today or _current_month()
    planned: list[Slice] = []

    for game in games:
        if not game.enabled or not game.first_month or not game.last_month:
            continue

        first = shift_month(game.first_month, -before)
        last = min(shift_month(game.last_month, after), today)
        if first > last:
            continue

        peak = game.peak_month or game.first_month
        for start, end in _periods(first, last, slicing):
            planned.append(
                Slice(
                    game_id=game.game_id,
                    period_start=f"{start}-01",
                    period_end=f"{end}-01",
                    published_after=_iso(start),
                    published_before=_iso(end),
                    yt_query=game.yt_query,
                    months_in_top200=game.months_in_top200 or 0,
                    distance_from_peak=abs(_month_index(start) - _month_index(peak)),
                )
            )
    return planned


def _month_index(month: str) -> int:
    year, mon = (int(part) for part in month[:7].split("-"))
    return year * 12 + mon


def slice_order(slices: Iterable[Slice]) -> list[Slice]:
    """Order the work so an interrupted collection is still usable.

    Long-running games first — they carry the trend — and within a game, outwards
    from the peak month, because the peak and its neighbours are what every research
    question leans on.
    """
    return sorted(
        slices,
        key=lambda s: (-s.months_in_top200, s.game_id, s.distance_from_peak, s.period_start),
    )


def day_estimate(total_calls: int, calls_per_day: int) -> int:
    """How many days of quota the remaining work needs."""
    if calls_per_day <= 0:
        return 0
    return -(-total_calls // calls_per_day)
