"""Channel panel (SPEC section 5 step 5, milestone 7).

The panel exists to answer question 2: do big creators start a trend or amplify
one. That needs a *complete* upload history for a fixed set of channels, which
search cannot give.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import responses

from ytcollector.client import API_ROOT, YouTubeClient
from ytcollector.models import ChannelSnapshot, Game, GameVideo, VideoSnapshot
from ytcollector.quota import QuotaLimits, QuotaTracker
from ytcollector.stages import panel as stage

NOW = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "TEST-KEY"


@pytest.fixture
def tracker(session: object) -> QuotaTracker:
    return QuotaTracker(session, QuotaLimits(100, 10_000, 0.0))  # type: ignore[arg-type]


@pytest.fixture
def client(session: object, tracker: QuotaTracker) -> YouTubeClient:
    return YouTubeClient(session=session, tracker=tracker, api_key=KEY, backoff_factor=0.0)  # type: ignore[arg-type]


@pytest.fixture
def seeded(session: object) -> object:
    session.add(Game(study="s", game_id="apex", game_name="Apex Legends",  # type: ignore[attr-defined]
                     twitch_names=["Apex Legends"], yt_query='"Apex Legends"',
                     match_terms=["Apex Legends"], role="leader", enabled=True))
    session.commit()  # type: ignore[attr-defined]
    return session


def add_channel(session: object, channel_id: str, subscribers: int | None,
                playlist: str | None = "UU1") -> None:
    session.add(ChannelSnapshot(channel_id=channel_id, collected_at=NOW,  # type: ignore[attr-defined]
                                subscriber_count=subscribers, uploads_playlist_id=playlist))


class TestPanelMembership:
    def test_includes_channels_above_the_threshold(self, seeded: object) -> None:
        add_channel(seeded, "UCbig", 200_000)
        add_channel(seeded, "UCsmall", 1_000)
        seeded.commit()  # type: ignore[attr-defined]
        assert [c[0] for c in stage.panel_channels(seeded, 100_000)] == ["UCbig"]  # type: ignore[arg-type]

    def test_a_hidden_subscriber_count_does_not_qualify(self, seeded: object) -> None:
        add_channel(seeded, "UChidden", None)
        seeded.commit()  # type: ignore[attr-defined]
        assert stage.panel_channels(seeded, 100_000) == []  # type: ignore[arg-type]

    def test_the_threshold_is_configurable(self, seeded: object) -> None:
        add_channel(seeded, "UCmid", 50_000)
        seeded.commit()  # type: ignore[attr-defined]
        assert stage.panel_channels(seeded, 100_000) == []  # type: ignore[arg-type]
        assert [c[0] for c in stage.panel_channels(seeded, 10_000)] == ["UCmid"]  # type: ignore[arg-type]

    def test_pinned_channels_are_always_in(
        self, seeded: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_channel(seeded, "UCtiny", 10)
        seeded.commit()  # type: ignore[attr-defined]
        monkeypatch.setattr(stage, "pinned_channels", lambda: ["UCtiny"])
        assert [c[0] for c in stage.panel_channels(seeded, 100_000)] == ["UCtiny"]  # type: ignore[arg-type]


class TestUploads:
    @responses.activate
    def test_walks_every_page_of_the_uploads_playlist(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        responses.add(responses.GET, f"{API_ROOT}/playlistItems", json={
            "items": [{"contentDetails": {"videoId": "v1"}}], "nextPageToken": "P2"})
        responses.add(responses.GET, f"{API_ROOT}/playlistItems", json={
            "items": [{"contentDetails": {"videoId": "v2"}}]})

        assert stage.collect_uploads(seeded, client, "UU1") == ["v1", "v2"]  # type: ignore[arg-type]

    @responses.activate
    def test_uploads_do_not_touch_the_search_counter(
        self, seeded: object, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        responses.add(responses.GET, f"{API_ROOT}/playlistItems", json={"items": []})
        stage.collect_uploads(seeded, client, "UU1")  # type: ignore[arg-type]
        # This is the whole appeal of the panel: it is free of the bottleneck.
        assert tracker.remaining_search_calls() == 100
        assert tracker.remaining_units() == 9_999


class TestAttachToGames:
    def test_links_a_video_that_mentions_the_game(self, seeded: object) -> None:
        seeded.add(VideoSnapshot(video_id="v1", collected_at=NOW, channel_id="UC1",  # type: ignore[attr-defined]
                                 title="Apex Legends ranked grind",
                                 published_at=datetime(2021, 3, 2, tzinfo=UTC)))
        seeded.commit()  # type: ignore[attr-defined]

        assert stage.attach_to_games(seeded, "s", ["v1"]) == 1  # type: ignore[arg-type]
        seeded.commit()  # type: ignore[attr-defined]

        link = seeded.get(GameVideo, ("s", "apex", "v1", "2021-03-01"))  # type: ignore[attr-defined]
        assert link is not None
        assert link.source == "panel"

    def test_ignores_a_video_about_another_game(self, seeded: object) -> None:
        seeded.add(VideoSnapshot(video_id="v1", collected_at=NOW, channel_id="UC1",  # type: ignore[attr-defined]
                                 title="Minecraft house tour",
                                 published_at=datetime(2021, 3, 2, tzinfo=UTC)))
        seeded.commit()  # type: ignore[attr-defined]
        assert stage.attach_to_games(seeded, "s", ["v1"]) == 0  # type: ignore[arg-type]

    def test_linking_twice_does_not_duplicate(self, seeded: object) -> None:
        seeded.add(VideoSnapshot(video_id="v1", collected_at=NOW, channel_id="UC1",  # type: ignore[attr-defined]
                                 title="Apex Legends gameplay",
                                 published_at=datetime(2021, 3, 2, tzinfo=UTC)))
        seeded.commit()  # type: ignore[attr-defined]
        stage.attach_to_games(seeded, "s", ["v1"])  # type: ignore[arg-type]
        seeded.commit()  # type: ignore[attr-defined]
        assert stage.attach_to_games(seeded, "s", ["v1"]) == 0  # type: ignore[arg-type]


class TestQuestionTwo:
    def test_reports_when_each_creator_first_covered_a_game(self, seeded: object) -> None:
        add_channel(seeded, "UCbig", 5_000_000)
        add_channel(seeded, "UCsmall", 1_000)
        for video_id, channel, day in (("v1", "UCbig", 15), ("v2", "UCbig", 2),
                                       ("v3", "UCsmall", 1)):
            seeded.add(VideoSnapshot(video_id=video_id, collected_at=NOW, channel_id=channel,  # type: ignore[attr-defined]
                                     title="Apex Legends", title_match=True,
                                     published_at=datetime(2019, 2, day, tzinfo=UTC)))
            seeded.add(GameVideo(study="s", game_id="apex", video_id=video_id,  # type: ignore[attr-defined]
                                 period_start="2019-02-01", source="panel"))
        seeded.commit()  # type: ignore[attr-defined]

        firsts = {f.channel_id: f for f in stage.first_videos_per_channel(seeded, "s")}  # type: ignore[arg-type]

        assert firsts["UCbig"].first_published_at.day == 2
        assert firsts["UCbig"].subscriber_count == 5_000_000
        assert firsts["UCsmall"].first_published_at.day == 1

    def test_counts_panel_videos_per_game_and_month(self, seeded: object) -> None:
        for video_id, month in (("v1", 1), ("v2", 1), ("v3", 2)):
            seeded.add(VideoSnapshot(video_id=video_id, collected_at=NOW, channel_id="UC1",  # type: ignore[attr-defined]
                                     title_match=True,
                                     published_at=datetime(2020, month, 5, tzinfo=UTC)))
            seeded.add(GameVideo(study="s", game_id="apex", video_id=video_id,  # type: ignore[attr-defined]
                                 period_start=f"2020-{month:02d}-01", source="panel"))
        seeded.commit()  # type: ignore[attr-defined]

        counts = stage.monthly_counts_per_game(seeded, "s")  # type: ignore[arg-type]

        assert counts == {("apex", "2020-01"): 2, ("apex", "2020-02"): 1}

    def test_search_results_are_excluded_from_panel_counts(self, seeded: object) -> None:
        # The panel is a fixed sample; mixing in search results would break it.
        seeded.add(VideoSnapshot(video_id="v1", collected_at=NOW, channel_id="UC1",  # type: ignore[attr-defined]
                                 title_match=True,
                                 published_at=datetime(2020, 1, 5, tzinfo=UTC)))
        seeded.add(GameVideo(study="s", game_id="apex", video_id="v1",  # type: ignore[attr-defined]
                             period_start="2020-01-01", source="search"))
        seeded.commit()  # type: ignore[attr-defined]
        assert stage.monthly_counts_per_game(seeded, "s") == {}  # type: ignore[arg-type]


class TestRun:
    def test_dry_run_emits_no_request(self, seeded: object, client: YouTubeClient) -> None:
        add_channel(seeded, "UCbig", 200_000)
        seeded.commit()  # type: ignore[attr-defined]
        with responses.RequestsMock() as mocked:
            result = stage.run(seeded, client, "s", dry_run=True)  # type: ignore[arg-type]
            assert len(mocked.calls) == 0
        assert result.processed == 1

    @responses.activate
    def test_walks_snapshots_and_links_in_one_pass(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_channel(seeded, "UCbig", 200_000)
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/playlistItems",
                      json={"items": [{"contentDetails": {"videoId": "v1"}}]})
        responses.add(responses.GET, f"{API_ROOT}/videos", json={"items": [{
            "id": "v1",
            "snippet": {"channelId": "UCbig", "title": "Apex Legends highlights",
                        "publishedAt": "2021-03-02T00:00:00Z"},
            "contentDetails": {"duration": "PT8M"},
            "statistics": {"viewCount": "500"},
        }]})

        result = stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert result.processed == 1
        assert result.items == 1
        assert seeded.get(GameVideo, ("s", "apex", "v1", "2021-03-01")) is not None  # type: ignore[attr-defined]

    def test_a_channel_without_an_uploads_playlist_is_skipped(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_channel(seeded, "UCbig", 200_000, playlist=None)
        seeded.commit()  # type: ignore[attr-defined]
        with responses.RequestsMock() as mocked:
            result = stage.run(seeded, client, "s")  # type: ignore[arg-type]
            assert len(mocked.calls) == 0
        assert result.processed == 0
