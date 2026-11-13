"""The daily run (SPEC section 9, milestone 6).

The properties under test are operational, not analytical: it must finish, it must
not run twice, and an empty quota is not a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import responses

from ytcollector import daily as daily_module
from ytcollector.client import API_ROOT, YouTubeClient
from ytcollector.config import GameConfig, StudyConfig
from ytcollector.models import Game, GameVideo, VideoSnapshot
from ytcollector.quota import QuotaLimits, QuotaTracker
from ytcollector.tasks import TaskQueue

KEY = "TEST-KEY"
SETTINGS = {
    "search": {"slicing": "month", "pages_per_slice": 1},
    "comments": {"min_comments": 30, "max_threads_per_video": 1000},
    "quota": {"units_reserve": 1000},
}


@pytest.fixture
def tracker(session: object) -> QuotaTracker:
    return QuotaTracker(session, QuotaLimits(100, 10_000, 0.0))  # type: ignore[arg-type]


@pytest.fixture
def client(session: object, tracker: QuotaTracker) -> YouTubeClient:
    return YouTubeClient(session=session, tracker=tracker, api_key=KEY, backoff_factor=0.0)  # type: ignore[arg-type]


@pytest.fixture
def study() -> StudyConfig:
    return StudyConfig(
        study="s", title="S", months_before_first=0, months_after_last=0,
        games=[GameConfig(
            game_id="apex", game_name="Apex", twitch_names=["Apex"], yt_query='"Apex"',
            match_terms=["Apex"], role="leader",
        )],
    )


@pytest.fixture
def seeded(session: object) -> object:
    session.add(Game(  # type: ignore[attr-defined]
        study="s", game_id="apex", game_name="Apex", twitch_names=["Apex"],
        yt_query='"Apex"', match_terms=["Apex"], role="leader", enabled=True,
        first_month="2020-01", last_month="2020-02", peak_month="2020-01",
        months_in_top200=2,
    ))
    session.commit()  # type: ignore[attr-defined]
    return session


class TestDryRun:
    def test_emits_no_request_and_returns(
        self, seeded: object, client: YouTubeClient, study: StudyConfig
    ) -> None:
        with responses.RequestsMock() as mocked:
            summary = daily_module.run(seeded, client, study, SETTINGS, dry_run=True)  # type: ignore[arg-type]
            assert len(mocked.calls) == 0
        assert summary.dry_run
        assert summary.search_calls == 0
        assert summary.slices_remaining == 2

    def test_reports_the_days_still_needed(
        self, seeded: object, client: YouTubeClient, study: StudyConfig
    ) -> None:
        with responses.RequestsMock():
            summary = daily_module.run(seeded, client, study, SETTINGS, dry_run=True)  # type: ignore[arg-type]
        assert summary.days_remaining == 1
        assert "days_remaining=1" in summary.as_line()


class TestFullPass:
    @responses.activate
    def test_runs_every_stage_and_finishes(
        self, seeded: object, client: YouTubeClient, study: StudyConfig
    ) -> None:
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search", json={
                "pageInfo": {"totalResults": 10},
                "items": [{"id": {"videoId": "v1"}}],
            })
        responses.add(responses.GET, f"{API_ROOT}/videos", json={"items": [{
            "id": "v1",
            "snippet": {"channelId": "UC1", "title": "Apex gameplay",
                        "description": "apex", "publishedAt": "2020-01-15T00:00:00Z"},
            "contentDetails": {"duration": "PT10M"},
            "statistics": {"viewCount": "1000", "commentCount": "100"},
        }]})
        responses.add(responses.GET, f"{API_ROOT}/channels", json={"items": [{
            "id": "UC1", "snippet": {"title": "C"}, "statistics": {"subscriberCount": "10"},
        }]})
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", json={"items": [{
            "id": "c1",
            "snippet": {"totalReplyCount": 0, "topLevelComment": {
                "snippet": {"publishedAt": "2020-02-01T00:00:00Z", "likeCount": 1}}},
        }]})

        summary = daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]

        assert summary.search_slices == 2
        assert summary.videos == 1
        assert summary.channels == 1
        assert summary.comments == 1
        assert summary.game_months == 1
        assert summary.attention_rows >= 1
        assert summary.slices_remaining == 0

    @responses.activate
    def test_a_second_run_does_not_redo_the_work(
        self, seeded: object, client: YouTubeClient, study: StudyConfig
    ) -> None:
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search",
                          json={"pageInfo": {"totalResults": 1}, "items": []})
        daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]
        calls = len(responses.calls)

        second = daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]

        assert second.search_slices == 0
        assert len(responses.calls) == calls


class TestQuotaEnd:
    @responses.activate
    def test_an_exhausted_quota_is_a_normal_ending(
        self, seeded: object, client: YouTubeClient, study: StudyConfig, tracker: QuotaTracker
    ) -> None:
        tracker.mark_exhausted("search")
        tracker.mark_exhausted("units")

        summary = daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]

        # No request, no exception: the run simply has nothing it may do today.
        assert len(responses.calls) == 0
        assert summary.search_calls == 0
        assert summary.slices_remaining == 2

    @responses.activate
    def test_work_left_undone_stays_queued(
        self, seeded: object, client: YouTubeClient, study: StudyConfig, tracker: QuotaTracker
    ) -> None:
        responses.add(responses.GET, f"{API_ROOT}/search",
                      json={"pageInfo": {"totalResults": 1}, "items": []})
        tracker.record_search_call(pages=99)

        daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]

        assert TaskQueue(seeded, "s").count("search", status="pending") == 1  # type: ignore[arg-type]


class TestOrphanRecovery:
    @responses.activate
    def test_a_task_left_running_is_picked_up_again(
        self, seeded: object, client: YouTubeClient, study: StudyConfig
    ) -> None:
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search",
                          json={"pageInfo": {"totalResults": 1}, "items": []})
        queue = TaskQueue(seeded, "s")  # type: ignore[arg-type]
        queue.enqueue("search", "apex:2020-01-01", payload={"game_id": "apex"})
        queue.claim("search")
        seeded.commit()  # type: ignore[attr-defined]

        summary = daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]

        assert summary.slices_remaining == 0


class TestRefresh:
    def test_no_snapshots_means_nothing_to_refresh(self, session: object) -> None:
        assert daily_module.needs_refresh(session) is False  # type: ignore[arg-type]

    def test_a_fresh_snapshot_is_not_refreshed(self, session: object) -> None:
        session.add(VideoSnapshot(video_id="v1", collected_at=datetime.now(UTC)))  # type: ignore[attr-defined]
        session.commit()  # type: ignore[attr-defined]
        assert daily_module.needs_refresh(session) is False  # type: ignore[arg-type]

    def test_a_week_old_snapshot_is_refreshed(self, session: object) -> None:
        old = datetime.now(UTC) - timedelta(days=8)
        session.add(VideoSnapshot(video_id="v1", collected_at=old))  # type: ignore[attr-defined]
        session.commit()  # type: ignore[attr-defined]
        assert daily_module.needs_refresh(session) is True  # type: ignore[arg-type]

    @responses.activate
    def test_refresh_appends_a_snapshot_for_known_videos(
        self, seeded: object, client: YouTubeClient, study: StudyConfig
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=8)
        seeded.add(GameVideo(study="s", game_id="apex", video_id="v1",  # type: ignore[attr-defined]
                             period_start="2020-01-01", source="search"))
        seeded.add(VideoSnapshot(video_id="v1", collected_at=old, title_match=True))  # type: ignore[attr-defined]
        seeded.commit()  # type: ignore[attr-defined]
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search",
                          json={"pageInfo": {"totalResults": 1}, "items": []})
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/videos", json={"items": [{
                "id": "v1",
                "snippet": {"channelId": "UC1", "title": "Apex",
                            "publishedAt": "2020-01-15T00:00:00Z"},
                "contentDetails": {"duration": "PT10M"},
                "statistics": {"viewCount": "2000"},
            }]})
        responses.add(responses.GET, f"{API_ROOT}/channels", json={"items": []})

        summary = daily_module.run(seeded, client, study, SETTINGS)  # type: ignore[arg-type]

        assert summary.refreshed == 1


class TestDeploySafety:
    def test_ytc_dry_run_forces_a_dry_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A deploy must not start collecting on its own.
        from typer.testing import CliRunner

        from ytcollector.cli import app

        monkeypatch.setenv("YTC_DRY_RUN", "1")
        monkeypatch.setenv("DATABASE_URL", "sqlite://")
        monkeypatch.setenv("YOUTUBE_API_KEY", "unused")
        with responses.RequestsMock() as mocked:
            result = CliRunner().invoke(app, ["daily", "--study", "battle_royale"])
            assert len(mocked.calls) == 0
        assert "YTC_DRY_RUN is set" in result.output
