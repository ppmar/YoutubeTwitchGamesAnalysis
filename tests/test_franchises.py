"""Franchises: a parent game and its spin-offs, summed as a derived view.

The members stay separate games — PUBG and PUBG Mobile have different Twitch
categories, so joining them as one would compare a YouTube mix against a single
Twitch series. The franchise sits on top, for the questions where the whole
family is the subject.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ytcollector.config import ConfigError, load_study
from ytcollector.derive import match_game
from ytcollector.web.app import _reindex, build_franchises


class Franchise:
    def __init__(self, franchise_id: str, members: list[str], title: str = "F") -> None:
        self.franchise_id = franchise_id
        self.title = title
        self.members = members


class Study:
    def __init__(self, franchises: list[Franchise]) -> None:
        self.franchises = franchises


def series(game_id: str, points: list[tuple[str, int]]) -> dict:
    return {"game_id": game_id, "name": game_id, "leader": False, "enabled": True,
            "points": [[m, v] for m, v in points]}


def attention(game_id: str, points: list[tuple[int, int, float]]) -> list[dict]:
    return [{"game_id": game_id, "year": y, "month": m, "index": v} for y, m, v in points]


class TestReindex:
    def test_the_peak_becomes_100(self) -> None:
        assert _reindex([["2020-01", 50], ["2020-02", 200]]) == [
            ["2020-01", 25.0], ["2020-02", 100.0]
        ]

    def test_an_all_zero_series_stays_at_zero(self) -> None:
        assert _reindex([["2020-01", 0]]) == [["2020-01", 0.0]]

    def test_an_empty_series_is_empty(self) -> None:
        assert _reindex([]) == []


class TestBuildFranchises:
    def test_it_sums_the_members_on_the_twitch_side(self) -> None:
        out = build_franchises(
            Study([Franchise("pubg", ["pubg", "pubg-mobile"])]),
            [series("pubg", [("2020-01", 100)]), series("pubg-mobile", [("2020-01", 300)])],
            [],
        )
        # 100 + 300, then rescaled on its own peak.
        assert out[0]["twitch"] == [["2020-01", 100.0]]
        assert out[0]["membersPresent"] == ["pubg", "pubg-mobile"]

    def test_months_only_one_member_has_still_count(self) -> None:
        out = build_franchises(
            Study([Franchise("pubg", ["pubg", "pubg-mobile"])]),
            [series("pubg", [("2020-01", 100), ("2020-02", 50)]),
             series("pubg-mobile", [("2020-02", 150)])],
            [],
        )
        assert out[0]["twitch"] == [["2020-01", 50.0], ["2020-02", 100.0]]

    def test_it_sums_the_youtube_side_too(self) -> None:
        out = build_franchises(
            Study([Franchise("pubg", ["pubg", "pubg-mobile"])]),
            [series("pubg", [("2020-01", 1)])],
            attention("pubg", [(2020, 1, 40.0)]) + attention("pubg-mobile", [(2020, 1, 60.0)]),
        )
        assert out[0]["youtube"] == [["2020-01", 100.0]]
        assert out[0]["membersWithAttention"] == ["pubg", "pubg-mobile"]

    def test_a_member_with_no_data_is_simply_absent(self) -> None:
        out = build_franchises(
            Study([Franchise("pubg", ["pubg", "pubg-mobile"])]),
            [series("pubg", [("2020-01", 10)])],
            [],
        )
        assert out[0]["membersPresent"] == ["pubg"]
        assert out[0]["youtube"] == []

    def test_a_franchise_with_no_member_present_is_dropped(self) -> None:
        out = build_franchises(
            Study([Franchise("ghost", ["a", "b"])]), [series("other", [("2020-01", 1)])], []
        )
        assert out == []

    def test_other_games_never_leak_in(self) -> None:
        out = build_franchises(
            Study([Franchise("pubg", ["pubg", "pubg-mobile"])]),
            [series("pubg", [("2020-01", 100)]), series("fortnite", [("2020-01", 9999)])],
            [],
        )
        assert out[0]["membersPresent"] == ["pubg"]


class TestStudyFile:
    def test_the_three_spin_off_pairs_are_declared(self) -> None:
        study = load_study("battle_royale")
        by_id = {f.franchise_id: f for f in study.franchises}
        assert by_id["pubg"].members == ["pubg", "pubg-mobile"]
        assert by_id["apex-legends"].members == ["apex-legends", "apex-legends-mobile"]
        assert by_id["warzone"].members == [
            "call-of-duty-warzone", "call-of-duty-warzone-mobile"
        ]

    def test_every_member_is_a_real_game(self) -> None:
        study = load_study("battle_royale")
        known = {g.game_id for g in study.games}
        for franchise in study.franchises:
            assert set(franchise.members) <= known, franchise.franchise_id

    def test_a_franchise_is_not_a_roster_entry(self) -> None:
        # Otherwise the genre would count the members twice.
        study = load_study("battle_royale")
        ids = {g.game_id for g in study.games}
        for franchise in study.franchises:
            assert franchise.franchise_id not in ids or franchise.franchise_id in franchise.members

    def test_an_unknown_member_is_refused(self, tmp_path: object) -> None:
        path = tmp_path / "bad.yml"  # type: ignore[operator]
        path.write_text(
            "study: bad\ngames:\n- {game_id: a, game_name: A, twitch_names: [A]}\n"
            "franchises:\n- {franchise_id: f, members: [a, nope]}\n"
        )
        with pytest.raises(ConfigError, match="unknown games"):
            load_study("bad", directory=tmp_path)  # type: ignore[arg-type]

    def test_a_franchise_needs_two_members(self, tmp_path: object) -> None:
        path = tmp_path / "bad.yml"  # type: ignore[operator]
        path.write_text(
            "study: bad\ngames:\n- {game_id: a, game_name: A, twitch_names: [A]}\n"
            "franchises:\n- {franchise_id: f, members: [a]}\n"
        )
        with pytest.raises(ConfigError, match="at least two members"):
            load_study("bad", directory=tmp_path)  # type: ignore[arg-type]


class TestArmaTermsNoLongerGeneric:
    """`Battle Royale` as a match term caught most of the genre."""

    def test_arma_no_longer_claims_the_whole_genre(self) -> None:
        games = {g.game_id: g for g in load_study("battle_royale").games}
        arma = games["arma-3"]
        assert "Battle Royale" not in arma.match_terms
        matched, _ = match_game(
            arma.match_terms, arma.exclude_terms, title="best battle royale moments 2021"
        )
        assert matched is False

    def test_no_game_catches_another_game_name_any_more(self) -> None:
        games = [g for g in load_study("battle_royale").games]
        offenders = []
        for a in games:
            for b in games:
                if a.game_id == b.game_id:
                    continue
                if match_game(a.match_terms, a.exclude_terms, title=b.game_name)[0]:
                    offenders.append((a.game_id, b.game_id))
        assert offenders == []


class TestLive:
    def test_the_payload_carries_the_franchises(self, client: TestClient) -> None:
        franchises = client.get("/api/state").json()["franchises"]
        ids = {f["franchise_id"] for f in franchises}
        assert "pubg" in ids
        pubg = next(f for f in franchises if f["franchise_id"] == "pubg")
        assert pubg["members"] == ["pubg", "pubg-mobile"]
        assert pubg["twitch"], "the Twitch side should be summed from the roster"
        assert max(v for _, v in pubg["twitch"]) == 100.0
