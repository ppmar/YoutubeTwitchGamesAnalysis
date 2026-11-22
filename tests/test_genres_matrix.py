"""Milestone G1 of SPEC_GENRES: the TF-IDF game x tag matrix.

No test reads ``data/client/``. The inputs are written in the fixtures: a small
hand-checkable example, plus a block-structured draw with a fixed seed.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

pytest.importorskip("scipy", reason="the genre tools are the optional `genres` extra")

from ytcollector.genres import load as load_module
from ytcollector.genres.load import (
    GenresConfig,
    GenresError,
    TagFamilies,
    load_config,
    load_game_tags,
    load_tag_families,
    read_reference,
    read_tag_votes,
    tag_game_counts,
    write_tag_families_draft,
)
from ytcollector.genres.matrix import build_matrix, load_matrix, write_matrix

# Four games, four tags, votes picked so TF-IDF can be worked out by hand.
# A: 60 Battle Royale, 30 Shooter, 10 Pixel Graphics  (100 votes)
# B: 50 Battle Royale, 50 Shooter                     (100 votes)
# C: 90 Farming, 10 Shooter                           (100 votes)
# D: 10 Shooter                                       (10 votes, below the floor)
TAG_ROWS = [
    (60, "Battle Royale", 10),
    (30, " Shooter ", 10),  # stray spaces, as in the export
    (10, "Pixel Graphics", 10),
    (50, "Battle Royale", 20),
    (30, "Shooter", 20),
    (20, "Shooter", 20),  # a duplicated pair, to be summed
    (90, "Farming", 30),
    (10, "Shooter", 30),
    (10, "Shooter", 40),
]

FAMILIES = {
    "Battle Royale": "gameplay",
    "Shooter": "gameplay",
    "Farming": "gameplay",
    "Pixel Graphics": "visuals",
}


def write_client_data(directory: Path, rows: list[tuple[int, str, int]] | None = None) -> None:
    import pandas as pd

    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows or TAG_ROWS, columns=["nb", "tag", "appid"]).to_csv(
        directory / "steam_tags.csv", index=False
    )
    pd.DataFrame(
        {
            "appid": [10, 20, 30, 40],
            "name": ["Alpha", "Bravo", "Charlie", "Delta"],
            "releaseDate": ["2019-01-01", "2020-06-15", "2021-03-02", "2022-11-30"],
            "copiesSold": [1000, 2000, 3000, 4000],
        }
    ).to_csv(directory / "steam_reference.csv", index=False)


def write_families(path: Path, mapping: dict[str, str] | None = None, used: str = "gameplay") -> Path:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "families": ["gameplay", "players", "visuals", "themes", "business", "other"],
                "used_for_factorization": [used],
                "tags": mapping or FAMILIES,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(tmp_path: Path) -> GenresConfig:
    write_client_data(tmp_path / "client")
    return GenresConfig(
        client_dir=tmp_path / "client",
        output_dir=tmp_path / "out",
        min_total_votes=100,
        min_tag_share=0.02,
    )


@pytest.fixture
def families(tmp_path: Path) -> TagFamilies:
    return load_tag_families(write_families(tmp_path / "config" / "tag_families.yml"))


class TestReading:
    def test_tags_are_stripped_and_duplicates_summed(self, config: GenresConfig) -> None:
        votes = read_tag_votes(config, ignore_expected=True)
        pairs = {(int(r.appid), str(r.tag)): int(r.nb) for r in votes.itertuples(index=False)}
        assert pairs[(10, "Shooter")] == 30  # " Shooter " stripped
        assert pairs[(20, "Shooter")] == 50  # 30 + 20, the duplicated pair

    def test_rows_without_an_appid_or_a_tag_are_dropped(self, tmp_path: Path) -> None:
        import pandas as pd

        client = tmp_path / "client"
        write_client_data(client)
        pd.DataFrame(
            [(10, "Shooter", 10), (5, "", 10), (5, "Shooter", None), (0, "Shooter", 20)],
            columns=["nb", "tag", "appid"],
        ).to_csv(client / "steam_tags.csv", index=False)
        config = GenresConfig(client_dir=client, output_dir=tmp_path / "out")
        votes = read_tag_votes(config, ignore_expected=True)
        assert len(votes) == 1
        assert int(votes.iloc[0]["appid"]) == 10

    def test_the_reference_gives_names_and_dates(self, config: GenresConfig) -> None:
        reference = read_reference(config, ignore_expected=True)
        assert list(reference.columns) == ["appid", "name", "release_date"]
        assert reference.set_index("appid").loc[20, "name"] == "Bravo"
        assert reference.set_index("appid").loc[20, "release_date"] == "2020-06-15"

    def test_light_games_are_filtered_out_and_counted(self, config: GenresConfig) -> None:
        tags = load_game_tags(config, ignore_expected=True)
        assert list(tags.games["appid"]) == [10, 20, 30]  # Delta has 10 votes
        assert tags.games_before_filter == 4
        assert tags.games_dropped == 1
        assert set(tags.pairs["appid"]) == {10, 20, 30}

    def test_the_threshold_is_configurable(self, config: GenresConfig) -> None:
        loose = GenresConfig(
            client_dir=config.client_dir, output_dir=config.output_dir, min_total_votes=5
        )
        assert len(load_game_tags(loose, ignore_expected=True).games) == 4

    def test_a_missing_directory_says_what_to_do_without_naming_the_machine(
        self, tmp_path: Path
    ) -> None:
        config = GenresConfig(client_dir=tmp_path / "gone", output_dir=tmp_path / "out")
        with pytest.raises(GenresError) as error:
            read_tag_votes(config)
        assert "steam_tags.csv" in str(error.value)
        assert str(tmp_path) not in str(error.value)

    def test_a_half_filled_directory_names_the_missing_file(self, tmp_path: Path) -> None:
        client = tmp_path / "client"
        client.mkdir()
        (client / "steam_tags.csv").write_text("nb,tag,appid\n")
        config = GenresConfig(client_dir=client, output_dir=tmp_path / "out")
        with pytest.raises(GenresError, match="steam_reference.csv"):
            read_tag_votes(config)


class TestTheExpectedCounts:
    def test_a_wide_gap_stops_the_run(self, config: GenresConfig) -> None:
        from ytcollector.genres.load import Expected

        strict = GenresConfig(
            client_dir=config.client_dir,
            output_dir=config.output_dir,
            expected=Expected(tag_rows=3_450_000, tag_games=206_428, tags=477),
        )
        with pytest.raises(GenresError, match="exploration notebook"):
            read_tag_votes(strict)

    def test_it_can_be_waved_through_once_understood(self, config: GenresConfig) -> None:
        from ytcollector.genres.load import Expected

        strict = GenresConfig(
            client_dir=config.client_dir,
            output_dir=config.output_dir,
            expected=Expected(tag_rows=3_450_000),
        )
        assert len(read_tag_votes(strict, ignore_expected=True)) > 0

    def test_a_small_gap_passes_and_is_logged(
        self, config: GenresConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        from ytcollector.genres.load import Expected

        close = GenresConfig(
            client_dir=config.client_dir,
            output_dir=config.output_dir,
            # 9 rows in the fixture; 9 expected, within tolerance.
            expected=Expected(tag_rows=9, tag_games=4, tags=4, tolerance=0.05),
        )
        with caplog.at_level(logging.INFO, logger=load_module.__name__):
            read_tag_votes(close)
        assert "tag rows" in caplog.text


class TestTheWeights:
    def test_the_values_match_a_calculation_made_by_hand(
        self, config: GenresConfig, families: TagFamilies
    ) -> None:
        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        dense = result.matrix.toarray()
        appids = list(result.rows["appid"])
        tags = list(result.cols["tag"])

        # Three games kept, so N = 3. Battle Royale is on A and B, Shooter on all
        # three, Farming on C alone. Pixel Graphics is a `visuals` tag: out.
        assert tags == ["Battle Royale", "Farming", "Shooter"]
        idf = {tag: math.log((1 + 3) / (1 + df)) + 1 for tag, df in zip(tags, [2, 1, 3])}
        assert dict(zip(result.cols["tag"], result.cols["df"])) == {
            "Battle Royale": 2, "Farming": 1, "Shooter": 3
        }

        # Alpha: TF over all four of its tags, including the one dropped later.
        row = [0.60 * idf["Battle Royale"], 0.0, 0.30 * idf["Shooter"]]
        norm = math.sqrt(sum(v * v for v in row))
        expected = [v / norm for v in row]
        assert dense[appids.index(10)] == pytest.approx(expected, abs=1e-12)

    def test_the_share_is_taken_before_the_family_filter(
        self, config: GenresConfig, families: TagFamilies
    ) -> None:
        """Alpha's 60 votes are 60% of 100, not 60/90 of its gameplay tags."""
        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        dense = result.matrix.toarray()
        appids = list(result.rows["appid"])
        tags = list(result.cols["tag"])

        br = dense[appids.index(10)][tags.index("Battle Royale")]
        shooter = dense[appids.index(10)][tags.index("Shooter")]
        idf_br = math.log(4 / 3) + 1
        idf_shooter = math.log(4 / 4) + 1
        assert br / shooter == pytest.approx((0.60 * idf_br) / (0.30 * idf_shooter))

    def test_every_row_has_length_one(self, config: GenresConfig, families: TagFamilies) -> None:
        import numpy as np

        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        norms = np.sqrt(result.matrix.multiply(result.matrix).sum(axis=1)).A.ravel()
        assert np.allclose(norms, 1.0)

    def test_no_appid_appears_twice(self, config: GenresConfig, families: TagFamilies) -> None:
        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        appids = list(result.rows["appid"])
        assert len(appids) == len(set(appids))

    def test_a_share_under_the_floor_is_zeroed(
        self, tmp_path: Path, families: TagFamilies
    ) -> None:
        import pandas as pd

        client = tmp_path / "client2"
        write_client_data(client)
        pd.DataFrame(
            [(990, "Battle Royale", 10), (10, "Shooter", 10), (100, "Farming", 20)],
            columns=["nb", "tag", "appid"],
        ).to_csv(client / "steam_tags.csv", index=False)
        config = GenresConfig(
            client_dir=client, output_dir=tmp_path / "out", min_tag_share=0.02
        )
        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        # Shooter is 1% of game 10's votes: gone, and with it the whole column.
        assert list(result.cols["tag"]) == ["Battle Royale", "Farming"]

    def test_rows_left_empty_are_dropped_and_counted(
        self, tmp_path: Path, families: TagFamilies
    ) -> None:
        import pandas as pd

        client = tmp_path / "client3"
        write_client_data(client)
        pd.DataFrame(
            [(100, "Battle Royale", 10), (100, "Pixel Graphics", 20)],
            columns=["nb", "tag", "appid"],
        ).to_csv(client / "steam_tags.csv", index=False)
        config = GenresConfig(client_dir=client, output_dir=tmp_path / "out")
        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        assert result.shape[0] == 1  # game 20 holds only a `visuals` tag
        assert result.empty_rows == 1
        assert list(result.rows["appid"]) == [10]

    def test_the_idf_follows_the_games_kept(self, config: GenresConfig, families: TagFamilies) -> None:
        """Lowering the vote floor adds a game, which changes every weight."""
        loose = GenresConfig(
            client_dir=config.client_dir,
            output_dir=config.output_dir,
            min_total_votes=5,
        )
        strict = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        wide = build_matrix(loose, families, load_game_tags(loose, ignore_expected=True))
        idf_strict = dict(zip(strict.cols["tag"], strict.cols["idf"]))
        idf_wide = dict(zip(wide.cols["tag"], wide.cols["idf"]))
        assert idf_strict["Battle Royale"] != idf_wide["Battle Royale"]

    def test_an_unclassified_tag_stops_the_build_and_lists_it(
        self, config: GenresConfig, tmp_path: Path
    ) -> None:
        partial = load_tag_families(
            write_families(
                tmp_path / "partial.yml",
                mapping={"Battle Royale": "gameplay", "Shooter": "gameplay"},
            )
        )
        with pytest.raises(GenresError) as error:
            build_matrix(config, partial, load_game_tags(config, ignore_expected=True))
        assert "Farming" in str(error.value)
        assert "Pixel Graphics" in str(error.value)

    def test_two_builds_give_the_same_matrix(
        self, config: GenresConfig, families: TagFamilies
    ) -> None:
        import numpy as np

        tags = load_game_tags(config, ignore_expected=True)
        first = build_matrix(config, families, tags)
        second = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        assert list(first.cols["tag"]) == list(second.cols["tag"])
        assert np.array_equal(first.matrix.toarray(), second.matrix.toarray())

    def test_a_block_structure_survives_the_weighting(self, tmp_path: Path) -> None:
        """Two blocks of tags, no overlap: the two groups must stay separable."""
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(11)
        client = tmp_path / "blocks"
        write_client_data(client)
        rows = []
        for game in range(40):
            block = game % 2
            for tag in range(block * 4, block * 4 + 4):
                rows.append((int(rng.integers(40, 300)), f"Tag {tag}", 1000 + game))
        pd.DataFrame(rows, columns=["nb", "tag", "appid"]).to_csv(
            client / "steam_tags.csv", index=False
        )
        mapping = {f"Tag {i}": "gameplay" for i in range(8)}
        families = load_tag_families(write_families(tmp_path / "blocks.yml", mapping))
        config = GenresConfig(client_dir=client, output_dir=tmp_path / "out")

        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        dense = result.matrix.toarray()
        first = dense[::2]  # the even games, block 0
        second = dense[1::2]
        assert np.allclose(first[:, 4:], 0)
        assert np.allclose(second[:, :4], 0)
        assert (first @ second.T).max() == pytest.approx(0.0, abs=1e-12)


class TestWritingItDown:
    def test_the_three_files_are_written_and_read_back(
        self, config: GenresConfig, families: TagFamilies
    ) -> None:
        import numpy as np

        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        paths = write_matrix(result, config.output_dir)
        assert all(p.is_file() for p in paths.values())

        again = load_matrix(config.output_dir)
        assert np.allclose(again.matrix.toarray(), result.matrix.toarray())
        assert list(again.rows["appid"]) == list(result.rows["appid"])
        assert list(again.cols["tag"]) == list(result.cols["tag"])

    def test_the_row_table_carries_what_the_page_shows(
        self, config: GenresConfig, families: TagFamilies
    ) -> None:
        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        assert list(result.rows.columns) == ["appid", "name", "release_date", "total_votes"]
        assert list(result.cols.columns) == ["tag", "family", "df", "idf"]
        assert result.rows.set_index("appid").loc[10, "name"] == "Alpha"
        assert int(result.rows.set_index("appid").loc[10, "total_votes"]) == 100

    def test_the_web_view_can_read_what_was_written(
        self, config: GenresConfig, families: TagFamilies, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The matrix tab of the page reads this build, so hold the contract."""
        from ytcollector.web import matrix_view

        result = build_matrix(config, families, load_game_tags(config, ignore_expected=True))
        write_matrix(result, config.output_dir)
        monkeypatch.setenv("YTC_GENRES_LOCAL", "1")
        monkeypatch.setenv("YTC_GENRES_DIR", str(config.output_dir))

        payload = matrix_view.read_submatrix(games=10, tags=10)
        assert payload["available"] is True
        assert [g["name"] for g in payload["games"]] == ["Alpha", "Bravo", "Charlie"]
        assert payload["source"]["games"] == 3

    def test_a_missing_build_says_which_command_makes_it(self, tmp_path: Path) -> None:
        with pytest.raises(GenresError, match="build-matrix"):
            load_matrix(tmp_path / "nothing")


class TestTheTagFamiliesDraft:
    def test_every_tag_starts_in_other_most_common_first(
        self, config: GenresConfig, tmp_path: Path
    ) -> None:
        votes = read_tag_votes(config, ignore_expected=True)
        where = tmp_path / "draft.yml"
        written = write_tag_families_draft(tag_game_counts(votes), where)
        assert written == 4

        families = load_tag_families(where)
        assert set(families.tags.values()) == {"other"}
        assert families.used_for_factorization == ["gameplay", "players"]
        # Shooter is on three games, Battle Royale on two: Shooter comes first.
        order = [line.split('"')[1] for line in where.read_text().splitlines() if '": ' in line]
        assert order[0] == "Shooter"
        assert set(order) == {"Shooter", "Battle Royale", "Farming", "Pixel Graphics"}

    def test_awkward_tag_names_round_trip(self, tmp_path: Path) -> None:
        import pandas as pd

        counts = pd.DataFrame(
            {"tag": ['Rogue"lite', "Action: RPG", "4X", "日本語"], "games": [4, 3, 2, 1]}
        )
        where = tmp_path / "awkward.yml"
        write_tag_families_draft(counts, where)
        assert set(load_tag_families(where).tags) == set(counts["tag"])


class TestTheConfiguration:
    def test_the_shipped_file_parses_and_keeps_the_defaults(self) -> None:
        config = load_config()
        assert config.client_dir == Path("data/client")
        assert config.output_dir == Path("data/processed/genres")
        assert config.min_total_votes == 100
        assert config.min_tag_share == pytest.approx(0.02)
        # Left for the team to settle (SPEC_GENRES section 12).
        assert config.selection["k_final"] is None

    def test_a_family_that_is_not_declared_is_refused(self, tmp_path: Path) -> None:
        import yaml

        path = tmp_path / "bad.yml"
        path.write_text(
            yaml.safe_dump(
                {
                    "families": ["gameplay", "other"],
                    "used_for_factorization": ["gameplay"],
                    "tags": {"Shooter": "invented"},
                }
            )
        )
        with pytest.raises(GenresError, match="invented"):
            load_tag_families(path)

    def test_factorising_an_undeclared_family_is_refused(self, tmp_path: Path) -> None:
        import yaml

        path = tmp_path / "bad2.yml"
        path.write_text(
            yaml.safe_dump(
                {
                    "families": ["gameplay", "other"],
                    "used_for_factorization": ["players"],
                    "tags": {"Shooter": "gameplay"},
                }
            )
        )
        with pytest.raises(GenresError, match="players"):
            load_tag_families(path)

    def test_a_missing_families_file_names_the_command_that_writes_it(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(GenresError, match="init-tag-families"):
            load_tag_families(tmp_path / "absent.yml")
