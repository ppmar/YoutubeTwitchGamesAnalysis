"""Slice planning (SPEC section 5, step 1, milestone 3).

Planning decides how the 100 daily search calls are spent, so the arithmetic
matters more than almost anything else in the project.
"""

from __future__ import annotations

from ytcollector.models import Game
from ytcollector.planning import Slice, month_range, plan_slices, shift_month, slice_order


def make_game(**kwargs: object) -> Game:
    defaults = {
        "study": "s",
        "game_id": "g",
        "game_name": "G",
        "twitch_names": ["G"],
        "yt_query": '"G"',
        "match_terms": ["G"],
        "role": "follower",
        "enabled": True,
        "first_month": "2020-01",
        "last_month": "2020-03",
        "peak_month": "2020-02",
        "months_in_top200": 3,
    }
    defaults.update(kwargs)
    return Game(**defaults)  # type: ignore[arg-type]


class TestShiftMonth:
    def test_moves_forward(self) -> None:
        assert shift_month("2020-01", 2) == "2020-03"

    def test_moves_backward(self) -> None:
        assert shift_month("2020-01", -1) == "2019-12"

    def test_crosses_several_years(self) -> None:
        assert shift_month("2020-06", -18) == "2018-12"
        assert shift_month("2020-06", 18) == "2021-12"


class TestMonthRange:
    def test_is_inclusive_at_both_ends(self) -> None:
        assert month_range("2020-01", "2020-03") == ["2020-01", "2020-02", "2020-03"]

    def test_a_single_month(self) -> None:
        assert month_range("2020-01", "2020-01") == ["2020-01"]

    def test_an_inverted_range_is_empty(self) -> None:
        assert month_range("2020-03", "2020-01") == []


class TestPlanSlices:
    def test_window_extends_before_and_after(self) -> None:
        game = make_game(first_month="2020-01", last_month="2020-03")
        slices = plan_slices([game], slicing="month", before=12, after=6, today="2026-01")
        months = [s.period_start[:7] for s in slices]
        # 12 months of lead-in to test who moved first, 6 of tail to watch the decline.
        assert months[0] == "2019-01"
        assert months[-1] == "2020-09"
        assert len(months) == 12 + 3 + 6

    def test_the_window_never_runs_past_today(self) -> None:
        game = make_game(first_month="2024-01", last_month="2024-09")
        slices = plan_slices([game], slicing="month", before=0, after=6, today="2024-11")
        assert slices[-1].period_start[:7] == "2024-11"

    def test_yearly_slicing_gives_one_slice_per_year(self) -> None:
        game = make_game(first_month="2019-05", last_month="2021-02")
        slices = plan_slices([game], slicing="year", before=0, after=0, today="2026-01")
        assert [s.period_start for s in slices] == ["2019-01-01", "2020-01-01", "2021-01-01"]

    def test_no_slicing_gives_a_single_slice(self) -> None:
        game = make_game(first_month="2019-05", last_month="2021-02")
        slices = plan_slices([game], slicing="none", before=0, after=0, today="2026-01")
        assert len(slices) == 1
        assert slices[0].period_start == "2019-05-01"
        assert slices[0].period_end == "2021-03-01"

    def test_bounds_are_utc_timestamps_meeting_end_to_end(self) -> None:
        game = make_game(first_month="2020-01", last_month="2020-01")
        s = plan_slices([game], slicing="month", before=0, after=0, today="2026-01")[0]
        assert s.published_after == "2020-01-01T00:00:00Z"
        assert s.published_before == "2020-02-01T00:00:00Z"

    def test_a_game_without_twitch_months_is_skipped(self) -> None:
        game = make_game(first_month=None, last_month=None, months_in_top200=0)
        assert plan_slices([game], slicing="month", today="2026-01") == []

    def test_disabled_games_are_skipped(self) -> None:
        game = make_game(enabled=False)
        assert plan_slices([game], slicing="month", today="2026-01") == []


class TestSliceOrder:
    def test_long_running_games_come_first(self) -> None:
        small = make_game(game_id="small", months_in_top200=3)
        big = make_game(game_id="big", months_in_top200=90, first_month="2016-01",
                        last_month="2023-06", peak_month="2018-01")
        slices = slice_order(plan_slices([small, big], slicing="month", before=0, after=0,
                                         today="2026-01"))
        assert slices[0].game_id == "big"

    def test_each_game_runs_outwards_from_its_peak(self) -> None:
        game = make_game(first_month="2020-01", last_month="2020-05", peak_month="2020-03")
        ordered = slice_order(
            plan_slices([game], slicing="month", before=0, after=0, today="2026-01")
        )
        months = [s.period_start[:7] for s in ordered]
        # An interrupted collection must still hold the months that matter.
        assert months[0] == "2020-03"
        assert set(months[1:3]) == {"2020-02", "2020-04"}
        assert set(months[3:]) == {"2020-01", "2020-05"}

    def test_ordering_is_stable(self) -> None:
        game = make_game(first_month="2020-01", last_month="2020-05", peak_month="2020-03")
        planned = plan_slices([game], slicing="month", before=0, after=0, today="2026-01")
        assert [s.period_start for s in slice_order(planned)] == [
            s.period_start for s in slice_order(list(reversed(planned)))
        ]


class TestCost:
    def test_the_battle_royale_budget_is_what_the_spec_expects(self) -> None:
        # SPEC section 5 puts the full study at roughly 1200-1400 search calls.
        games = [
            make_game(game_id=f"g{i}", first_month="2018-01", last_month="2022-01",
                      peak_month="2020-01", months_in_top200=49)
            for i in range(4)
        ]
        slices = plan_slices(games, slicing="month", before=12, after=6, today="2026-01")
        assert len(slices) == 4 * (12 + 49 + 6)

    def test_pages_per_slice_multiplies_the_cost(self) -> None:
        game = make_game(first_month="2020-01", last_month="2020-03")
        slices = plan_slices([game], slicing="month", before=0, after=0, today="2026-01")
        assert Slice.total_calls(slices, pages_per_slice=2) == len(slices) * 2
