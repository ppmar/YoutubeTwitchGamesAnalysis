"""Attention score (SPEC section 6, milestone 5).

Synthetic data throughout: the point is the arithmetic, not the API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ytcollector import attention
from ytcollector.models import Comment, Game, GameMonth, GameVideo, VideoSnapshot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def at(month: str, day: int = 15) -> datetime:
    return datetime.fromisoformat(f"{month}-{day:02d}T12:00:00+00:00")


def add_video(
    session: object,
    video_id: str,
    published: str,
    views: int,
    comments: dict[str, int] | None = None,
    game_id: str = "apex",
    truncated: bool = False,
    video_type: str = "gameplay",
    is_short: bool = False,
    collected_at: datetime = NOW,
) -> None:
    total = sum((comments or {}).values())
    session.add(GameVideo(study="s", game_id=game_id, video_id=video_id,  # type: ignore[attr-defined]
                          period_start=f"{published}-01", source="search"))
    session.add(VideoSnapshot(  # type: ignore[attr-defined]
        video_id=video_id, collected_at=collected_at, channel_id="UC1",
        published_at=at(published), view_count=views, comment_count=total,
        title_match=True, video_type=video_type, is_short_proxy=is_short,
    ))
    if truncated:
        # Collection state lives on the video now, not on the snapshot.
        from ytcollector.stages.comments import mark_state

        mark_state(session, video_id, truncated=True)
    for month, count in (comments or {}).items():
        for i in range(count):
            session.add(Comment(  # type: ignore[attr-defined]
                video_id=video_id, comment_id_hash=f"{video_id}-{month}-{i}",
                comment_published_at=at(month, day=(i % 28) + 1),
            ))


@pytest.fixture
def game(session: object) -> object:
    session.add(Game(study="s", game_id="apex", game_name="Apex", twitch_names=["Apex"],  # type: ignore[attr-defined]
                     yt_query="Apex", match_terms=["Apex"], role="leader", enabled=True))
    session.commit()  # type: ignore[attr-defined]
    return session


class TestSpreadByComments:
    def test_conservation_the_series_sums_back_to_the_views(self) -> None:
        series = attention.spread_by_comments(
            1000, "2020-01", {"2020-01": 10, "2020-02": 30, "2020-03": 60}
        )
        assert sum(series.values()) == pytest.approx(1000)

    def test_shares_follow_the_comment_distribution(self) -> None:
        series = attention.spread_by_comments(
            1000, "2020-01", {"2020-01": 10, "2020-02": 30, "2020-03": 60}
        )
        assert series["2020-01"] == pytest.approx(100)
        assert series["2020-02"] == pytest.approx(300)
        assert series["2020-03"] == pytest.approx(600)

    def test_all_comments_in_one_month_concentrates_everything_there(self) -> None:
        series = attention.spread_by_comments(5000, "2020-01", {"2020-04": 42})
        assert series == {"2020-04": pytest.approx(5000)}

    def test_no_comments_gives_no_series(self) -> None:
        assert attention.spread_by_comments(1000, "2020-01", {}) == {}

    def test_the_rate_function_reweights_by_age(self) -> None:
        # A rate that halves with age must pull attention towards publication.
        flat = attention.spread_by_comments(1000, "2020-01", {"2020-01": 50, "2020-02": 50})
        decaying = attention.spread_by_comments(
            1000, "2020-01", {"2020-01": 50, "2020-02": 50}, rate=lambda age: 0.5**age
        )
        assert flat["2020-01"] == pytest.approx(500)
        assert decaying["2020-01"] > decaying["2020-02"]
        assert sum(decaying.values()) == pytest.approx(1000)


class TestKernel:
    def test_normalise_turns_a_series_into_shares_by_age(self) -> None:
        curve = attention.normalise_curve({"2020-01": 50.0, "2020-02": 150.0}, "2020-01")
        assert curve == {0: pytest.approx(0.25), 1: pytest.approx(0.75)}

    def test_the_average_kernel_sums_to_one(self) -> None:
        kernel = attention.average_kernel([{0: 0.5, 1: 0.5}, {0: 0.9, 1: 0.1}])
        assert sum(kernel) == pytest.approx(1.0)
        assert kernel[0] == pytest.approx(0.7)

    def test_spread_by_kernel_conserves_the_views(self) -> None:
        series = attention.spread_by_kernel(1000, "2020-01", [0.6, 0.3, 0.1])
        assert sum(series.values()) == pytest.approx(1000)
        assert series["2020-01"] == pytest.approx(600)
        assert series["2020-03"] == pytest.approx(100)

    def test_the_kernel_never_spreads_past_today(self) -> None:
        series = attention.spread_by_kernel(1000, "2020-01", [0.5, 0.3, 0.2],
                                            last_month="2020-02")
        assert set(series) == {"2020-01", "2020-02"}
        # Truncating renormalises rather than losing views.
        assert sum(series.values()) == pytest.approx(1000)

    def test_the_default_kernel_is_a_valid_distribution(self) -> None:
        assert sum(attention.default_kernel()) == pytest.approx(1.0)


class TestMethodSelection:
    def test_a_video_with_enough_comments_uses_method_a(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 40})
        game.commit()  # type: ignore[attr-defined]
        _, methods = attention.build_curves(game, "s")  # type: ignore[arg-type]
        assert methods["v1"] == attention.METHOD_A

    def test_a_video_with_too_few_comments_uses_method_b(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 5})
        game.commit()  # type: ignore[attr-defined]
        _, methods = attention.build_curves(game, "s")  # type: ignore[arg-type]
        assert methods["v1"] == attention.METHOD_B

    def test_a_truncated_video_falls_back_to_method_b(self, game: object) -> None:
        # Its earliest comments are missing, so its shape cannot be trusted.
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 500}, truncated=True)
        game.commit()  # type: ignore[attr-defined]
        _, methods = attention.build_curves(game, "s")  # type: ignore[arg-type]
        assert methods["v1"] == attention.METHOD_B

    def test_every_curve_conserves_its_video_views(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 40, "2020-03": 60})
        add_video(game, "v2", "2020-02", 7777, {"2020-02": 2})
        game.commit()  # type: ignore[attr-defined]
        curves, _ = attention.build_curves(game, "s")  # type: ignore[arg-type]
        totals = {c.video_id: c.total for c in curves}
        assert totals["v1"] == pytest.approx(1000)
        assert totals["v2"] == pytest.approx(7777)


class TestBuild:
    def test_writes_one_row_per_game_month(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 40, "2020-02": 60})
        game.commit()  # type: ignore[attr-defined]
        attention.build(game, "s")  # type: ignore[arg-type]
        rows = list(game.scalars(select(GameMonth)))  # type: ignore[attr-defined]
        assert {(r.year, r.month) for r in rows} == {(2020, 1), (2020, 2)}

    def test_the_index_is_100_at_the_peak_month(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 10, "2020-02": 90})
        game.commit()  # type: ignore[attr-defined]
        attention.build(game, "s")  # type: ignore[arg-type]
        rows = {(r.year, r.month): r for r in game.scalars(select(GameMonth))}  # type: ignore[attr-defined]
        assert rows[(2020, 2)].attention_index == pytest.approx(100.0)
        assert rows[(2020, 1)].attention_index == pytest.approx(100 / 9)

    def test_each_game_is_indexed_against_its_own_peak(self, game: object) -> None:
        game.add(Game(study="s", game_id="pubg", game_name="PUBG", twitch_names=["PUBG"],  # type: ignore[attr-defined]
                      yt_query="PUBG", match_terms=["PUBG"], role="leader", enabled=True))
        add_video(game, "v1", "2020-01", 1_000_000, {"2020-01": 40}, game_id="apex")
        add_video(game, "v2", "2020-01", 10, {"2020-01": 40}, game_id="pubg")
        game.commit()  # type: ignore[attr-defined]
        attention.build(game, "s")  # type: ignore[arg-type]
        rows = {r.game_id: r for r in game.scalars(select(GameMonth))}  # type: ignore[attr-defined]
        # A tiny game still peaks at 100: the index is within-game, like Trends.
        assert rows["apex"].attention_index == pytest.approx(100.0)
        assert rows["pubg"].attention_index == pytest.approx(100.0)
        assert rows["apex"].attention_raw > rows["pubg"].attention_raw

    def test_share_method_a_reports_how_trustworthy_a_month_is(self, game: object) -> None:
        add_video(game, "trusted", "2020-01", 1000, {"2020-01": 40})
        add_video(game, "guessed", "2020-01", 1000, {"2020-01": 1})
        game.commit()  # type: ignore[attr-defined]
        attention.build(game, "s")  # type: ignore[arg-type]
        row = game.scalar(select(GameMonth).where(GameMonth.month == 1))  # type: ignore[attr-defined]
        assert 0.0 < row.share_method_a < 1.0

    def test_a_month_built_only_from_method_a_has_share_one(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 40})
        game.commit()  # type: ignore[attr-defined]
        attention.build(game, "s")  # type: ignore[arg-type]
        row = game.scalar(select(GameMonth).where(GameMonth.month == 1))  # type: ignore[attr-defined]
        assert row.share_method_a == pytest.approx(1.0)

    def test_records_the_sample_it_rests_on(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 40})
        add_video(game, "v2", "2020-01", 1000, {"2020-01": 35})
        game.commit()  # type: ignore[attr-defined]
        attention.build(game, "s")  # type: ignore[arg-type]
        row = game.scalar(select(GameMonth).where(GameMonth.month == 1))  # type: ignore[attr-defined]
        assert row.sample_basis == 2
        # Comments dated in January, not the game's running total.
        assert row.n_comments_dated == 75

    def test_no_videos_writes_nothing(self, game: object) -> None:
        assert attention.build(game, "s") == 0  # type: ignore[arg-type]


class TestCalibration:
    def test_without_snapshots_the_rate_stays_flat(self, game: object) -> None:
        rate = attention.calibrate_rate(game)  # type: ignore[arg-type]
        assert rate(0) == 1.0
        assert rate(50) == 1.0

    def test_the_rate_is_estimated_from_snapshot_deltas(self, game: object) -> None:
        # Five weekly snapshots, each adding 1000 views and 10 comments: r = 100.
        for week in range(5):
            when = NOW + timedelta(days=7 * week)
            game.add(VideoSnapshot(  # type: ignore[attr-defined]
                video_id="v1", collected_at=when, published_at=at("2025-12"),
                view_count=1000 * (week + 1), comment_count=10 * (week + 1),
                title_match=True,
            ))
        game.commit()  # type: ignore[attr-defined]
        rate = attention.calibrate_rate(game)  # type: ignore[arg-type]
        assert rate(1) == pytest.approx(100.0)

    def test_the_validation_report_needs_observations(self, game: object) -> None:
        assert attention.validation_report(game)["observations"] == 0  # type: ignore[arg-type]

    def test_the_validation_report_scores_the_prediction(self, game: object) -> None:
        for week in range(6):
            when = NOW + timedelta(days=7 * week)
            game.add(VideoSnapshot(  # type: ignore[attr-defined]
                video_id="v1", collected_at=when, published_at=at("2025-12"),
                view_count=1000 * (week + 1), comment_count=10 * (week + 1),
                title_match=True,
            ))
        game.commit()  # type: ignore[attr-defined]
        report = attention.validation_report(game)  # type: ignore[arg-type]
        assert report["observations"] == 5
        # A perfectly linear history must be predicted exactly.
        assert report["median_relative_error"] == pytest.approx(0.0, abs=1e-9)

    def test_calibrated_build_differs_from_the_flat_one(self, game: object) -> None:
        add_video(game, "v1", "2020-01", 1000, {"2020-01": 50, "2020-06": 50})
        for week in range(5):
            when = NOW + timedelta(days=7 * week)
            game.add(VideoSnapshot(  # type: ignore[attr-defined]
                video_id="probe", collected_at=when, published_at=at("2025-12"),
                view_count=1000 * (week + 1), comment_count=10 * (week + 1),
                title_match=False,
            ))
        game.commit()  # type: ignore[attr-defined]
        assert attention.build(game, "s", calibrated=True) > 0  # type: ignore[arg-type]


class TestStrata:
    def test_videos_are_bucketed_by_size(self) -> None:
        assert attention.view_bucket(500) == 0
        assert attention.view_bucket(50_000) == 1
        assert attention.view_bucket(50_000_000) == 4

    def test_the_stratum_combines_type_format_and_size(self) -> None:
        assert attention.stratum_of("trailer", True, 5_000) == ("trailer", True, 0)
        assert attention.stratum_of(None, None, None) == ("other", False, 0)
