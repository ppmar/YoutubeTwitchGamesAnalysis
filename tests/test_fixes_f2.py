"""Milestone F2 of SPEC_FIXES: the genre aggregate holds only the study's games.

"Reste du genre" summed every non-leader series, including the four `role: mode`
entries. Minecraft alone carried 3.16bn hours — three quarters of what the
aggregate showed — so the curve presented as the battle royale genre was mostly
a Minecraft curve.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ytcollector.web.app import rest_of_genre


def series(game_id: str, hours: int, leader: bool = False, enabled: bool = True) -> dict:
    return {
        "game_id": game_id,
        "name": game_id,
        "leader": leader,
        "enabled": enabled,
        "points": [["2020-01", hours], ["2020-02", hours]],
    }


class TestRestOfGenre:
    def test_a_disabled_game_is_left_out(self) -> None:
        rest = rest_of_genre([
            series("fall-guys", 100),
            series("minecraft", 1_000_000, enabled=False),
        ])
        assert rest["points"] == [["2020-01", 100], ["2020-02", 100]]
        assert rest["games"] == 1

    def test_leaders_are_left_out_too(self) -> None:
        rest = rest_of_genre([
            series("fortnite", 5_000, leader=True),
            series("fall-guys", 100),
        ])
        assert rest["games"] == 1
        assert rest["points"][0][1] == 100

    def test_the_remaining_games_are_summed(self) -> None:
        rest = rest_of_genre([series("a", 10), series("b", 25), series("c", 5)])
        assert rest["points"] == [["2020-01", 40], ["2020-02", 40]]
        assert rest["games"] == 3

    def test_the_name_counts_the_games(self) -> None:
        rest = rest_of_genre([series("a", 1), series("b", 1)])
        assert rest["name"] == "Reste du genre (2 jeux)"

    def test_months_are_ordered(self) -> None:
        rest = rest_of_genre([
            {"game_id": "a", "name": "a", "leader": False, "enabled": True,
             "points": [["2020-03", 3], ["2020-01", 1]]},
        ])
        assert [m for m, _ in rest["points"]] == ["2020-01", "2020-03"]

    def test_nothing_enabled_gives_an_empty_series(self) -> None:
        rest = rest_of_genre([series("minecraft", 999, enabled=False)])
        assert rest["points"] == []
        assert rest["games"] == 0


class TestLiveAggregate:
    def test_minecraft_is_absent_from_the_aggregate(self, client: TestClient) -> None:
        state = client.get("/api/state").json()
        rest = state["restOfGenre"]

        minecraft = next(s for s in state["series"] if s["game_id"] == "minecraft")
        assert minecraft["enabled"] is False

        total_rest = sum(hours for _, hours in rest["points"])
        minecraft_hours = sum(hours for _, hours in minecraft["points"])
        assert minecraft_hours > 3e9, "Minecraft should still be in the raw series"
        assert total_rest < minecraft_hours, "the aggregate must not carry Minecraft"

    def test_the_aggregate_counts_the_enabled_non_leaders(self, client: TestClient) -> None:
        state = client.get("/api/state").json()
        expected = sum(
            1 for s in state["series"] if s["enabled"] and not s["leader"]
        )
        assert state["restOfGenre"]["games"] == expected
        assert state["restOfGenre"]["name"] == f"Reste du genre ({expected} jeux)"

    def test_every_series_says_whether_it_is_enabled(self, client: TestClient) -> None:
        # The chronology and the scatter grey the disabled ones out.
        state = client.get("/api/state").json()
        assert all("enabled" in s for s in state["series"])
        assert any(s["enabled"] is False for s in state["series"])
