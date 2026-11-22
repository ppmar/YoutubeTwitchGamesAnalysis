"""Milestone G2 of SPEC_GENRES: NMF over a grid of k, and the four indicators.

The spec's two criteria are the two tests that matter here: on data built from
four blocks of tags the grid must call k = 4 the most stable, and two runs with
the same seeds must give the same numbers. Everything is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("scipy", reason="the genre tools are the optional `genres` extra")
pytest.importorskip("sklearn", reason="the genre tools are the optional `genres` extra")

from ytcollector.genres.load import GenresError
from ytcollector.genres.nmf import (
    NmfParams,
    best_k,
    consensus_matrix,
    cophenetic_correlation,
    dominant,
    fit_grid,
    fit_once,
    mean_pairwise_ari,
    npmi_coherence,
    parse_grid,
    write_k_selection,
)

FAST = NmfParams(max_iter=300, tol=1e-5)


def blocks(n_blocks: int = 4, per_block: int = 30, tags_per_block: int = 6, seed: int = 5):
    """Games drawn from disjoint blocks of tags, L2-normalised like the real matrix."""
    import numpy as np
    from scipy import sparse

    rng = np.random.default_rng(seed)
    n_tags = n_blocks * tags_per_block
    rows = []
    for block in range(n_blocks):
        for _ in range(per_block):
            row = np.zeros(n_tags)
            columns = range(block * tags_per_block, (block + 1) * tags_per_block)
            for column in columns:
                if rng.random() < 0.8:
                    row[column] = rng.random() + 0.25
            if not row.any():
                row[block * tags_per_block] = 1.0
            rows.append(row / np.linalg.norm(row))
    dense = np.array(rows)
    return sparse.csr_matrix(dense)


class TestTheGrid:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("10:60:5", [10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]),
            ("2:5", [2, 3, 4, 5]),
            ("4,8,12", [4, 8, 12]),
            ("25", [25]),
            (25, [25]),
            (None, [10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]),
        ],
    )
    def test_it_reads_every_shape_of_grid(self, spec: object, expected: list[int]) -> None:
        assert parse_grid(spec) == expected  # type: ignore[arg-type]

    @pytest.mark.parametrize("spec", ["60:10:5", "10:60:0", "1,2", "abc", "10:20:30:40"])
    def test_a_grid_that_makes_no_sense_is_refused(self, spec: str) -> None:
        with pytest.raises(GenresError):
            parse_grid(spec)


class TestOneFit:
    def test_the_same_seed_gives_the_same_factorisation(self) -> None:
        import numpy as np

        matrix = blocks()
        first = fit_once(matrix, 4, 0, FAST)
        second = fit_once(matrix, 4, 0, FAST)
        assert np.array_equal(first[0], second[0])
        assert np.array_equal(first[1], second[1])
        assert first[2] == second[2]

    def test_the_relative_error_is_a_share_of_the_matrix_norm(self) -> None:
        matrix = blocks()
        _, _, rel_error, _ = fit_once(matrix, 4, 0, FAST)
        assert 0.0 <= rel_error <= 1.0
        # Four components cannot carry the within-block variation of each game,
        # so the error stays well above zero; what matters is that it is a share.
        assert rel_error < 0.7
        assert rel_error < fit_once(matrix, 2, 0, FAST)[2]

    def test_more_components_explain_more(self) -> None:
        matrix = blocks()
        small = fit_once(matrix, 2, 0, FAST)[2]
        large = fit_once(matrix, 8, 0, FAST)[2]
        assert large < small

    def test_a_game_with_no_weight_is_not_given_a_genre(self) -> None:
        import numpy as np

        W = np.array([[0.0, 0.0], [0.1, 0.9], [0.7, 0.2]])
        assert list(dominant(W)) == [-1, 1, 0]


class TestTheIndicators:
    def test_identical_partitions_score_one(self) -> None:
        import numpy as np

        from ytcollector.genres.nmf import Fit

        labels = np.array([0, 0, 1, 1, 2, 2])
        rows = np.arange(6)
        fits = [
            Fit(k=3, seed=s, kind="seed", labels=labels.copy(), rows=rows,
                rel_error=0.1, n_iter=10)
            for s in range(3)
        ]
        assert mean_pairwise_ari(fits) == pytest.approx(1.0)

    def test_subsamples_are_compared_on_the_games_they_share(self) -> None:
        import numpy as np

        from ytcollector.genres.nmf import Fit

        full = Fit(k=2, seed=0, kind="seed", labels=np.array([0, 0, 1, 1]),
                   rows=np.arange(4), rel_error=0.1, n_iter=5)
        part = Fit(k=2, seed=0, kind="bootstrap", labels=np.array([1, 0]),
                   rows=np.array([1, 2]), rel_error=0.1, n_iter=5)
        # On games 1 and 2 both say "different genres": perfect agreement.
        assert mean_pairwise_ari([full, part]) == pytest.approx(1.0)

    def test_a_consensus_everyone_agrees_on_is_cophenetic_one(self) -> None:
        import numpy as np

        from ytcollector.genres.nmf import Fit

        labels = np.array([0, 0, 1, 1, 2, 2])
        rows = np.arange(6)
        fits = [
            Fit(k=3, seed=s, kind="seed", labels=labels.copy(), rows=rows,
                rel_error=0.1, n_iter=5)
            for s in range(4)
        ]
        consensus = consensus_matrix(fits, rows)
        assert np.allclose(np.diag(consensus), 1.0)
        assert consensus[0, 1] == pytest.approx(1.0)
        assert consensus[0, 5] == pytest.approx(0.0)
        assert cophenetic_correlation(consensus) == pytest.approx(1.0, abs=1e-9)

    def test_disagreement_lowers_the_consensus(self) -> None:
        import numpy as np

        from ytcollector.genres.nmf import Fit

        rows = np.arange(4)
        agree = Fit(k=2, seed=0, kind="seed", labels=np.array([0, 0, 1, 1]), rows=rows,
                    rel_error=0.1, n_iter=5)
        disagree = Fit(k=2, seed=1, kind="seed", labels=np.array([0, 1, 0, 1]), rows=rows,
                       rel_error=0.1, n_iter=5)
        consensus = consensus_matrix([agree, disagree], rows)
        assert consensus[0, 1] == pytest.approx(0.5)

    def test_npmi_is_one_for_tags_that_always_travel_together(self) -> None:
        import numpy as np
        from scipy import sparse

        # Tags 0 and 1 are on the same two games and nowhere else.
        presence = sparse.csr_matrix(
            np.array([[1, 1, 0], [1, 1, 0], [0, 0, 1], [0, 0, 1]], dtype="float64")
        )
        H = np.array([[1.0, 0.9, 0.0]])
        assert npmi_coherence(presence, H, top=2) == pytest.approx(1.0)

    def test_npmi_is_minus_one_for_tags_that_never_meet(self) -> None:
        import numpy as np
        from scipy import sparse

        presence = sparse.csr_matrix(
            np.array([[1, 0], [1, 0], [0, 1], [0, 1]], dtype="float64")
        )
        H = np.array([[1.0, 0.9]])
        assert npmi_coherence(presence, H, top=2) == pytest.approx(-1.0)

    def test_coherence_prefers_the_genres_that_hold_together(self) -> None:
        """A factorisation of block data beats one of a shuffled matrix."""
        import numpy as np
        from scipy import sparse

        matrix = blocks()
        presence = matrix.copy()
        presence.data = np.ones_like(presence.data)

        # Five tags: a block holds six, so the top of a genre stays inside it.
        # Asking for ten would reach into other blocks, where tags never co-occur
        # — which is the measure working, not failing.
        _, H, _, _ = fit_once(matrix, 4, 0, FAST)
        structured = npmi_coherence(presence, H, top=5)

        rng = np.random.default_rng(3)
        noise = sparse.csr_matrix(rng.random(matrix.shape) * (rng.random(matrix.shape) < 0.2))
        noise_presence = noise.copy()
        noise_presence.data = np.ones_like(noise_presence.data)
        _, noise_H, _, _ = fit_once(noise, 4, 0, FAST)
        assert structured > npmi_coherence(noise_presence, noise_H, top=5)


@pytest.fixture(scope="module")
def reports() -> list:
    """One grid over four blocks, shared by the criterion tests below."""
    return fit_grid(blocks(n_blocks=4), ks=[2, 3, 4, 5, 6], seeds=3, params=FAST, n_bootstrap=3)


class TestTheGridFinds4Blocks:
    """The spec's acceptance criterion for G2."""

    def test_four_blocks_make_k_four_the_most_stable(self, reports: list) -> None:
        assert best_k(reports) == 4
        four = next(r for r in reports if r.k == 4)
        assert four.stability > 0.95

    def test_the_error_falls_as_k_rises(self, reports: list) -> None:
        errors = [r.rel_error_mean for r in sorted(reports, key=lambda r: r.k)]
        assert errors == sorted(errors, reverse=True)

    def test_the_true_k_is_where_the_error_stops_falling_steeply(self, reports: list) -> None:
        by_k = {r.k: r.rel_error_mean for r in reports}
        before = by_k[3] - by_k[4]
        after = by_k[4] - by_k[5]
        assert before > after

    def test_every_k_is_scored_on_all_four_indicators(self, reports: list) -> None:
        import math

        for report in reports:
            assert math.isfinite(report.rel_error_mean)
            assert math.isfinite(report.stability)
            assert math.isfinite(report.cophenetic)
            assert math.isfinite(report.coherence)
            assert report.n_seed_fits == 3
            assert report.n_bootstrap_fits == 3


class TestItRepeats:
    def test_two_runs_with_the_same_seeds_give_the_same_numbers(self, tmp_path: Path) -> None:
        import pandas as pd

        matrix = blocks(n_blocks=3, per_block=20)
        rounds = []
        for name in ("first", "second"):
            reports = fit_grid(
                matrix, ks=[3, 4], seeds=2, params=FAST, n_bootstrap=2,
                output_dir=tmp_path / name,
            )
            rounds.append(pd.read_csv(write_k_selection(reports, tmp_path / name)))

        columns = ["k", "rel_error_mean", "rel_error_std", "stability", "cophenetic", "coherence"]
        pd.testing.assert_frame_equal(rounds[0][columns], rounds[1][columns])

    def test_the_fits_are_written_one_file_per_seed(self, tmp_path: Path) -> None:
        import numpy as np

        matrix = blocks(n_blocks=3, per_block=15)
        fit_grid(matrix, ks=[3], seeds=2, params=FAST, n_bootstrap=2, output_dir=tmp_path)

        fits = sorted(p.name for p in (tmp_path / "fits").iterdir())
        assert fits == ["k3_b0.npz", "k3_b1.npz", "k3_s0.npz", "k3_s1.npz"]

        with np.load(tmp_path / "fits" / "k3_s0.npz") as blob:
            assert blob["W"].shape == (matrix.shape[0], 3)
            assert blob["H"].shape == (3, matrix.shape[1])
            assert int(blob["k"]) == 3

    def test_a_subsample_fit_keeps_its_labels_and_its_rows(self, tmp_path: Path) -> None:
        import numpy as np

        matrix = blocks(n_blocks=3, per_block=15)
        fit_grid(matrix, ks=[3], seeds=1, params=FAST, n_bootstrap=1, output_dir=tmp_path)
        with np.load(tmp_path / "fits" / "k3_b0.npz") as blob:
            rows, labels = blob["rows"], blob["labels"]
        assert len(rows) == len(labels)
        assert len(rows) == round(0.8 * matrix.shape[0])
        assert len(set(rows.tolist())) == len(rows)

    def test_the_selection_table_has_a_row_per_k(self, tmp_path: Path) -> None:
        import pandas as pd

        reports = fit_grid(blocks(n_blocks=2, per_block=12), ks=[2, 3], seeds=2,
                           params=FAST, n_bootstrap=2)
        frame = pd.read_csv(write_k_selection(reports, tmp_path))
        assert list(frame["k"]) == [2, 3]
        assert list(frame.columns) == [
            "k", "rel_error_mean", "rel_error_std", "stability", "cophenetic",
            "coherence", "n_seed_fits", "n_bootstrap_fits", "seconds",
        ]


class TestTheParameters:
    def test_the_config_file_supplies_the_defaults(self) -> None:
        from ytcollector.genres.load import load_config

        params = NmfParams.from_config(load_config())
        assert params.max_iter == 500
        assert params.beta_loss == "frobenius"
        assert params.alpha_W == 0.0
        assert params.init == "nndsvda"
        assert params.solver == "cd"

    def test_the_divergence_loss_switches_the_solver(self) -> None:
        assert NmfParams(beta_loss="kullback-leibler").solver == "mu"

    def test_kullback_leibler_also_fits(self) -> None:
        matrix = blocks(n_blocks=2, per_block=10)
        _, _, rel_error, _ = fit_once(
            matrix, 2, 0, NmfParams(beta_loss="kullback-leibler", max_iter=200)
        )
        assert 0.0 <= rel_error <= 1.0

    def test_an_l1_penalty_on_w_leaves_fewer_genres_per_game(self) -> None:
        import numpy as np

        matrix = blocks(n_blocks=4)
        plain, _, _, _ = fit_once(matrix, 6, 0, FAST)
        penalised, _, _, _ = fit_once(
            matrix, 6, 0, NmfParams(max_iter=300, tol=1e-5, alpha_W=0.05, l1_ratio=1.0)
        )
        assert (np.asarray(penalised) > 1e-6).sum() < (np.asarray(plain) > 1e-6).sum()


class TestTheSeedWarning:
    def test_a_deterministic_init_says_the_seeds_cannot_vary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """nndsvda ignores random_state, so stability rests on the subsamples."""
        import logging

        from ytcollector.genres import nmf as nmf_module

        with caplog.at_level(logging.WARNING, logger=nmf_module.__name__):
            fit_grid(blocks(n_blocks=2, per_block=12), ks=[2], seeds=3, params=FAST,
                     n_bootstrap=2)
        assert "deterministic" in caplog.text
        assert "'random'" in caplog.text

    def test_the_seeds_do_give_the_same_fit_under_nndsvda(self) -> None:
        matrix = blocks(n_blocks=3, per_block=20)
        errors = {fit_once(matrix, 3, seed, FAST)[2] for seed in range(3)}
        assert len(errors) == 1

    def test_a_random_init_makes_them_differ_once_the_data_is_noisy(self) -> None:
        """On clean blocks every start finds the same optimum; on noise it does not."""
        import numpy as np
        from scipy import sparse

        rng = np.random.default_rng(9)
        dense = rng.random((300, 40)) * (rng.random((300, 40)) < 0.25)
        dense /= np.maximum(1e-9, np.linalg.norm(dense, axis=1, keepdims=True))
        matrix = sparse.csr_matrix(dense)

        params = NmfParams(init="random", max_iter=200, tol=1e-5)
        errors = {round(fit_once(matrix, 8, seed, params)[2], 9) for seed in range(4)}
        assert len(errors) > 1
