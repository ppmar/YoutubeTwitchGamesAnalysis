"""Milestone F1 of SPEC_FIXES: one game, one match.

`title_match` used to be a snapshot column computed from the union of every
linked game's terms, so a PUBG Mobile video counted for PUBG as well. The
decision now belongs to the (game, video) pair, and exclusion terms keep the
neighbours apart.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import responses
from sqlalchemy import select

from ytcollector.client import API_ROOT, YouTubeClient
from ytcollector.config import GameConfig, StudyConfig
from ytcollector.derive import match_game
from ytcollector.games import prepare_games
from ytcollector.models import Game, GameVideo, VideoSnapshot
from ytcollector.quota import QuotaLimits, QuotaTracker
from ytcollector.rematch import audit_queries, rematch
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
def neighbours(session: object) -> object:
    """PUBG and PUBG Mobile: the pair that exposed the bug."""
    session.add(Game(  # type: ignore[attr-defined]
        study="s", game_id="pubg", game_name="PUBG", twitch_names=["PUBG"],
        yt_query='"PUBG" -mobile', match_terms=["PUBG", "PLAYERUNKNOWN"],
        exclude_terms=["mobile", "BGMI"], role="leader", enabled=True,
    ))
    session.add(Game(  # type: ignore[attr-defined]
        study="s", game_id="pubg-mobile", game_name="PUBG Mobile",
        twitch_names=["PUBG Mobile"], yt_query='"PUBG Mobile"',
        match_terms=["PUBG Mobile"], exclude_terms=[], role="follower", enabled=True,
    ))
    session.commit()  # type: ignore[attr-defined]
    return session


def add_video(session: object, video_id: str, title: str, games: list[str],
              tags: list[str] | None = None, description: str = "",
              comment_count: int = 100) -> None:
    session.add(VideoSnapshot(  # type: ignore[attr-defined]
        video_id=video_id, collected_at=NOW, channel_id="UC1", title=title,
        tags=tags or [], description=description,
        published_at=datetime(2020, 5, 5, tzinfo=UTC), view_count=1000,
        comment_count=comment_count, video_type="gameplay", is_short_proxy=False,
    ))
    for game_id in games:
        session.add(GameVideo(  # type: ignore[attr-defined]
            study="s", game_id=game_id, video_id=video_id,
            period_start="2020-05-01", source="search",
        ))


class TestMatchGame:
    def test_a_match_term_alone_is_enough(self) -> None:
        assert match_game(["PUBG"], [], title="PUBG gameplay") == (True, "PUBG")

    def test_an_exclusion_blocks_and_says_why(self) -> None:
        matched, reason = match_game(["PUBG"], ["mobile"], title="PUBG Mobile gameplay")
        assert matched is False
        assert reason == "-mobile"

    def test_the_description_does_not_trigger_an_exclusion(self) -> None:
        # Descriptions list every game a channel covers; excluding on them would
        # throw away good videos.
        matched, reason = match_game(
            ["PUBG"], ["mobile"],
            title="PUBG gameplay", description="also on my channel: PUBG Mobile, Free Fire",
        )
        assert matched is True
        assert reason == "PUBG"

    def test_a_tag_does_trigger_an_exclusion(self) -> None:
        matched, reason = match_game(
            ["PUBG"], ["mobile"], title="ranked push", tags=["pubg", "mobile"]
        )
        assert matched is False
        assert reason == "-mobile"

    def test_no_term_at_all_gives_no_reason(self) -> None:
        assert match_game(["PUBG"], ["mobile"], title="Minecraft house") == (False, None)


class TestPerPairDecision:
    def test_one_video_gets_an_independent_value_per_game(self, neighbours: object) -> None:
        add_video(neighbours, "v1", "PUBG Mobile gameplay", ["pubg", "pubg-mobile"])
        neighbours.commit()  # type: ignore[attr-defined]

        videos_stage.apply_matches(neighbours, "s", ["v1"])  # type: ignore[arg-type]
        neighbours.commit()  # type: ignore[attr-defined]

        rows = {
            r.game_id: (r.title_match, r.match_reason)
            for r in neighbours.scalars(select(GameVideo))  # type: ignore[attr-defined]
        }
        assert rows["pubg"] == (False, "-mobile")
        assert rows["pubg-mobile"] == (True, "PUBG Mobile")

    def test_a_desktop_video_still_belongs_to_pubg(self, neighbours: object) -> None:
        add_video(neighbours, "v1", "PUBG chicken dinner solo", ["pubg"])
        neighbours.commit()  # type: ignore[attr-defined]

        videos_stage.apply_matches(neighbours, "s", ["v1"])  # type: ignore[arg-type]

        row = neighbours.scalar(select(GameVideo))  # type: ignore[attr-defined]
        assert row.title_match is True

    @responses.activate
    def test_the_videos_stage_decides_as_it_collects(
        self, neighbours: object, client: YouTubeClient
    ) -> None:
        neighbours.add(GameVideo(study="s", game_id="pubg", video_id="v1",  # type: ignore[attr-defined]
                                 period_start="2020-05-01", source="search"))
        neighbours.add(GameVideo(study="s", game_id="pubg-mobile", video_id="v1",  # type: ignore[attr-defined]
                                 period_start="2020-05-01", source="search"))
        neighbours.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/videos", json={"items": [{
            "id": "v1",
            "snippet": {"channelId": "UC1", "title": "BGMI gameplay",
                        "publishedAt": "2020-05-05T00:00:00Z"},
            "contentDetails": {"duration": "PT9M"},
            "statistics": {"viewCount": "500"},
        }]})

        videos_stage.run(neighbours, client, "s", video_ids=["v1"])  # type: ignore[arg-type]

        rows = {
            r.game_id: r.title_match
            for r in neighbours.scalars(select(GameVideo))  # type: ignore[attr-defined]
        }
        # BGMI is Battlegrounds Mobile India: excluded from pubg, and it does not
        # carry the words "PUBG Mobile" either.
        assert rows["pubg"] is False
        assert rows["pubg-mobile"] is False


class TestRematch:
    @responses.activate
    def test_it_emits_no_request(self, neighbours: object) -> None:
        add_video(neighbours, "v1", "PUBG Mobile gameplay", ["pubg", "pubg-mobile"])
        neighbours.commit()  # type: ignore[attr-defined]
        with responses.RequestsMock() as mocked:
            rematch(neighbours, "s")  # type: ignore[arg-type]
            assert len(mocked.calls) == 0

    def test_it_rejects_what_the_old_decision_accepted(self, neighbours: object) -> None:
        add_video(neighbours, "v1", "PUBG Mobile gameplay", ["pubg", "pubg-mobile"])
        # As the migration leaves things: permissive on every pair.
        neighbours.commit()  # type: ignore[attr-defined]
        for row in neighbours.scalars(select(GameVideo)):  # type: ignore[attr-defined]
            row.title_match = True
            row.match_reason = "migrated"
        neighbours.commit()  # type: ignore[attr-defined]

        result = rematch(neighbours, "s")  # type: ignore[arg-type]

        assert result.pairs == 2
        assert result.changed == 1
        assert result.now_rejected == 1
        rows = {
            r.game_id: r.title_match
            for r in neighbours.scalars(select(GameVideo))  # type: ignore[attr-defined]
        }
        assert rows == {"pubg": False, "pubg-mobile": True}

    def test_it_can_be_limited_to_some_games(self, neighbours: object) -> None:
        add_video(neighbours, "v1", "PUBG Mobile gameplay", ["pubg", "pubg-mobile"])
        neighbours.commit()  # type: ignore[attr-defined]
        result = rematch(neighbours, "s", games=["pubg"])  # type: ignore[arg-type]
        assert result.pairs == 1
        assert set(result.by_game) == {"pubg"}


class TestAuditQueries:
    def test_it_reports_the_match_rate(self, neighbours: object) -> None:
        add_video(neighbours, "good", "PUBG solo win", ["pubg"])
        add_video(neighbours, "bad", "PUBG Mobile gameplay", ["pubg"])
        neighbours.commit()  # type: ignore[attr-defined]

        audits = {a.game_id: a for a in audit_queries(neighbours, "s")}  # type: ignore[arg-type]

        assert audits["pubg"].videos == 2
        assert audits["pubg"].matching == 1
        assert audits["pubg"].excluded == 1
        assert audits["pubg"].match_rate == 0.5
        assert audits["pubg"].flagged is True

    def test_it_finds_overlap_between_two_games(self, session: object) -> None:
        # Two games whose terms both hit the same video.
        session.add(Game(  # type: ignore[attr-defined]
            study="s", game_id="a", game_name="A", twitch_names=["A"], yt_query="A",
            match_terms=["battle royale"], exclude_terms=[], role="follower", enabled=True))
        session.add(Game(  # type: ignore[attr-defined]
            study="s", game_id="b", game_name="B", twitch_names=["B"], yt_query="B",
            match_terms=["battle royale"], exclude_terms=[], role="follower", enabled=True))
        session.commit()  # type: ignore[attr-defined]
        add_video(session, "v1", "best battle royale moments", ["a"])
        session.commit()  # type: ignore[attr-defined]

        audits = {x.game_id: x for x in audit_queries(session, "s")}  # type: ignore[arg-type]

        assert audits["a"].overlap["b"] == 1.0
        assert audits["a"].flagged is True

    def test_a_clean_game_is_not_flagged(self, neighbours: object) -> None:
        add_video(neighbours, "v1", "PUBG solo win", ["pubg"])
        neighbours.commit()  # type: ignore[attr-defined]
        audits = {a.game_id: a for a in audit_queries(neighbours, "s")}  # type: ignore[arg-type]
        assert audits["pubg"].flagged is False


class TestForceFields:
    def test_a_tuned_field_still_survives_by_default(
        self, session: object, twitch_csv: object
    ) -> None:
        study = StudyConfig(study="s", title="S", games=[GameConfig(
            game_id="pubg", game_name="PUBG", twitch_names=["PUBG: BATTLEGROUNDS"],
            yt_query='"PUBG" -mobile', match_terms=["PUBG"],
            exclude_terms=["mobile"], role="leader")])
        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]
        row = session.scalar(select(Game))  # type: ignore[attr-defined]
        row.yt_query = "hand tuned"
        session.commit()  # type: ignore[attr-defined]

        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]

        assert session.scalar(select(Game)).yt_query == "hand tuned"  # type: ignore[attr-defined]

    def test_forcing_overwrites_it(self, session: object, twitch_csv: object) -> None:
        study = StudyConfig(study="s", title="S", games=[GameConfig(
            game_id="pubg", game_name="PUBG", twitch_names=["PUBG: BATTLEGROUNDS"],
            yt_query='"PUBG" -mobile', match_terms=["PUBG"],
            exclude_terms=["mobile", "BGMI"], role="leader")])
        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]
        row = session.scalar(select(Game))  # type: ignore[attr-defined]
        row.yt_query = "stale"
        row.exclude_terms = []
        session.commit()  # type: ignore[attr-defined]

        prepare_games(session, study, csv_path=twitch_csv,  # type: ignore[arg-type]
                      force_fields=["yt_query", "exclude_terms"])

        row = session.scalar(select(Game))  # type: ignore[attr-defined]
        assert row.yt_query == '"PUBG" -mobile'
        assert row.exclude_terms == ["mobile", "BGMI"]

    def test_an_unknown_field_is_refused(self, session: object, twitch_csv: object) -> None:
        study = StudyConfig(study="s", title="S", games=[GameConfig(
            game_id="pubg", game_name="PUBG", twitch_names=["PUBG: BATTLEGROUNDS"],
            yt_query="q", match_terms=["PUBG"], exclude_terms=[], role="leader")])
        with pytest.raises(ValueError, match="hours_watched_total"):
            prepare_games(session, study, csv_path=twitch_csv,  # type: ignore[arg-type]
                          force_fields=["hours_watched_total"])


class TestStudyFile:
    def test_the_battle_royale_queries_exclude_mobile(self) -> None:
        from ytcollector.config import load_study

        games = {g.game_id: g for g in load_study("battle_royale").games}
        assert games["pubg"].yt_query == '"PUBG" -mobile'
        assert "mobile" in games["pubg"].exclude_terms
        # BATTLEGROUNDS caught Totally Accurate Battlegrounds and BGMI.
        assert "BATTLEGROUNDS" not in games["pubg"].match_terms
        assert "mobile" in games["apex-legends"].exclude_terms
        assert "mobile" in games["call-of-duty-warzone"].exclude_terms
