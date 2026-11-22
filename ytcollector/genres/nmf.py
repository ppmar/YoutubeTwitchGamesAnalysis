"""NMF over a grid of k, and the four indicators that compare them.

SPEC_GENRES section 5. The factorisation has no unique solution, so nothing here
fits once: every k is fitted with several seeds and several subsamples, and what
is written out is how much the answers agree.

Two choices worth knowing:

* ``W`` and ``H`` are stored as float32. At the full size a single ``W`` is
  194k x k doubles, and the grid holds dozens of them; half the precision of a
  factorisation that converged to 1e-4 costs nothing and halves the disk.
* A bootstrap fit keeps only its dominant-genre labels and the rows it saw,
  which is all stability needs — storing its ``W`` would double the disk for
  nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ytcollector.genres.load import GenresConfig, GenresError, display

log = logging.getLogger(__name__)

FITS_DIR = "fits"
K_SELECTION_FILE = "k_selection.csv"
# The consensus matrix is quadratic in the number of games, so it is read on a
# sample. The seed is fixed so two runs sample the same games.
CONSENSUS_SAMPLE = 5000
CONSENSUS_SEED = 20261122
BOOTSTRAP_SEED = 777
TOP_TAGS_FOR_COHERENCE = 10


@dataclass(frozen=True)
class NmfParams:
    """What is passed to scikit-learn, read from ``config/genres.yml``."""

    max_iter: int = 500
    beta_loss: str = "frobenius"
    alpha_W: float = 0.0
    l1_ratio: float = 0.0
    init: str = "nndsvda"
    tol: float = 1e-4

    @classmethod
    def from_config(cls, config: GenresConfig) -> NmfParams:
        nmf = config.nmf or {}
        return cls(
            max_iter=int(nmf.get("max_iter", 500)),
            beta_loss=str(nmf.get("beta_loss", "frobenius")),
            alpha_W=float(nmf.get("alpha_W", 0.0)),
            l1_ratio=float(nmf.get("l1_ratio", 0.0)),
            init=str(nmf.get("init", "nndsvda")),
            tol=float(nmf.get("tol", 1e-4)),
        )

    @property
    def solver(self) -> str:
        # The coordinate-descent solver only knows the Frobenius loss.
        return "cd" if self.beta_loss == "frobenius" else "mu"


def parse_grid(spec: str | int | None, default: str = "10:60:5") -> list[int]:
    """``"10:60:5"``, ``"10,20,30"`` or ``25`` into a list of k, ends included."""
    raw = str(spec if spec not in (None, "") else default).strip()
    try:
        if ":" in raw:
            parts = [int(p) for p in raw.split(":")]
            if len(parts) == 2:
                start, stop, step = parts[0], parts[1], 1
            elif len(parts) == 3:
                start, stop, step = parts
            else:
                raise ValueError
            if step <= 0 or stop < start:
                raise ValueError
            values = list(range(start, stop + 1, step))
        else:
            values = [int(p) for p in raw.replace(" ", "").split(",") if p]
    except ValueError:
        raise GenresError(
            f"cannot read the k grid {raw!r}; expected start:stop:step, "
            "a comma-separated list, or a single number"
        ) from None
    if not values or any(v < 2 for v in values):
        raise GenresError(f"the k grid {raw!r} must hold values of 2 or more")
    return sorted(set(values))


@dataclass
class Fit:
    """One factorisation: its labels, its error, and where it was written."""

    k: int
    seed: int
    kind: str  # "seed" or "bootstrap"
    labels: Any  # dominant genre per row that this fit saw
    rows: Any  # the row indices it saw, in label order
    rel_error: float
    n_iter: int
    path: Path | None = None
    components: Any = None  # H, kept only for the seed fits


@dataclass
class KReport:
    """What ``k_selection.csv`` holds for one k."""

    k: int
    rel_error_mean: float
    rel_error_std: float
    stability: float
    cophenetic: float
    coherence: float
    n_seed_fits: int
    n_bootstrap_fits: int
    seconds: float
    extra: dict[str, Any] = field(default_factory=dict)


def _nmf_model(k: int, seed: int, params: NmfParams) -> Any:
    from sklearn.decomposition import NMF

    return NMF(
        n_components=k,
        init=params.init,
        random_state=seed,
        max_iter=params.max_iter,
        beta_loss=params.beta_loss,
        solver=params.solver,
        alpha_W=params.alpha_W,
        l1_ratio=params.l1_ratio,
        tol=params.tol,
    )


def fit_once(matrix: Any, k: int, seed: int, params: NmfParams) -> tuple[Any, Any, float, int]:
    """Fit one factorisation. Returns ``W``, ``H``, the relative error and iterations."""
    import warnings

    import numpy as np
    from sklearn.exceptions import ConvergenceWarning

    model = _nmf_model(k, seed, params)
    with warnings.catch_warnings():
        # Hitting max_iter is reported through the indicators, not as noise.
        warnings.simplefilter("ignore", ConvergenceWarning)
        W = model.fit_transform(matrix)
    H = model.components_

    # The relative error is defined on the Frobenius norm whatever the loss
    # being minimised, so that two losses stay comparable (SPEC_GENRES 5).
    residual = matrix - (W @ H)
    numerator = float(np.sqrt((np.asarray(residual) ** 2).sum()))
    denominator = float(np.sqrt(matrix.multiply(matrix).sum()))
    rel_error = numerator / denominator if denominator else 0.0
    return W, H, rel_error, int(model.n_iter_)


def dominant(W: Any) -> Any:
    """Each game's heaviest genre. A row with no weight at all is labelled -1."""
    import numpy as np

    labels = np.asarray(W).argmax(axis=1)
    return np.where(np.asarray(W).sum(axis=1) > 0, labels, -1)


def _subsample(n_rows: int, fraction: float, seed: int) -> Any:
    import numpy as np

    rng = np.random.default_rng(seed)
    size = max(2, round(fraction * n_rows))
    return np.sort(rng.choice(n_rows, size=size, replace=False))


def mean_pairwise_ari(fits: list[Fit]) -> float:
    """Mean ARI over every pair of fits, each pair read on the games they share."""
    import numpy as np
    from sklearn.metrics import adjusted_rand_score

    if len(fits) < 2:
        return float("nan")

    by_row = [dict(zip(f.rows.tolist(), f.labels.tolist())) for f in fits]
    scores = []
    for i in range(len(fits)):
        for j in range(i + 1, len(fits)):
            shared = np.intersect1d(fits[i].rows, fits[j].rows, assume_unique=True)
            if len(shared) < 2:
                continue
            left = [by_row[i][int(r)] for r in shared]
            right = [by_row[j][int(r)] for r in shared]
            scores.append(adjusted_rand_score(left, right))
    return float(np.mean(scores)) if scores else float("nan")


def consensus_matrix(fits: list[Fit], sample: Any) -> Any:
    """Share of fits in which two games fall in the same dominant genre."""
    import numpy as np

    size = len(sample)
    agree = np.zeros((size, size), dtype="float64")
    seen = np.zeros((size, size), dtype="float64")
    position = {int(r): i for i, r in enumerate(sample)}

    for fit in fits:
        mine = np.array([position.get(int(r), -1) for r in fit.rows])
        keep = mine >= 0
        if keep.sum() < 2:
            continue
        index = mine[keep]
        labels = np.asarray(fit.labels)[keep]
        same = (labels[:, None] == labels[None, :]).astype("float64")
        agree[np.ix_(index, index)] += same
        seen[np.ix_(index, index)] += 1.0

    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(seen > 0, agree / np.maximum(seen, 1e-12), 0.0)
    np.fill_diagonal(out, 1.0)
    return out


def cophenetic_correlation(consensus: Any) -> float:
    """How faithfully a tree represents the consensus: 1 means a clean split."""
    import numpy as np
    from scipy.cluster.hierarchy import cophenet, linkage
    from scipy.spatial.distance import squareform

    size = consensus.shape[0]
    if size < 3:
        return float("nan")
    distance = 1.0 - consensus
    np.fill_diagonal(distance, 0.0)
    distance = np.clip((distance + distance.T) / 2.0, 0.0, 1.0)
    condensed = squareform(distance, checks=False)
    if not np.isfinite(condensed).all() or condensed.std() == 0:
        # Every pair agrees (or none does): the tree is exact by construction.
        return 1.0
    tree = linkage(condensed, method="average")
    correlation, _ = cophenet(tree, condensed)
    return float(correlation)


def npmi_coherence(presence: Any, H: Any, top: int = TOP_TAGS_FOR_COHERENCE) -> float:
    """Mean NPMI of the top tags of each genre, read on tag co-occurrence.

    NPMI is +1 when two tags always appear together, 0 when they are
    independent, -1 when they never do. A genre whose top tags never co-occur is
    a genre no game actually has.
    """
    import numpy as np

    presence = presence.tocsc() if hasattr(presence, "tocsc") else presence
    n_games = presence.shape[0]
    if n_games == 0:
        return float("nan")

    counts = np.asarray(presence.sum(axis=0)).ravel()
    scores = []
    for row in np.asarray(H):
        tags = np.argsort(row)[::-1][:top]
        tags = [t for t in tags if counts[t] > 0]
        for a in range(len(tags)):
            for b in range(a + 1, len(tags)):
                i, j = int(tags[a]), int(tags[b])
                both = int(presence[:, i].multiply(presence[:, j]).sum())
                p_i, p_j = counts[i] / n_games, counts[j] / n_games
                if both == 0:
                    scores.append(-1.0)
                    continue
                p_ij = both / n_games
                scores.append(float(np.log(p_ij / (p_i * p_j)) / -np.log(p_ij)))
    return float(np.mean(scores)) if scores else float("nan")


def _check_grid_fits(
    ks: list[int], shape: tuple[int, int], n_bootstrap: int, fraction: float
) -> None:
    """A genre cannot be wider than the data: nndsvda needs k <= min(rows, tags).

    The subsample fits see fewer rows than the full one, so they set the real
    ceiling as soon as bootstrapping is on.
    """
    rows, tags = int(shape[0]), int(shape[1])
    ceiling = min(rows, tags)
    if n_bootstrap:
        ceiling = min(ceiling, max(2, round(fraction * rows)), tags)
    too_big = [k for k in ks if k > ceiling]
    if too_big:
        raise GenresError(
            f"k={', '.join(str(k) for k in too_big)} is larger than this matrix allows: "
            f"it is {rows} games x {tags} tags"
            + (
                f", and a subsample holds {max(2, round(fraction * rows))} games"
                if n_bootstrap
                else ""
            )
            + f", so k cannot exceed {ceiling}."
        )


def fit_grid(
    matrix: Any,
    *,
    ks: list[int],
    seeds: int,
    params: NmfParams,
    n_bootstrap: int = 5,
    bootstrap_fraction: float = 0.8,
    output_dir: Path | None = None,
    consensus_sample: int = CONSENSUS_SAMPLE,
) -> list[KReport]:
    """Fit every (k, seed), fit the subsamples, and score each k.

    Deterministic: the seeds are 0..seeds-1 and each subsample is drawn from a
    fixed generator, so two runs produce the same files and the same numbers.
    """
    import numpy as np

    presence = matrix.copy()
    presence.data = np.ones_like(presence.data)

    fits_dir = (output_dir / FITS_DIR) if output_dir else None
    if fits_dir:
        fits_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(CONSENSUS_SEED)
    n_rows = matrix.shape[0]
    _check_grid_fits(ks, matrix.shape, n_bootstrap, bootstrap_fraction)
    sample = (
        np.arange(n_rows)
        if n_rows <= consensus_sample
        else np.sort(rng.choice(n_rows, size=consensus_sample, replace=False))
    )

    if seeds > 1 and params.init.startswith("nndsvd"):
        log.warning(
            "init=%s starts from a deterministic decomposition, so the %d seeds move "
            "little or not at all and stability mostly reflects the %d subsample(s). "
            "Set nmf.init to 'random' in config/genres.yml for genuine seed-to-seed "
            "variation; SPEC_GENRES section 5 asks for nndsvda, so this is the team's call.",
            params.init, seeds, n_bootstrap,
        )

    reports = []
    for k in ks:
        started = time.monotonic()
        seed_fits: list[Fit] = []
        for seed in range(seeds):
            W, H, rel_error, n_iter = fit_once(matrix, k, seed, params)
            path = None
            if fits_dir:
                path = fits_dir / f"k{k}_s{seed}.npz"
                np.savez_compressed(
                    path, W=W.astype("float32"), H=H.astype("float32"),
                    k=k, seed=seed, rel_error=rel_error, n_iter=n_iter,
                )
            seed_fits.append(
                Fit(k=k, seed=seed, kind="seed", labels=dominant(W),
                    rows=np.arange(n_rows), rel_error=rel_error, n_iter=n_iter,
                    path=path, components=H)
            )
            log.info(
                "k=%d seed=%d: relative error %.4f after %d iterations", k, seed, rel_error, n_iter
            )

        boot_fits: list[Fit] = []
        for index in range(n_bootstrap):
            rows = _subsample(n_rows, bootstrap_fraction, BOOTSTRAP_SEED + 1000 * k + index)
            W, _, rel_error, n_iter = fit_once(matrix[rows], k, index, params)
            fit = Fit(k=k, seed=index, kind="bootstrap", labels=dominant(W), rows=rows,
                      rel_error=rel_error, n_iter=n_iter)
            if fits_dir:
                fit.path = fits_dir / f"k{k}_b{index}.npz"
                np.savez_compressed(fit.path, labels=fit.labels, rows=rows, k=k, seed=index)
            boot_fits.append(fit)

        errors = np.array([f.rel_error for f in seed_fits], dtype="float64")
        best = min(seed_fits, key=lambda f: f.rel_error)
        every = seed_fits + boot_fits
        report = KReport(
            k=k,
            rel_error_mean=float(errors.mean()),
            rel_error_std=float(errors.std(ddof=0)),
            stability=mean_pairwise_ari(every),
            cophenetic=cophenetic_correlation(consensus_matrix(every, sample)),
            coherence=npmi_coherence(presence, best.components),
            n_seed_fits=len(seed_fits),
            n_bootstrap_fits=len(boot_fits),
            seconds=time.monotonic() - started,
        )
        log.info(
            "k=%d: error %.4f±%.4f, stability %.3f, cophenetic %.3f, coherence %.3f (%.1fs)",
            k, report.rel_error_mean, report.rel_error_std, report.stability,
            report.cophenetic, report.coherence, report.seconds,
        )
        reports.append(report)
    return reports


def write_k_selection(reports: list[KReport], output_dir: Path) -> Path:
    """Write ``k_selection.csv``: one row per k, for the team to read."""
    import pandas as pd

    output_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        [
            {
                "k": r.k,
                "rel_error_mean": round(r.rel_error_mean, 6),
                "rel_error_std": round(r.rel_error_std, 6),
                "stability": round(r.stability, 6),
                "cophenetic": round(r.cophenetic, 6),
                "coherence": round(r.coherence, 6),
                "n_seed_fits": r.n_seed_fits,
                "n_bootstrap_fits": r.n_bootstrap_fits,
                "seconds": round(r.seconds, 2),
            }
            for r in reports
        ]
    ).sort_values("k")
    path = output_dir / K_SELECTION_FILE
    frame.to_csv(path, index=False)
    log.info("written to %s: %s", display(output_dir), K_SELECTION_FILE)
    return path


def best_k(reports: list[KReport]) -> int | None:
    """The most stable k — a suggestion for the team, never a decision."""
    usable = [r for r in reports if r.stability == r.stability]  # drops NaN
    if not usable:
        return None
    return max(usable, key=lambda r: (r.stability, -r.rel_error_mean)).k
