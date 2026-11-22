"""Milestone G3 of SPEC_GENRES: genres, membership and hierarchy.

The spec's criteria: a game's shares sum to 1, each synthetic block becomes a
genre, and the hierarchy links two blocks merged at a smaller k to their common
parent. All synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("scipy", reason="the genre tools are the optional `genres` extra")
pytest.importorskip("sklearn", reason="the genre tools are the optional `genres` extra")

from tests.test_genres_nmf import FAST, blocks
from ytcollector.genres.load import GenresConfig, GenresError
from ytcollector.genres.matrix import BuildResult, write_matrix
from ytcollector.genres.nmf import fit_grid
from ytcollector.genres.select import (
    MIN_MEMBERSHIP,
    available_ks,
    final_k,
    game_genre,
    genre_definition,
    genre_name,
    hierarchy,
    label_of,
    load_model,
    memberships,
    normalise,
    report_markdown,
    threshold_column,
    threshold_counts,
    write_report,
    write_tables,
    write_tree,
)

THRESHOLDS = [0.2, 0.3, 0.5]


def written_matrix(tmp_path: Path, n_blocks: int = 4, per_block: int = 30) -> Path:
    """A block matrix on disk with its labels, as milestone G1 writes it."""
    import pandas as pd

    matrix = blocks(n_blocks=n_blocks, per_block=per_block)
    n_games, n_tags = matrix.shape
    rows = pd.DataFrame(
        {
            "appid": [1000 + i for i in range(n_games)],
            "name": pd.Series([f"Jeu {i}" for i in range(n_games)], dtype="string"),
            "release_date": pd.Series(
                [f"{2015 + (i % 8)}-0{1 + (i % 9)}-15" for i in range(n_games)], dtype="string"
            ),
            "total_votes": [500 + i for i in range(n_games)],
        }
    )
    cols = pd.DataFrame(
        {
            "tag": pd.Series([f"Tag {j}" for j in range(n_tags)], dtype="string"),
            "family": pd.Series(["gameplay"] * n_tags, dtype="string"),
            "df": [10] * n_tags,
            "idf": [1.5] * n_tags,
        }
    )
    write_matrix(
        BuildResult(matrix=matrix, rows=rows, cols=cols, games_in=n_games,
                    empty_rows=0, tags_total=n_tags),
        tmp_path,
    )
    return tmp_path


@pytest.fixture(scope="module")
def fitted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Four blocks, fitted over k = 2, 4 and 6 — enough for a hierarchy."""
    directory = tmp_path_factory.mktemp("genres")
    written_matrix(directory)
    fit_grid(
        blocks(n_blocks=4),
        ks=[2, 4, 6],
        seeds=2,
        params=FAST,
        n_bootstrap=2,
        output_dir=directory,
    )
    return directory


class TestNormalisation:
    def test_each_genre_peaks_at_one_and_the_product_is_unchanged(self) -> None:
        import numpy as np

        W = np.array([[1.0, 2.0], [0.5, 0.25]])
        H = np.array([[4.0, 2.0, 0.0], [0.0, 1.0, 5.0]])
        scaled_W, scaled_H = normalise(W, H)
        assert np.allclose(scaled_H.max(axis=1), 1.0)
        assert np.allclose(scaled_W @ scaled_H, W @ H)

    def test_a_genre_with_no_tag_weight_does_not_divide_by_zero(self) -> None:
        import numpy as np

        W = np.array([[1.0, 1.0]])
        H = np.array([[2.0, 0.0], [0.0, 0.0]])
        _, scaled_H = normalise(W, H)
        assert np.isfinite(scaled_H).all()

    def test_shares_sum_to_one(self) -> None:
        import numpy as np

        W = np.array([[3.0, 1.0], [0.0, 2.0], [0.0, 0.0]])
        shares, empty = memberships(W)
        assert list(empty) == [False, False, True]
        assert shares[0].sum() == pytest.approx(1.0)
        assert shares[1].sum() == pytest.approx(1.0)
        assert shares[2].sum() == 0.0

    def test_the_label_is_the_three_heaviest_tags(self) -> None:
        import numpy as np
        import pandas as pd

        cols = pd.DataFrame({"tag": ["a", "b", "c", "d"]})
        assert label_of(np.array([0.1, 1.0, 0.5, 0.9]), cols) == "b + d + c"


class TestTheModelOfAK:
    def test_it_takes_the_seed_with_the_lowest_error(self, fitted: Path) -> None:
        import numpy as np

        errors = {}
        for path in sorted(fitted.glob("fits/k4_s*.npz")):
            with np.load(path) as blob:
                errors[int(blob["seed"])] = float(blob["rel_error"])
        model = load_model(fitted, 4)
        assert model.seed == min(errors, key=lambda s: errors[s])
        assert model.rel_error == pytest.approx(min(errors.values()))

    def test_every_game_keeps_shares_that_sum_to_one(self, fitted: Path) -> None:
        import numpy as np

        model = load_model(fitted, 4)
        totals = model.shares.sum(axis=1)
        assert np.allclose(totals[totals > 0], 1.0)

    def test_an_unknown_k_names_the_ones_that_exist(self, fitted: Path) -> None:
        with pytest.raises(GenresError) as error:
            load_model(fitted, 7)
        assert "2, 4, 6" in str(error.value)

    def test_the_grid_is_read_from_the_fits(self, fitted: Path) -> None:
        assert available_ks(fitted) == [2, 4, 6]


class TestEachBlockBecomesAGenre:
    """The spec's acceptance criterion for G3."""

    def test_four_blocks_give_four_genres_each_with_its_own_tags(self, fitted: Path) -> None:
        import numpy as np

        model = load_model(fitted, 4)
        # Each genre's heaviest tags must all sit in one block of six.
        for genre in range(4):
            top = np.argsort(model.H[genre])[::-1][:3]
            assert len({int(t) // 6 for t in top}) == 1
        # And the four genres must cover four different blocks.
        owners = {int(np.argmax(model.H[genre]) // 6) for genre in range(4)}
        assert owners == {0, 1, 2, 3}

    def test_games_of_one_block_share_a_dominant_genre(self, fitted: Path) -> None:

        model = load_model(fitted, 4)
        dominant = model.shares.argmax(axis=1)
        for block in range(4):
            members = dominant[block * 30 : (block + 1) * 30]
            assert len(set(members.tolist())) == 1

    def test_the_definition_table_describes_each_genre(self, fitted: Path) -> None:
        model = load_model(fitted, 4)
        definition = genre_definition(model)
        assert list(definition.columns) == [
            "genre_id", "name", "label", "top_tags", "top_games", "n_dominant"
        ]
        assert list(definition["name"]) == ["Genre 1", "Genre 2", "Genre 3", "Genre 4"]
        assert len(definition) == 4
        assert definition["n_dominant"].sum() == len(model.rows)
        assert all(len(row) <= 10 for row in definition["top_tags"])
        assert all(len(row) <= 10 for row in definition["top_games"])

    def test_the_membership_table_holds_one_row_per_pair_above_the_floor(
        self, fitted: Path
    ) -> None:
        model = load_model(fitted, 4)
        membership = game_genre(model)
        assert list(membership.columns) == ["appid", "genre_id", "share"]
        assert (membership["share"] > MIN_MEMBERSHIP).all()
        assert set(membership["appid"]) <= set(model.rows["appid"])
        # Each game of a clean block belongs mostly to one genre.
        assert membership.groupby("appid")["share"].max().min() > 0.5

    def test_the_thresholds_are_counted_per_genre(self, fitted: Path) -> None:
        model = load_model(fitted, 4)
        counts = threshold_counts(model, THRESHOLDS)
        assert list(counts.columns) == ["genre_id"] + [threshold_column(t) for t in THRESHOLDS]
        # A higher bar can only keep fewer games.
        assert (counts["n_share_ge_0p2"] >= counts["n_share_ge_0p5"]).all()
        assert counts["n_share_ge_0p5"].sum() <= len(model.rows)


class TestTheHierarchy:
    def test_each_genre_is_linked_to_its_nearest_parent(self, fitted: Path) -> None:
        tree = hierarchy(fitted)
        assert list(tree.columns) == [
            "k", "genre_id", "parent_k", "parent_genre_id", "similarity"
        ]
        assert set(tree["k"]) == {2, 4, 6}
        # The smallest k of the grid has no parent.
        first = tree[tree["k"] == 2]
        assert first["parent_k"].isna().all()
        # Every other genre has one, with a similarity in [0, 1].
        rest = tree[tree["k"] > 2]
        assert rest["parent_genre_id"].notna().all()
        assert ((rest["similarity"] >= 0) & (rest["similarity"] <= 1)).all()

    def test_two_blocks_merged_at_a_smaller_k_point_at_the_same_parent(
        self, fitted: Path
    ) -> None:
        """k=2 can only hold two of the four blocks, so genres must share parents."""
        tree = hierarchy(fitted)
        at_four = tree[tree["k"] == 4]
        parents = at_four["parent_genre_id"].tolist()
        assert len(set(parents)) < len(parents)

    def test_a_genre_is_closest_to_the_parent_that_shares_its_tags(
        self, fitted: Path
    ) -> None:
        import numpy as np

        tree = hierarchy(fitted)
        four = load_model(fitted, 4)
        six = load_model(fitted, 6)
        for row in tree[tree["k"] == 6].itertuples(index=False):
            mine = six.H[int(row.genre_id)]
            theirs = four.H[int(row.parent_genre_id)]
            assert int(np.argmax(mine)) // 6 == int(np.argmax(theirs)) // 6


class TestTheReport:
    def test_it_says_what_it_is_and_lists_every_genre(self, fitted: Path) -> None:
        model = load_model(fitted, 4)
        text = report_markdown(model, THRESHOLDS, hierarchy(fitted))
        assert text.startswith("# Genres à k = 4")
        assert "numérotés, pas nommés" in text
        assert "part ≥ 0.2" in text
        for genre in range(1, 5):
            assert f"### Genre {genre} —" in text
        assert "accord de confidentialité" in text

    def test_it_is_written_next_to_the_matrix(self, fitted: Path) -> None:
        path = write_report(load_model(fitted, 4), fitted, THRESHOLDS)
        assert path.name == "report_k4.md"
        assert path.read_text(encoding="utf-8").startswith("# Genres")

    def test_the_tables_land_as_parquet(self, fitted: Path) -> None:
        import pandas as pd

        paths = write_tables(load_model(fitted, 4), fitted, THRESHOLDS)
        definition = pd.read_parquet(paths["definition"])
        membership = pd.read_parquet(paths["membership"])
        assert "n_share_ge_0p3" in definition.columns
        assert len(definition) == 4
        assert membership["genre_id"].between(0, 3).all()

    def test_the_tree_lands_as_csv(self, fitted: Path) -> None:
        import pandas as pd

        path = write_tree(fitted)
        assert path is not None
        assert list(pd.read_csv(path)["k"].unique()) == [2, 4, 6]


class TestSelectRefusesToGuess:
    def test_a_null_k_final_stops_it(self, tmp_path: Path) -> None:
        config = GenresConfig(
            client_dir=tmp_path, output_dir=tmp_path, selection={"k_final": None}
        )
        with pytest.raises(GenresError) as error:
            final_k(config)
        assert "decision" in str(error.value)
        assert "k_selection.csv" in str(error.value)

    def test_a_chosen_k_is_returned(self, tmp_path: Path) -> None:
        config = GenresConfig(
            client_dir=tmp_path, output_dir=tmp_path, selection={"k_final": 25}
        )
        assert final_k(config) == 25

    def test_the_shipped_config_still_leaves_it_open(self) -> None:
        from ytcollector.genres.load import load_config

        with pytest.raises(GenresError):
            final_k(load_config())


class TestGenreNames:
    """Numbered, not named: nothing has been decided about what a genre is."""

    def test_they_are_numbered_from_one(self) -> None:
        assert genre_name(0) == "Genre 1"
        assert genre_name(24) == "Genre 25"

    def test_the_tags_stay_beside_the_number(self, fitted: Path) -> None:
        from ytcollector.genres.select import genre_definition

        definition = genre_definition(load_model(fitted, 4))
        for _, row in definition.iterrows():
            assert row["name"].startswith("Genre ")
            assert " + " in row["label"]
            assert len(row["top_tags"]) > 0
