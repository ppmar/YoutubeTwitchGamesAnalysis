"""Step 0 — building the `games` table (SPEC section 4, milestone 1)."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import select

from ytcollector.config import GameConfig, StudyConfig, load_study
from ytcollector.games import aggregate_game, load_twitch_months, prepare_games
from ytcollector.models import Game


def game(game_id: str, names: list[str], **kwargs: object) -> GameConfig:
    defaults = {
        "game_name": game_id,
        "yt_query": f'"{game_id}"',
        "match_terms": [game_id],
        "role": "follower",
    }
    defaults.update(kwargs)
    return GameConfig(game_id=game_id, twitch_names=names, **defaults)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def twitch(twitch_csv: object) -> pd.DataFrame:
    return load_twitch_months(twitch_csv)  # type: ignore[arg-type]


class TestLoadTwitchMonths:
    def test_drops_the_row_without_a_game_name(self, twitch: pd.DataFrame) -> None:
        # 21 000 data rows, exactly one of which has no game name (2019-02, rank 162).
        assert len(twitch) == 20_999
        assert twitch["key"].notna().all()

    def test_months_are_iso_prefixes(self, twitch: pd.DataFrame) -> None:
        assert twitch["month"].str.fullmatch(r"\d{4}-\d{2}").all()
        assert twitch["month"].min() == "2016-01"
        assert twitch["month"].max() == "2024-09"

    def test_case_variants_share_a_key(self, twitch: pd.DataFrame) -> None:
        keys = twitch.loc[twitch["game"].isin(["ELDEN RING", "Elden Ring"]), "key"].unique()
        assert len(keys) == 1

    def test_damaged_encodings_share_a_key(self, twitch: pd.DataFrame) -> None:
        damaged = ["Pok<U+00E9>mon GO", "PokÃ©mon GO"]
        keys = twitch.loc[twitch["game"].isin(damaged), "key"].unique()
        assert len(keys) == 1
        names = twitch.loc[twitch["game"].isin(damaged), "game_name"].unique()
        assert list(names) == ["Pokémon GO"]


class TestAggregateGame:
    def test_merges_the_two_pubg_labels(self, twitch: pd.DataFrame) -> None:
        pubg = game("pubg", ["PLAYERUNKNOWN'S BATTLEGROUNDS", "PUBG: BATTLEGROUNDS"])
        assert aggregate_game(twitch, pubg)["months_in_top200"] == 95

    def test_merging_is_not_double_counting(self, twitch: pd.DataFrame) -> None:
        old = aggregate_game(twitch, game("a", ["PLAYERUNKNOWN'S BATTLEGROUNDS"]))
        new = aggregate_game(twitch, game("b", ["PUBG: BATTLEGROUNDS"]))
        both = aggregate_game(twitch, game("c", ["PLAYERUNKNOWN'S BATTLEGROUNDS", "PUBG: BATTLEGROUNDS"]))
        assert old["months_in_top200"] == 55
        assert new["months_in_top200"] == 40
        assert both["months_in_top200"] == 95
        assert both["first_month"] == "2016-09"
        assert both["last_month"] == "2024-09"

    def test_merges_the_naraka_case_variants(self, twitch: pd.DataFrame) -> None:
        naraka = game("naraka", ["NARAKA: BLADEPOINT", "Naraka: Bladepoint"])
        assert aggregate_game(twitch, naraka)["months_in_top200"] == 22

    def test_one_label_is_enough_to_catch_both_case_variants(self, twitch: pd.DataFrame) -> None:
        # The study file need not list every casing: grouping is case-insensitive.
        assert aggregate_game(twitch, game("naraka", ["Naraka: Bladepoint"]))["months_in_top200"] == 22

    def test_peak_month_is_the_best_watched_month(self, twitch: pd.DataFrame) -> None:
        aggregates = aggregate_game(twitch, game("fortnite", ["Fortnite", "Fortnite Zero Build"]))
        assert aggregates["months_in_top200"] == 87
        assert aggregates["peak_month"] == "2018-07"
        assert aggregates["best_rank"] == 1

    def test_valid_from_drops_earlier_months(self, twitch: pd.DataFrame) -> None:
        labels = ["Call of Duty: Warzone", "Call of Duty: Warzone 2.0"]
        unfiltered = aggregate_game(twitch, game("wz", labels))
        filtered = aggregate_game(twitch, game("wz", labels, valid_from="2020-03"))
        # Twitch opened the category in 2019-08 but Warzone shipped in 2020-03: the
        # months in between are Modern Warfare and must not count.
        assert unfiltered["first_month"] == "2019-08"
        assert filtered["first_month"] == "2020-03"
        assert filtered["months_in_top200"] < unfiltered["months_in_top200"]

    def test_unknown_label_yields_empty_aggregates(self, twitch: pd.DataFrame) -> None:
        aggregates = aggregate_game(twitch, game("ghost", ["No Such Game At All"]))
        assert aggregates["months_in_top200"] == 0
        assert aggregates["first_month"] is None
        assert aggregates["hours_watched_total"] is None


class TestPrepareGames:
    def test_battle_royale_has_39_enabled_games(self, session: object, twitch_csv: object) -> None:
        study = load_study("battle_royale")
        result = prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]
        assert result.enabled == 39
        assert result.total == 43
        assert result.created == 43

    def test_every_enabled_game_is_found_in_twitch(self, session: object, twitch_csv: object) -> None:
        study = load_study("battle_royale")
        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]
        rows = session.scalars(select(Game).where(Game.enabled)).all()  # type: ignore[attr-defined]
        unmatched = [r.game_id for r in rows if not r.months_in_top200]
        assert unmatched == []

    def test_second_run_updates_instead_of_duplicating(self, session: object, twitch_csv: object) -> None:
        study = load_study("battle_royale")
        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]
        result = prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]
        assert result.created == 0
        assert result.updated == 43
        assert session.scalar(select(Game).where(Game.game_id == "pubg")) is not None  # type: ignore[attr-defined]

    def test_hand_edited_fields_survive_a_second_run(self, session: object, twitch_csv: object) -> None:
        study = load_study("battle_royale")
        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]

        row = session.scalar(select(Game).where(Game.game_id == "sos"))  # type: ignore[attr-defined]
        row.yt_query = '"SOS" survival battle royale 2017'
        row.match_terms = ["SOS game", "SOS Outbreak"]
        row.enabled = False
        session.commit()  # type: ignore[attr-defined]

        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]

        row = session.scalar(select(Game).where(Game.game_id == "sos"))  # type: ignore[attr-defined]
        assert row.yt_query == '"SOS" survival battle royale 2017'
        assert row.match_terms == ["SOS game", "SOS Outbreak"]
        assert row.enabled is False

    def test_aggregates_are_refreshed_on_a_second_run(self, session: object, twitch_csv: object) -> None:
        study = load_study("battle_royale")
        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]

        row = session.scalar(select(Game).where(Game.game_id == "pubg"))  # type: ignore[attr-defined]
        row.months_in_top200 = 0
        row.notes = "stale"
        session.commit()  # type: ignore[attr-defined]

        prepare_games(session, study, csv_path=twitch_csv)  # type: ignore[arg-type]

        row = session.scalar(select(Game).where(Game.game_id == "pubg"))  # type: ignore[attr-defined]
        assert row.months_in_top200 == 95
        assert row.notes != "stale"

    def test_games_of_another_study_are_untouched(self, session: object, twitch_csv: object) -> None:
        other = StudyConfig(study="souls_like", title="Souls-like", games=[game("elden-ring", ["ELDEN RING"])])
        prepare_games(session, other, csv_path=twitch_csv)  # type: ignore[arg-type]
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        rows = session.scalars(select(Game).where(Game.study == "souls_like")).all()  # type: ignore[attr-defined]
        assert [r.game_id for r in rows] == ["elden-ring"]


class TestColumnWidths:
    def test_hours_watched_total_needs_64_bits(self, twitch: pd.DataFrame) -> None:
        # Fortnite alone is past 6e9 hours; a 32-bit column would overflow on PostgreSQL.
        fortnite = game("fortnite", ["Fortnite", "Fortnite Zero Build"])
        assert aggregate_game(twitch, fortnite)["hours_watched_total"] > 2**31
        assert Game.__table__.c.hours_watched_total.type.python_type is int
        assert type(Game.__table__.c.hours_watched_total.type).__name__ == "BigInteger"
