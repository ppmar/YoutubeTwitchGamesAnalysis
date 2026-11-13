"""Collection stages 1 to 3 on mocked responses (SPEC section 5, milestone 3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import responses
from sqlalchemy import func, select

from ytcollector.client import API_ROOT, YouTubeClient
from ytcollector.models import ChannelSnapshot, Game, GameVideo, SearchSlice, VideoSnapshot
from ytcollector.quota import QuotaLimits, QuotaTracker
from ytcollector.stages import channels as channels_stage
from ytcollector.stages import search as search_stage
from ytcollector.stages import videos as videos_stage
from ytcollector.tasks import TaskQueue

KEY = "TEST-KEY"


@pytest.fixture
def tracker(session: object) -> QuotaTracker:
    return QuotaTracker(session, QuotaLimits(100, 10_000, 0.0))  # type: ignore[arg-type]


@pytest.fixture
def client(session: object, tracker: QuotaTracker) -> YouTubeClient:
    return YouTubeClient(session=session, tracker=tracker, api_key=KEY, backoff_factor=0.0)  # type: ignore[arg-type]


@pytest.fixture
def game(session: object) -> Game:
    row = Game(
        study="s", game_id="apex-legends", game_name="Apex Legends",
        twitch_names=["Apex Legends"], yt_query='"Apex Legends"', match_terms=["Apex Legends"],
        role="leader", enabled=True, first_month="2020-01", last_month="2020-02",
        peak_month="2020-01", months_in_top200=2,
    )
    session.add(row)  # type: ignore[attr-defined]
    session.commit()  # type: ignore[attr-defined]
    return row


def search_body(ids: list[str], next_token: str | None = None, total: int = 1234) -> dict:
    body: dict = {
        "pageInfo": {"totalResults": total, "resultsPerPage": 50},
        "items": [{"id": {"kind": "youtube#video", "videoId": i}} for i in ids],
    }
    if next_token:
        body["nextPageToken"] = next_token
    return body


def video_item(video_id: str, **kwargs: object) -> dict:
    snippet = {
        "channelId": kwargs.get("channel_id", "UC1"),
        "title": kwargs.get("title", "Apex Legends gameplay"),
        "description": kwargs.get("description", "playing apex legends"),
        "tags": kwargs.get("tags", ["apex"]),
        "publishedAt": kwargs.get("published_at", "2020-01-15T10:00:00Z"),
        "categoryId": "20",
    }
    item: dict = {
        "id": video_id,
        "snippet": snippet,
        "contentDetails": {"duration": kwargs.get("duration", "PT10M")},
        "statistics": kwargs.get("statistics", {"viewCount": "1000", "likeCount": "100",
                                                "commentCount": "50"}),
        "status": {"madeForKids": False},
    }
    if kwargs.get("live"):
        item["liveStreamingDetails"] = {"actualStartTime": "2020-01-15T10:00:00Z"}
    return item


class TestSearchStage:
    def test_plan_orders_and_counts_slices(self, session: object, game: Game) -> None:
        slices = search_stage.plan(session, "s", slicing="month", before=1, after=1)  # type: ignore[arg-type]
        assert [s.period_start[:7] for s in slices] == ["2020-01", "2019-12", "2020-02", "2020-03"]

    def test_enqueue_is_idempotent(self, session: object, game: Game) -> None:
        queue = TaskQueue(session, "s")  # type: ignore[arg-type]
        slices = search_stage.plan(session, "s", slicing="month", before=0, after=0)  # type: ignore[arg-type]
        assert search_stage.enqueue(queue, slices) == 2
        assert search_stage.enqueue(queue, slices) == 0

    @responses.activate
    def test_dry_run_emits_no_request(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        queue = TaskQueue(session, "s")  # type: ignore[arg-type]
        search_stage.enqueue(queue, search_stage.plan(session, "s", before=0, after=0))  # type: ignore[arg-type]
        result = search_stage.run(session, client, "s", dry_run=True)  # type: ignore[arg-type]
        assert result.processed == 2
        assert len(responses.calls) == 0
        assert queue.count("search", status="pending") == 2

    @responses.activate
    def test_stores_slices_and_videos(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search", json=search_body(["v1", "v2"]))
        queue = TaskQueue(session, "s")  # type: ignore[arg-type]
        search_stage.enqueue(queue, search_stage.plan(session, "s", before=0, after=0))  # type: ignore[arg-type]

        result = search_stage.run(session, client, "s")  # type: ignore[arg-type]

        assert result.processed == 2
        assert result.items == 4
        slices = list(session.scalars(select(SearchSlice)))  # type: ignore[attr-defined]
        assert len(slices) == 2
        assert slices[0].approx_total_results == 1234
        assert slices[0].n_returned == 2
        links = list(session.scalars(select(GameVideo)))  # type: ignore[attr-defined]
        assert {link.video_id for link in links} == {"v1", "v2"}

    @responses.activate
    def test_keys_are_unique_per_slice_and_page(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search", json=search_body(["v1"]))
        queue = TaskQueue(session, "s")  # type: ignore[arg-type]
        search_stage.enqueue(queue, search_stage.plan(session, "s", before=0, after=0))  # type: ignore[arg-type]
        search_stage.run(session, client, "s")  # type: ignore[arg-type]

        total = session.scalar(select(func.count()).select_from(SearchSlice))  # type: ignore[attr-defined]
        distinct = session.scalar(  # type: ignore[attr-defined]
            select(func.count()).select_from(
                select(SearchSlice.study, SearchSlice.game_id, SearchSlice.period_start,
                       SearchSlice.page).distinct().subquery()
            )
        )
        assert total == distinct

    @responses.activate
    def test_stops_cleanly_when_the_search_budget_is_gone(
        self, session: object, client: YouTubeClient, game: Game, tracker: QuotaTracker
    ) -> None:
        responses.add(responses.GET, f"{API_ROOT}/search", json=search_body(["v1"]))
        queue = TaskQueue(session, "s")  # type: ignore[arg-type]
        search_stage.enqueue(queue, search_stage.plan(session, "s", before=0, after=0))  # type: ignore[arg-type]
        tracker.record_search_call(pages=99)

        result = search_stage.run(session, client, "s")  # type: ignore[arg-type]

        assert result.processed == 1
        assert result.stopped_on_quota
        # The unfinished slice is still pending, ready for tomorrow.
        assert queue.count("search", status="pending") == 1

    @responses.activate
    def test_a_rerun_does_not_redo_finished_slices(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        for _ in range(2):
            responses.add(responses.GET, f"{API_ROOT}/search", json=search_body(["v1"]))
        queue = TaskQueue(session, "s")  # type: ignore[arg-type]
        search_stage.enqueue(queue, search_stage.plan(session, "s", before=0, after=0))  # type: ignore[arg-type]
        search_stage.run(session, client, "s")  # type: ignore[arg-type]
        calls_after_first = len(responses.calls)

        search_stage.enqueue(queue, search_stage.plan(session, "s", before=0, after=0))  # type: ignore[arg-type]
        again = search_stage.run(session, client, "s")  # type: ignore[arg-type]

        assert again.processed == 0
        assert len(responses.calls) == calls_after_first


class TestVideosStage:
    @responses.activate
    def test_builds_snapshots_with_derived_fields(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        session.add(GameVideo(study="s", game_id="apex-legends", video_id="v1",  # type: ignore[attr-defined]
                              period_start="2020-01-01", rank=1, source="search"))
        session.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/videos",
                      json={"items": [video_item("v1", duration="PT45S")]})

        videos_stage.run(session, client, "s")  # type: ignore[arg-type]

        snap = session.scalar(select(VideoSnapshot))  # type: ignore[attr-defined]
        assert snap.video_id == "v1"
        assert snap.duration_seconds == 45
        assert snap.is_short_proxy is True
        assert snap.was_live is False
        assert snap.video_type == "gameplay"
        assert snap.view_count == 1000
        # The match is decided per (game, video) pair, not on the snapshot.
        link = session.scalar(select(GameVideo))  # type: ignore[attr-defined]
        assert link.title_match is True

    @responses.activate
    def test_hidden_statistics_stay_null(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        session.add(GameVideo(study="s", game_id="apex-legends", video_id="v1",  # type: ignore[attr-defined]
                              period_start="2020-01-01", source="search"))
        session.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/videos",
                      json={"items": [video_item("v1", statistics={"viewCount": "10"})]})

        videos_stage.run(session, client, "s")  # type: ignore[arg-type]

        snap = session.scalar(select(VideoSnapshot))  # type: ignore[attr-defined]
        assert snap.view_count == 10
        assert snap.like_count is None
        assert snap.comment_count is None

    @responses.activate
    def test_a_video_that_does_not_mention_the_game_is_not_a_match(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        session.add(GameVideo(study="s", game_id="apex-legends", video_id="v1",  # type: ignore[attr-defined]
                              period_start="2020-01-01", source="search"))
        session.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/videos", json={"items": [
            video_item("v1", title="Minecraft survival", description="blocks", tags=["mc"])]})

        videos_stage.run(session, client, "s")  # type: ignore[arg-type]

        assert session.scalar(select(GameVideo)).title_match is False  # type: ignore[attr-defined]

    @responses.activate
    def test_refresh_appends_instead_of_overwriting(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        session.add(GameVideo(study="s", game_id="apex-legends", video_id="v1",  # type: ignore[attr-defined]
                              period_start="2020-01-01", source="search"))
        session.commit()  # type: ignore[attr-defined]
        for views in ("1000", "2000"):
            responses.add(responses.GET, f"{API_ROOT}/videos",
                          json={"items": [video_item("v1", statistics={"viewCount": views})]})

        videos_stage.run(session, client, "s", video_ids=["v1"])  # type: ignore[arg-type]
        import time as _t
        _t.sleep(0.01)
        videos_stage.run(session, client, "s", video_ids=["v1"])  # type: ignore[arg-type]

        snaps = list(session.scalars(select(VideoSnapshot).order_by(VideoSnapshot.collected_at)))  # type: ignore[attr-defined]
        assert len(snaps) == 2
        assert [s.view_count for s in snaps] == [1000, 2000]

    def test_dry_run_emits_no_request(
        self, session: object, client: YouTubeClient, game: Game
    ) -> None:
        session.add(GameVideo(study="s", game_id="apex-legends", video_id="v1",  # type: ignore[attr-defined]
                              period_start="2020-01-01", source="search"))
        session.commit()  # type: ignore[attr-defined]
        with responses.RequestsMock() as mocked:
            videos_stage.run(session, client, "s", dry_run=True)  # type: ignore[arg-type]
            assert len(mocked.calls) == 0


class TestChannelsStage:
    @responses.activate
    def test_builds_channel_snapshots(self, session: object, client: YouTubeClient) -> None:
        session.add(VideoSnapshot(video_id="v1", collected_at=datetime.now(UTC),  # type: ignore[attr-defined]
                                  channel_id="UC1"))
        session.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/channels", json={"items": [{
            "id": "UC1",
            "snippet": {"title": "Chan", "country": "US", "publishedAt": "2015-01-01T00:00:00Z"},
            "statistics": {"subscriberCount": "1000000", "viewCount": "5", "videoCount": "3"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
        }]})

        channels_stage.run(session, client, "s")  # type: ignore[arg-type]

        row = session.scalar(select(ChannelSnapshot))  # type: ignore[attr-defined]
        assert row.channel_id == "UC1"
        assert row.subscriber_count == 1_000_000
        assert row.uploads_playlist_id == "UU1"

    @responses.activate
    def test_a_hidden_subscriber_count_is_null(
        self, session: object, client: YouTubeClient
    ) -> None:
        session.add(VideoSnapshot(video_id="v1", collected_at=datetime.now(UTC),  # type: ignore[attr-defined]
                                  channel_id="UC1"))
        session.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/channels", json={"items": [{
            "id": "UC1", "snippet": {"title": "Chan"},
            "statistics": {"hiddenSubscriberCount": True, "viewCount": "5"},
        }]})

        channels_stage.run(session, client, "s")  # type: ignore[arg-type]

        assert session.scalar(select(ChannelSnapshot)).subscriber_count is None  # type: ignore[attr-defined]
