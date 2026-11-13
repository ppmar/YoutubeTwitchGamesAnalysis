"""Daily quota ledger (SPEC section 8).

Two independent budgets:

- ``search.list`` has its own counter, 100 calls a day, and every page is a call.
  This is the bottleneck of the whole project.
- Everything else shares 10 000 units a day.

Both reset at midnight Pacific time, so the ledger is keyed by a Pacific date and
never by a UTC one. Every request actually sent is counted, including the ones that
come back as errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ytcollector.models import QuotaLedger, utcnow

PACIFIC = ZoneInfo("America/Los_Angeles")

Budget = Literal["search", "units"]


class QuotaExhausted(RuntimeError):
    """Raised instead of sending a request that the day's budget cannot pay for."""

    def __init__(self, budget: Budget, remaining: int, needed: int) -> None:
        super().__init__(f"{budget} budget exhausted: {remaining} left, {needed} needed")
        self.budget = budget
        self.remaining = remaining
        self.needed = needed


@dataclass(frozen=True)
class QuotaLimits:
    """Daily budgets, read from ``config/settings.yml``."""

    search_calls_per_day: int = 100
    units_per_day: int = 10_000
    safety_margin: float = 0.05
    units_reserve: int = 1_000

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> QuotaLimits:
        quota = settings.get("quota", {})
        return cls(
            search_calls_per_day=int(quota.get("search_calls_per_day", 100)),
            units_per_day=int(quota.get("units_per_day", 10_000)),
            safety_margin=float(quota.get("safety_margin", 0.05)),
            units_reserve=int(quota.get("units_reserve", 1_000)),
        )

    @property
    def usable_search_calls(self) -> int:
        return int(self.search_calls_per_day * (1 - self.safety_margin))

    @property
    def usable_units(self) -> int:
        return int(self.units_per_day * (1 - self.safety_margin))


def pacific_date(moment: datetime | None = None) -> str:
    """Return the quota day as ``YYYY-MM-DD`` in Pacific time.

    Using UTC here would shift the reset by seven or eight hours depending on the
    season, and silently spend the next day's budget.
    """
    moment = moment or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(PACIFIC).date().isoformat()


class QuotaTracker:
    """Reads and writes the daily counters.

    Every method takes an optional ``now`` so tests can cross a day boundary
    without touching the clock.
    """

    def __init__(self, session: Session, limits: QuotaLimits | None = None) -> None:
        self.session = session
        self.limits = limits or QuotaLimits()

    def ledger(self, day: str | None = None, now: datetime | None = None) -> QuotaLedger:
        """Return today's row, creating it on first use."""
        day = day or pacific_date(now)
        row = self.session.get(QuotaLedger, day)
        if row is None:
            row = QuotaLedger(quota_date=day, search_calls=0, units=0)
            self.session.add(row)
            self.session.flush()
        return row

    # Search budget ---------------------------------------------------------

    def remaining_search_calls(self, now: datetime | None = None) -> int:
        row = self.ledger(now=now)
        if row.search_exhausted:
            return 0
        return max(0, self.limits.usable_search_calls - row.search_calls)

    def can_search(self, pages: int = 1, now: datetime | None = None) -> bool:
        return self.remaining_search_calls(now) >= pages

    def check_search_call(self, pages: int = 1, now: datetime | None = None) -> None:
        """Raise :class:`QuotaExhausted` rather than send a call we cannot pay for."""
        remaining = self.remaining_search_calls(now)
        if remaining < pages:
            raise QuotaExhausted("search", remaining, pages)

    def record_search_call(self, pages: int = 1, now: datetime | None = None) -> None:
        """Count calls that were actually sent, successful or not."""
        row = self.ledger(now=now)
        row.search_calls += pages
        row.updated_at = utcnow()
        self.session.flush()

    # Unit budget -----------------------------------------------------------

    def remaining_units(self, reserve: int = 0, now: datetime | None = None) -> int:
        row = self.ledger(now=now)
        if row.units_exhausted:
            return 0
        return max(0, self.limits.usable_units - row.units - reserve)

    def check_units(self, units: int, reserve: int = 0, now: datetime | None = None) -> None:
        remaining = self.remaining_units(reserve, now)
        if remaining < units:
            raise QuotaExhausted("units", remaining, units)

    def record_units(self, units: int, now: datetime | None = None) -> None:
        row = self.ledger(now=now)
        row.units += units
        row.updated_at = utcnow()
        self.session.flush()

    # The API's own verdict -------------------------------------------------

    def mark_exhausted(self, budget: Budget, now: datetime | None = None) -> None:
        """Record a 403 ``quotaExceeded`` so the rest of the day stops immediately.

        The API knows better than our counter: another client, a retry we did not
        see, or a cost we mispriced can empty the budget early.
        """
        row = self.ledger(now=now)
        if budget == "search":
            row.search_exhausted = True
        else:
            row.units_exhausted = True
        row.updated_at = utcnow()
        self.session.flush()

    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        row = self.ledger(now=now)
        return {
            "quota_date": row.quota_date,
            "search_calls": row.search_calls,
            "search_remaining": self.remaining_search_calls(now),
            "units": row.units,
            "units_remaining": self.remaining_units(now=now),
            "search_exhausted": row.search_exhausted,
            "units_exhausted": row.units_exhausted,
        }
