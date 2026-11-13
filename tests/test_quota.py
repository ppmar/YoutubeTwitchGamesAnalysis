"""Daily quota ledger (SPEC section 8, milestone 2).

The search counter is the bottleneck: 100 calls a day, reset at midnight Pacific.
Nothing here may ever let a 101st call out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ytcollector.quota import QuotaExhausted, QuotaLimits, QuotaTracker, pacific_date


class TestPacificDate:
    def test_uses_pacific_not_utc(self) -> None:
        # 07:00 UTC on 2026-01-15 is still 2026-01-14 in California (UTC-8).
        moment = datetime(2026, 1, 15, 7, 0, tzinfo=UTC)
        assert pacific_date(moment) == "2026-01-14"

    def test_rolls_over_at_pacific_midnight(self) -> None:
        before = datetime(2026, 1, 15, 7, 59, tzinfo=UTC)
        after = datetime(2026, 1, 15, 8, 1, tzinfo=UTC)
        assert pacific_date(before) == "2026-01-14"
        assert pacific_date(after) == "2026-01-15"

    def test_daylight_saving_shifts_the_boundary(self) -> None:
        # In July California is UTC-7, so the day turns over at 07:00 UTC.
        assert pacific_date(datetime(2026, 7, 15, 6, 59, tzinfo=UTC)) == "2026-07-14"
        assert pacific_date(datetime(2026, 7, 15, 7, 1, tzinfo=UTC)) == "2026-07-15"


@pytest.fixture
def limits() -> QuotaLimits:
    return QuotaLimits(search_calls_per_day=100, units_per_day=10_000, safety_margin=0.0)


@pytest.fixture
def tracker(session: object, limits: QuotaLimits) -> QuotaTracker:
    return QuotaTracker(session, limits)  # type: ignore[arg-type]


class TestSearchCounter:
    def test_starts_with_the_full_budget(self, tracker: QuotaTracker) -> None:
        assert tracker.remaining_search_calls() == 100

    def test_counts_every_call(self, tracker: QuotaTracker) -> None:
        for _ in range(10):
            tracker.record_search_call()
        assert tracker.remaining_search_calls() == 90

    def test_the_101st_call_is_refused(self, tracker: QuotaTracker) -> None:
        for _ in range(100):
            tracker.check_search_call()
            tracker.record_search_call()
        assert tracker.remaining_search_calls() == 0
        assert not tracker.can_search()
        with pytest.raises(QuotaExhausted):
            tracker.check_search_call()

    def test_a_page_counts_as_a_call(self, tracker: QuotaTracker) -> None:
        tracker.record_search_call(pages=3)
        assert tracker.remaining_search_calls() == 97

    def test_safety_margin_shrinks_the_budget(self, session: object) -> None:
        limits = QuotaLimits(search_calls_per_day=100, units_per_day=10_000, safety_margin=0.05)
        tracker = QuotaTracker(session, limits)  # type: ignore[arg-type]
        assert tracker.remaining_search_calls() == 95


class TestUnitCounter:
    def test_counts_units_separately_from_search(self, tracker: QuotaTracker) -> None:
        tracker.record_units(500)
        assert tracker.remaining_units() == 9_500
        assert tracker.remaining_search_calls() == 100

    def test_refuses_once_the_budget_is_gone(self, tracker: QuotaTracker) -> None:
        tracker.record_units(10_000)
        with pytest.raises(QuotaExhausted):
            tracker.check_units(1)

    def test_reserve_is_held_back_for_later_stages(self, tracker: QuotaTracker) -> None:
        tracker.record_units(9_200)
        # 800 units left, but 1000 are reserved for videos/channels/refresh-stats.
        assert tracker.remaining_units(reserve=1_000) == 0
        with pytest.raises(QuotaExhausted):
            tracker.check_units(1, reserve=1_000)
        tracker.check_units(1)  # without the reserve the call is still allowed


class TestDayBoundary:
    def test_counters_restart_on_the_next_pacific_day(self, tracker: QuotaTracker) -> None:
        day_one = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)  # Pacific 2026-01-15
        for _ in range(100):
            tracker.record_search_call(now=day_one)
        assert tracker.remaining_search_calls(now=day_one) == 0

        day_two = datetime(2026, 1, 16, 9, 0, tzinfo=UTC)
        assert tracker.remaining_search_calls(now=day_two) == 100

    def test_each_day_keeps_its_own_row(self, tracker: QuotaTracker) -> None:
        tracker.record_search_call(now=datetime(2026, 1, 15, 9, 0, tzinfo=UTC))
        tracker.record_search_call(now=datetime(2026, 1, 16, 9, 0, tzinfo=UTC))
        assert tracker.ledger("2026-01-15").search_calls == 1
        assert tracker.ledger("2026-01-16").search_calls == 1


class TestApiSaysExhausted:
    def test_marking_search_exhausted_stops_the_day(self, tracker: QuotaTracker) -> None:
        tracker.mark_exhausted("search")
        assert not tracker.can_search()
        with pytest.raises(QuotaExhausted):
            tracker.check_search_call()
        # Units are a separate budget and stay usable.
        tracker.check_units(1)

    def test_marking_units_exhausted_stops_only_units(self, tracker: QuotaTracker) -> None:
        tracker.mark_exhausted("units")
        with pytest.raises(QuotaExhausted):
            tracker.check_units(1)
        tracker.check_search_call()

    def test_the_flag_does_not_leak_into_the_next_day(self, tracker: QuotaTracker) -> None:
        day_one = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
        tracker.mark_exhausted("search", now=day_one)
        assert not tracker.can_search(now=day_one)
        assert tracker.can_search(now=datetime(2026, 1, 16, 9, 0, tzinfo=UTC))
