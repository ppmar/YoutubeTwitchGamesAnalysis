"""Milestone G4 of SPEC_GENRES: joining the study to the genres.

The spec's criteria: ``prepare-games`` keeps a hand-entered ``steam_appid`` the
way it keeps ``yt_query``, and ``suggest-appids`` touches neither the study file
nor the database. All synthetic.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("scipy", reason="the genre tools are the optional `genres` extra")
pytest.importorskip("sklearn", reason="the genre tools are the optional `genres` extra")

from tests.test_genres_select import fitted, written_matrix  # noqa: F401  (fixture)
from ytcollector.config import GameConfig, StudyConfig, load_study
from ytcollector.genres.select import load_model
from ytcollector.genres.study import (
    check_study,
    score,
    simplify,
    suggest_appids,
    write_suggestions,
)


def game(game_id: str, name: str, appid: int | None = None, twitch: list[str] | None = None):
    return GameConfig(
        game_id=game_id,
        game_name=name,
        twitch_names=twitch or [name],
        yt_query=f'"{name}"',
        match_terms=[name],
        role="follower",
        steam_appid=appid,
    )


def reference(rows: list[tuple[int, str]]):
    import pandas as pd

    return pd.DataFrame(
        {
            "appid": [r[0] for r in rows],
            "name": pd.Series([r[1] for r in rows], dtype="string"),
            "release_date": pd.Series(["2019-01-01"] * len(rows), dtype="string"),
        }
    )


class TestNameMatching:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PUBG: BATTLEGROUNDS", "pubg battlegrounds"),
            ("Apex Legends™", "apex legends"),
            ("DayZ - Game of the Year Edition", "dayz"),
            ("  Fall   Guys  ", "fall guys"),
        ],
    )
    def test_names_are_simplified_the_same_way_on_both_sides(
        self, raw: str, expected: str
    ) -> None:
        assert simplify(raw) == expected

    def test_an_exact_match_scores_one(self) -> None:
        assert score("apex legends", "apex legends") == 1.0

    def test_a_subtitle_does_not_sink_the_score(self) -> None:
        assert score("pubg", "pubg battlegrounds") >= 0.9

    def test_two_different_games_score_low(self) -> None:
        assert score("fall guys", "farming simulator") < 0.55


class TestSuggestAppids:
    @pytest.fixture
    def study(self) -> StudyConfig:
        return StudyConfig(
            study="t",
            title="T",
            games=[
                game("pubg", "PUBG", twitch=["PUBG: BATTLEGROUNDS"]),
                game("apex-legends", "Apex Legends"),
                game("fortnite", "Fortnite"),  # not on Steam
            ],
        )

    def test_it_proposes_the_right_appid_first(self, study: StudyConfig) -> None:
        suggestions = suggest_appids(
            study,
            reference([(578080, "PUBG: BATTLEGROUNDS"), (1172470, "Apex Legends"),
                       (1517290, "Battlefield 2042")]),
        )
        best = {}
        for item in suggestions:
            best.setdefault(item.game_id, item)
        assert best["pubg"].candidate_appid == 578080
        assert best["apex-legends"].candidate_appid == 1172470

    def test_a_game_absent_from_steam_gets_no_candidate(self, study: StudyConfig) -> None:
        suggestions = suggest_appids(
            study, reference([(578080, "PUBG: BATTLEGROUNDS"), (1172470, "Apex Legends")])
        )
        assert "fortnite" not in {s.game_id for s in suggestions}

    def test_it_says_which_name_matched(self, study: StudyConfig) -> None:
        suggestions = suggest_appids(study, reference([(578080, "PUBG: BATTLEGROUNDS")]))
        assert any(s.matched_on for s in suggestions)

    def test_it_writes_the_csv_the_spec_asks_for(
        self, study: StudyConfig, tmp_path: Path
    ) -> None:
        import pandas as pd

        suggestions = suggest_appids(study, reference([(578080, "PUBG: BATTLEGROUNDS")]))
        path = write_suggestions(suggestions, tmp_path)
        frame = pd.read_csv(path)
        assert list(frame.columns)[:4] == [
            "game_id", "candidate_appid", "candidate_name", "score"
        ]

    def test_it_writes_nothing_else(self, study: StudyConfig, tmp_path: Path) -> None:
        """Neither the study file nor the database: the team copies the appid."""
        suggestions = suggest_appids(study, reference([(578080, "PUBG: BATTLEGROUNDS")]))
        write_suggestions(suggestions, tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["appid_suggestions.csv"]


class TestTheStudyFileAndTheRoster:
    def test_the_study_file_accepts_an_appid_and_defaults_to_none(self) -> None:
        roster = load_study("battle_royale")
        assert all(
            g.steam_appid is None or isinstance(g.steam_appid, int) for g in roster.games
        )

    def test_prepare_games_keeps_an_appid_entered_by_hand(self, session, twitch_csv) -> None:
        """Like yt_query: a hand-entered value survives the next prepare-games."""
        from sqlalchemy import select

        from ytcollector.games import prepare_games
        from ytcollector.models import Game

        roster = load_study("battle_royale")
        prepare_games(session, roster, csv_path=twitch_csv)

        row = session.scalars(
            select(Game).where(Game.game_id == "apex-legends")
        ).one()
        row.steam_appid = 1172470
        session.commit()

        prepare_games(session, roster, csv_path=twitch_csv)
        again = session.scalars(select(Game).where(Game.game_id == "apex-legends")).one()
        assert again.steam_appid == 1172470

    def test_an_appid_in_the_study_file_reaches_a_fresh_row(self, session, twitch_csv) -> None:
        from sqlalchemy import select

        from ytcollector.games import prepare_games
        from ytcollector.models import Game

        roster = load_study("battle_royale")
        patched = replace(
            roster,
            games=[
                replace(g, steam_appid=1172470) if g.game_id == "apex-legends" else g
                for g in roster.games
            ],
        )
        prepare_games(session, patched, csv_path=twitch_csv)
        row = session.scalars(select(Game).where(Game.game_id == "apex-legends")).one()
        assert row.steam_appid == 1172470


class TestCheckStudy:
    def test_it_reports_two_genres_per_game(self, fitted: Path) -> None:  # noqa: F811
        model = load_model(fitted, 4)
        appids = list(model.rows["appid"])
        study = StudyConfig(
            study="t", title="T",
            games=[game("a", "Jeu 0", appids[0]), game("b", "Jeu 1", appids[1])],
        )
        found = check_study(model, study)
        assert len(found.with_appid) == 2
        for item in found.with_appid:
            assert 1 <= len(item["genres"]) <= 2
            assert item["genres"][0]["share"] >= item["genres"][-1]["share"]

    def test_it_names_the_genre_holding_most_of_the_study(self, fitted: Path) -> None:  # noqa: F811
        model = load_model(fitted, 4)
        appids = list(model.rows["appid"])
        # The first block of the synthetic data: all of one genre.
        study = StudyConfig(
            study="t", title="T",
            games=[game(f"g{i}", f"Jeu {i}", appids[i]) for i in range(0, 10, 2)],
        )
        found = check_study(model, study)
        assert found.leading_genre is not None
        assert found.leading_genre_count == len(found.with_appid)
        assert found.leading_genre_label

    def test_games_without_an_appid_are_listed_not_guessed(self, fitted: Path) -> None:  # noqa: F811
        model = load_model(fitted, 4)
        study = StudyConfig(
            study="t", title="T",
            games=[game("fortnite", "Fortnite"), game("a", "Jeu 0", next(iter(model.rows["appid"])))],
        )
        found = check_study(model, study)
        assert found.without_appid == ["fortnite"]
        assert [i["game_id"] for i in found.with_appid] == ["a"]

    def test_an_appid_absent_from_the_matrix_is_reported_apart(self, fitted: Path) -> None:  # noqa: F811
        model = load_model(fitted, 4)
        study = StudyConfig(study="t", title="T", games=[game("x", "Inconnu", 99_999_999)])
        found = check_study(model, study)
        assert found.unknown_appid == ["x"]
        assert found.with_appid == []

    def test_it_compares_with_the_plain_tag_criterion(self, fitted: Path) -> None:  # noqa: F811
        model = load_model(fitted, 4)
        appids = list(model.rows["appid"])
        study = StudyConfig(
            study="t", title="T",
            games=[game(f"g{i}", f"Jeu {i}", appids[i]) for i in range(0, 6, 2)],
        )
        # "Tag 0" belongs to the first block, so it plays the part of the
        # Battle Royale tag here.
        found = check_study(model, study, tag="Tag 0")
        assert found.tag_games > 0
        assert found.tag_and_study > 0
        assert found.genre_outside_study >= 0
