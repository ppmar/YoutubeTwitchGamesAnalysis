"""Milestone C0 of SPEC_CALIBRATION: three defects that quietly skew the score.

Each test here failed before the fix. They are kept together because they share a
cause — collection state was stored on a snapshot row, which a later snapshot
silently reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import responses
from sqlalchemy import select

from ytcollector import attention
from ytcollector import daily as daily_module
from ytcollector.client import API_ROOT, YouTubeClient
from ytcollector.models import (
    Comment,
    Game,
    GameMonth,
    GameVideo,
    VideoCommentState,
    VideoSnapshot,
)
from ytcollector.quota import QuotaLimits, QuotaTracker
from ytcollector.stages import comments as comments_stage
from ytcollector.stages import videos as videos_stage

NOW = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "TEST-KEY"


@pytest.fixture
def tracker(session: object) -> QuotaTracker:
    return QuotaTracker(session, QuotaLimits(100, 10_000, 0.0))  # type: ignore[arg-type]


@pytest.fixture
def client(session: object, tracker: QuotaTracker) -> YouTubeClient:
    return YouTubeClient(session=session, tracker=tracker, api_key=KEY, backoff_factor=0.0)  # type: ignore[arg-type]


@pytest.fixture
def game(session: object) -> object:
    session.add(Game(  # type: ignore[attr-defined]
        study="s", game_id="apex", game_name="Apex Legends", twitch_names=["Apex Legends"],
        yt_query='"Apex Legends"', match_terms=["Apex Legends"], role="leader", enabled=True,
    ))
    session.commit()  # type: ignore[attr-defined]
    return session


def add_video(
    session: object,
    video_id: str,
    collected_at: datetime = NOW,
    comment_count: int = 500,
    published: str = "2020-01",
) -> None:
    session.add(GameVideo(study="s", game_id="apex", video_id=video_id,  # type: ignore[attr-defined]
                          period_start=f"{published}-01", source="search",
                          title_match=True))
    session.add(VideoSnapshot(  # type: ignore[attr-defined]
        video_id=video_id, collected_at=collected_at, channel_id="UC1",
        published_at=datetime.fromisoformat(f"{published}-05T00:00:00+00:00"),
        view_count=10_000, comment_count=comment_count,
        video_type="gameplay", is_short_proxy=False,
    ))


def add_comments(session: object, video_id: str, months: dict[str, int]) -> None:
    for month, count in months.items():
        for i in range(count):
            session.add(Comment(  # type: ignore[attr-defined]
                video_id=video_id, comment_id_hash=f"{video_id}-{month}-{i}",
                comment_published_at=datetime.fromisoformat(f"{month}-02T12:00:00+00:00"),
            ))


class TestTruncationSurvivesRefresh:
    """3.1 — a truncated video must never go back to method A.

    The flag used to live on `video_snapshots`, and `refresh-stats` writes a new
    snapshot with the column back at its default. The truncated video then looked
    complete again, and its amputated history — missing exactly the early months —
    was fed to method A.
    """

    def test_the_state_table_keeps_the_flag(self, game: object) -> None:
        add_video(game, "v1")
        game.commit()  # type: ignore[attr-defined]
        comments_stage.mark_state(game, "v1", truncated=True)  # type: ignore[arg-type]
        game.commit()  # type: ignore[attr-defined]

        state = game.get(VideoCommentState, "v1")  # type: ignore[attr-defined]
        assert state.truncated is True

    @responses.activate
    def test_a_refresh_does_not_clear_it(
        self, game: object, client: YouTubeClient
    ) -> None:
        add_video(game, "v1")
        game.commit()  # type: ignore[attr-defined]
        comments_stage.mark_state(game, "v1", truncated=True)  # type: ignore[arg-type]
        game.commit()  # type: ignore[attr-defined]

        responses.add(responses.GET, f"{API_ROOT}/videos", json={"items": [{
            "id": "v1",
            "snippet": {"channelId": "UC1", "title": "Apex Legends",
                        "publishedAt": "2020-01-05T00:00:00Z"},
            "contentDetails": {"duration": "PT10M"},
            "statistics": {"viewCount": "20000", "commentCount": "600"},
        }]})
        videos_stage.run(game, client, "s", video_ids=["v1"])  # type: ignore[arg-type]

        assert game.get(VideoCommentState, "v1").truncated is True  # type: ignore[attr-defined]

    def test_a_truncated_video_stays_on_method_b_after_a_refresh(
        self, game: object
    ) -> None:
        add_video(game, "v1")
        add_comments(game, "v1", {"2020-01": 60})
        game.commit()  # type: ignore[attr-defined]
        comments_stage.mark_state(game, "v1", truncated=True)  # type: ignore[arg-type]
        # A later snapshot, as refresh-stats would write.
        game.add(VideoSnapshot(  # type: ignore[attr-defined]
            video_id="v1", collected_at=NOW + timedelta(days=7), channel_id="UC1",
            published_at=datetime(2020, 1, 5, tzinfo=UTC),
            view_count=20_000, comment_count=600,
            video_type="gameplay", is_short_proxy=False,
        ))
        game.commit()  # type: ignore[attr-defined]

        _, methods = attention.build_curves(game, "s")  # type: ignore[arg-type]

        assert methods["v1"] == attention.METHOD_B

    def test_disabled_comments_also_survive(self, game: object) -> None:
        add_video(game, "v1")
        game.commit()  # type: ignore[attr-defined]
        comments_stage.mark_state(game, "v1", disabled=True)  # type: ignore[arg-type]
        game.commit()  # type: ignore[attr-defined]

        assert comments_stage.eligible_videos(game, "s") == []  # type: ignore[arg-type]


class TestWeeklyRefreshFires:
    """3.2 — the refresh never triggered while discovery was still running.

    `needs_refresh` looked at the newest snapshot in the whole table. Search
    creates fresh snapshots every day, so that maximum was always recent and no
    video was ever re-snapshotted — which also means the calibration data would
    never exist.
    """

    def test_a_stale_video_is_due_even_beside_a_fresh_one(self, game: object) -> None:
        old = datetime.now(UTC) - timedelta(days=9)
        add_video(game, "old", collected_at=old)
        add_video(game, "fresh", collected_at=datetime.now(UTC))
        game.commit()  # type: ignore[attr-defined]

        due = daily_module.videos_due_for_refresh(game, "s", after_days=7)  # type: ignore[arg-type]

        assert due == ["old"]

    def test_nothing_is_due_when_every_snapshot_is_recent(self, game: object) -> None:
        now = datetime.now(UTC)
        add_video(game, "a", collected_at=now)
        add_video(game, "b", collected_at=now - timedelta(days=2))
        game.commit()  # type: ignore[attr-defined]
        assert daily_module.videos_due_for_refresh(game, "s", after_days=7) == []  # type: ignore[arg-type]

    def test_a_video_refreshed_yesterday_is_not_due_again(self, game: object) -> None:
        old = datetime.now(UTC) - timedelta(days=30)
        add_video(game, "v1", collected_at=old)
        game.add(VideoSnapshot(  # type: ignore[attr-defined]
            video_id="v1", collected_at=datetime.now(UTC) - timedelta(days=1),
            channel_id="UC1",
        ))
        game.commit()  # type: ignore[attr-defined]
        assert daily_module.videos_due_for_refresh(game, "s", after_days=7) == []  # type: ignore[arg-type]

    def test_the_window_is_configurable(self, game: object) -> None:
        add_video(game, "v1", collected_at=datetime.now(UTC) - timedelta(days=3))
        game.commit()  # type: ignore[attr-defined]
        assert daily_module.videos_due_for_refresh(game, "s", after_days=7) == []  # type: ignore[arg-type]
        assert daily_module.videos_due_for_refresh(game, "s", after_days=2) == ["v1"]  # type: ignore[arg-type]


class TestCommentsDatedPerMonth:
    """3.3 — `n_comments_dated` carried the game's total on every row.

    The column is meant to say how many comments are dated *in that month*, which
    is what tells a reader whether the month rests on anything.
    """

    def test_each_month_reports_its_own_count(self, game: object) -> None:
        add_video(game, "v1")
        add_comments(game, "v1", {"2020-01": 40, "2020-03": 10})
        game.commit()  # type: ignore[attr-defined]

        attention.build(game, "s")  # type: ignore[arg-type]

        rows = {
            (r.year, r.month): r.n_comments_dated
            for r in game.scalars(select(GameMonth))  # type: ignore[attr-defined]
        }
        assert rows[(2020, 1)] == 40
        assert rows[(2020, 3)] == 10

    def test_a_month_carrying_attention_but_no_comment_reports_zero(
        self, game: object
    ) -> None:
        # v1 is method A over three months, so the kernel has mass at ages 0, 1
        # and 2. v2 is method B and is published later, so the kernel carries it
        # into months that hold no dated comment of their own.
        add_video(game, "v1", published="2020-01")
        add_comments(game, "v1", {"2020-01": 40, "2020-02": 15, "2020-03": 5})
        add_video(game, "v2", comment_count=5, published="2020-06")
        add_comments(game, "v2", {"2020-06": 5})
        game.commit()  # type: ignore[attr-defined]

        attention.build(game, "s")  # type: ignore[arg-type]

        rows = {
            (r.year, r.month): r.n_comments_dated
            for r in game.scalars(select(GameMonth))  # type: ignore[attr-defined]
        }
        assert rows[(2020, 1)] == 40
        assert rows[(2020, 6)] == 5
        later = [v for k, v in rows.items() if k in {(2020, 7), (2020, 8)}]
        assert later, "the kernel should have carried v2 into later months"
        assert all(v == 0 for v in later)

    def test_counts_are_not_all_identical(self, game: object) -> None:
        # The symptom that gave the bug away: the same number on every row.
        add_video(game, "v1")
        add_comments(game, "v1", {"2020-01": 40, "2020-02": 25, "2020-05": 7})
        game.commit()  # type: ignore[attr-defined]

        attention.build(game, "s")  # type: ignore[arg-type]

        counts = {
            r.n_comments_dated
            for r in game.scalars(select(GameMonth))  # type: ignore[attr-defined]
        }
        assert len(counts) > 1
