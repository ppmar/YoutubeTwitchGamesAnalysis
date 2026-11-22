"""Genres, membership and hierarchy for one k — SPEC_GENRES section 6.

A factorisation is not yet a set of genres. Three things turn it into one:

* ``H`` is rescaled so each genre's heaviest tag weighs 1, and ``W`` is
  compensated, which makes two genres comparable without changing ``W x H``;
* a game's membership is a share of its own weight, so the shares sum to 1 and a
  game can belong to several genres;
* the provisional name of a genre is simply its three heaviest tags — a label to
  talk with, not a claim, until the team and PulluP have seen it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytcollector.genres.load import GenresConfig, GenresError, display
from ytcollector.genres.nmf import FITS_DIR

log = logging.getLogger(__name__)

GENRE_DEFINITION_FILE = "genre_definition.parquet"
GAME_GENRE_FILE = "game_genre.parquet"
GENRE_TREE_FILE = "genre_tree.csv"
MIN_MEMBERSHIP = 0.01
TOP_TAGS = 10
TOP_GAMES = 10
LABEL_TAGS = 3


@dataclass
class GenreModel:
    """One k, read back from its best fit and turned into genres."""

    k: int
    seed: int
    rel_error: float
    W: Any  # genre weight per game, after the rescaling
    H: Any  # tag weight per genre, each row peaking at 1
    shares: Any  # W normalised row-wise: the memberships
    rows: Any  # the matrix's game table
    cols: Any  # the matrix's tag table
    games_without_weight: int
    matrix: Any = None  # the TF-IDF matrix itself, for the checks that need it

    def tag_rows(self, tag: str) -> Any:
        """The games carrying a tag at all — the plain criterion, not a genre."""
        import numpy as np

        columns = [str(t) for t in self.cols["tag"]]
        if self.matrix is None or tag not in columns:
            return np.array([], dtype="int64")
        column = self.matrix[:, columns.index(tag)]
        return np.unique(column.tocoo().row) if hasattr(column, "tocoo") else np.nonzero(column)[0]

    @property
    def labels(self) -> list[str]:
        return [label_of(self.H[j], self.cols) for j in range(self.k)]


def available_fits(output_dir: Path, k: int) -> list[Path]:
    return sorted((output_dir / FITS_DIR).glob(f"k{k}_s*.npz"))


def available_ks(output_dir: Path) -> list[int]:
    """Every k the grid actually fitted, read from the file names."""
    fits = output_dir / FITS_DIR
    if not fits.is_dir():
        return []
    found = set()
    for path in fits.glob("k*_s*.npz"):
        match = re.match(r"k(\d+)_s\d+\.npz$", path.name)
        if match:
            found.add(int(match.group(1)))
    return sorted(found)


def normalise(W: Any, H: Any) -> tuple[Any, Any]:
    """Scale each row of ``H`` to peak at 1 and compensate ``W``.

    ``W x H`` is unchanged; what changes is that the tag weights of two genres
    can now be read side by side.
    """
    import numpy as np

    peaks = np.asarray(H).max(axis=1)
    safe = np.where(peaks > 0, peaks, 1.0)
    return np.asarray(W) * safe, np.asarray(H) / safe[:, None]


def memberships(W: Any) -> tuple[Any, Any]:
    """Shares of a game's own weight. Returns the shares and the empty-row mask."""
    import numpy as np

    totals = np.asarray(W).sum(axis=1)
    empty = totals <= 0
    shares = np.zeros_like(np.asarray(W), dtype="float64")
    np.divide(np.asarray(W), totals[:, None], out=shares, where=~empty[:, None])
    return shares, empty


def genre_name(genre_id: int) -> str:
    """What a genre is called before anyone has named it: "Genre 1", "Genre 2".

    The tags stay beside it in ``label``. A number says plainly that nothing has
    been decided, where a name made of tags can be mistaken for a finding.
    """
    return f"Genre {genre_id + 1}"


def label_of(tag_weights: Any, cols: Any, count: int = LABEL_TAGS) -> str:
    """A genre's provisional name: its heaviest tags, joined by ' + '."""
    import numpy as np

    order = np.argsort(np.asarray(tag_weights))[::-1][:count]
    return " + ".join(str(cols["tag"].iloc[int(j)]) for j in order)


def load_model(output_dir: Path, k: int, seed: int | None = None) -> GenreModel:
    """Read the fit with the lowest error for this k, and derive the genres."""
    import numpy as np

    from ytcollector.genres.matrix import load_matrix

    fits = available_fits(output_dir, k)
    if not fits:
        known = available_ks(output_dir)
        raise GenresError(
            f"no fit for k={k} in {display(output_dir / FITS_DIR)}"
            + (f"; the grid holds {', '.join(str(x) for x in known)}. " if known else ". ")
            + "Run `ytc genres fit` first."
        )

    best: tuple[float, int, Any, Any] | None = None
    for path in fits:
        with np.load(path) as blob:
            if seed is not None and int(blob["seed"]) != seed:
                continue
            error = float(blob["rel_error"])
            if best is None or error < best[0]:
                best = (error, int(blob["seed"]), blob["W"], blob["H"])
    if best is None:
        raise GenresError(f"no fit for k={k} with seed {seed}")

    rel_error, chosen, W, H = best
    built = load_matrix(output_dir)
    if W.shape[0] != len(built.rows):
        raise GenresError(
            f"the fit for k={k} has {W.shape[0]} games but the matrix has "
            f"{len(built.rows)}. Rebuild the matrix and refit."
        )

    W, H = normalise(W.astype("float64"), H.astype("float64"))
    shares, empty = memberships(W)
    if empty.any():
        log.info("k=%d: %s game(s) carry no genre weight and are left out", k, f"{int(empty.sum()):,}")

    return GenreModel(
        k=k, seed=chosen, rel_error=rel_error, W=W, H=H, shares=shares,
        rows=built.rows.reset_index(drop=True), cols=built.cols.reset_index(drop=True),
        games_without_weight=int(empty.sum()), matrix=built.matrix,
    )


def genre_definition(model: GenreModel, top_tags: int = TOP_TAGS, top_games: int = TOP_GAMES) -> Any:
    """One row per genre: its label, its heaviest tags and its heaviest games."""
    import numpy as np
    import pandas as pd

    dominant = np.asarray(model.shares).argmax(axis=1)
    carries = np.asarray(model.shares).sum(axis=1) > 0
    rows = []
    for j in range(model.k):
        order = np.argsort(model.H[j])[::-1][:top_tags]
        tags = [
            {"tag": str(model.cols["tag"].iloc[int(t)]), "weight": round(float(model.H[j][t]), 4)}
            for t in order
            if model.H[j][t] > 0
        ]
        heaviest = np.argsort(model.W[:, j])[::-1][:top_games]
        games = [
            {
                "appid": int(model.rows["appid"].iloc[int(i)]),
                "name": str(model.rows["name"].iloc[int(i)]),
                "share": round(float(model.shares[i, j]), 4),
            }
            for i in heaviest
            if model.W[i, j] > 0
        ]
        rows.append(
            {
                "genre_id": j,
                "name": genre_name(j),
                "label": label_of(model.H[j], model.cols),
                "top_tags": tags,
                "top_games": games,
                "n_dominant": int(((dominant == j) & carries).sum()),
            }
        )
    return pd.DataFrame(rows)


def game_genre(model: GenreModel, min_share: float = MIN_MEMBERSHIP) -> Any:
    """One row per (game, genre) above the floor. The shares of a game sum to 1."""
    import numpy as np
    import pandas as pd

    rows, cols = np.nonzero(np.asarray(model.shares) > min_share)
    return pd.DataFrame(
        {
            "appid": model.rows["appid"].to_numpy()[rows],
            "genre_id": cols.astype("int32"),
            "share": np.round(np.asarray(model.shares)[rows, cols], 6),
        }
    ).sort_values(["appid", "share"], ascending=[True, False]).reset_index(drop=True)


def threshold_column(tau: float) -> str:
    """``0.2`` becomes ``n_share_ge_0p2``: a column name without a dot in it."""
    return "n_share_ge_" + f"{tau:g}".replace(".", "p").replace("-", "m")


def threshold_counts(model: GenreModel, thresholds: list[float]) -> Any:
    """How many games each genre holds, once membership has to clear a bar."""
    import numpy as np
    import pandas as pd

    shares = np.asarray(model.shares)
    return pd.DataFrame(
        {
            "genre_id": list(range(model.k)),
            **{
                threshold_column(tau): (shares >= tau).sum(axis=0).astype("int64")
                for tau in thresholds
            },
        }
    )


def hierarchy(output_dir: Path, ks: list[int] | None = None) -> Any:
    """Link every genre to its nearest genre at the previous k of the grid.

    Cosine similarity between rows of ``H``: the tag profile is what a genre is,
    so two genres are close when they weigh the same tags the same way.
    """
    import numpy as np
    import pandas as pd

    grid = ks or available_ks(output_dir)
    rows = []
    previous: tuple[int, Any] | None = None
    for k in grid:
        model_H = _best_H(output_dir, k)
        if model_H is None:
            continue
        unit = model_H / np.maximum(np.linalg.norm(model_H, axis=1, keepdims=True), 1e-12)
        if previous is None:
            rows.extend(
                {"k": k, "genre_id": j, "parent_k": None, "parent_genre_id": None,
                 "similarity": None}
                for j in range(unit.shape[0])
            )
        else:
            parent_k, parent_unit = previous
            similarity = unit @ parent_unit.T
            best = similarity.argmax(axis=1)
            rows.extend(
                {
                    "k": k,
                    "genre_id": j,
                    "parent_k": parent_k,
                    "parent_genre_id": int(best[j]),
                    "similarity": round(float(similarity[j, best[j]]), 4),
                }
                for j in range(unit.shape[0])
            )
        previous = (k, unit)
    return pd.DataFrame(rows)


def _best_H(output_dir: Path, k: int) -> Any:
    import numpy as np

    best: tuple[float, Any] | None = None
    for path in available_fits(output_dir, k):
        with np.load(path) as blob:
            error = float(blob["rel_error"])
            if best is None or error < best[0]:
                best = (error, blob["H"].astype("float64"))
    if best is None:
        return None
    _, H = normalise(np.zeros((1, k)), best[1])
    return H


def write_tables(model: GenreModel, output_dir: Path, thresholds: list[float]) -> dict[str, Path]:
    """Write the two tables the rest of the study joins on."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "definition": output_dir / GENRE_DEFINITION_FILE,
        "membership": output_dir / GAME_GENRE_FILE,
    }
    definition = genre_definition(model)
    counts = threshold_counts(model, thresholds)
    definition.merge(counts, on="genre_id").to_parquet(paths["definition"], index=False)
    game_genre(model).to_parquet(paths["membership"], index=False)
    log.info(
        "written to %s: %s, %s", display(output_dir), GENRE_DEFINITION_FILE, GAME_GENRE_FILE
    )
    return paths


def write_tree(output_dir: Path) -> Path | None:
    tree = hierarchy(output_dir)
    if tree.empty:
        return None
    path = output_dir / GENRE_TREE_FILE
    tree.to_csv(path, index=False)
    log.info("written to %s: %s", display(output_dir), GENRE_TREE_FILE)
    return path


def _fr(value: int) -> str:
    """Group thousands the French way, as the rest of the project does."""
    return f"{value:,}".replace(",", " ")


def report_markdown(model: GenreModel, thresholds: list[float], tree: Any | None = None) -> str:
    """The Markdown the team reads before choosing k — French, like the README."""
    import numpy as np

    definition = genre_definition(model)
    counts = threshold_counts(model, thresholds)
    merged = definition.merge(counts, on="genre_id")
    membership = game_genre(model)
    per_game = membership.groupby("appid").size()

    lines = [
        f"# Genres à k = {model.k}",
        "",
        (f"Graine retenue : {model.seed} (erreur relative {model.rel_error:.4f}). "
        f"{_fr(len(model.rows))} jeux, {_fr(len(model.cols))} tags."),
        "",
        ("Les genres sont **numérotés, pas nommés** : « Genre 1 », « Genre 2 »… et la "
        "colonne suivante donne leurs trois tags les plus lourds. Nommer un genre est une "
        "décision de l'équipe et de PulluP. Un k peu stable ne doit pas être interprété — "
        "lire `k_selection.csv` avant ce rapport."),
        "",
        "## Vue d'ensemble",
        "",
        f"- Jeux sans aucun poids de genre : {_fr(model.games_without_weight)}",
        f"- Appartenances au-dessus de {MIN_MEMBERSHIP:g} : {_fr(len(membership))}",
        f"- Genres par jeu (médiane) : {per_game.median():.0f}" if len(per_game) else "",
        "",
        "## Genres",
        "",
    ]

    header = ["genre", "tags dominants", "jeux dominants"]
    header += [f"part ≥ {tau:g}" for tau in thresholds]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for _, row in merged.iterrows():
        cells = [str(row["name"]), str(row["label"]), _fr(int(row["n_dominant"]))]
        cells += [_fr(int(row[threshold_column(tau)])) for tau in thresholds]
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## Détail par genre", ""]
    for _, row in merged.iterrows():
        lines.append(f"### {row['name']} — {row['label']}")
        lines.append("")
        tags = ", ".join(f"{t['tag']} ({t['weight']:.2f})" for t in row["top_tags"])
        lines.append(f"**Tags** : {tags}")
        lines.append("")
        games = ", ".join(f"{g['name']} ({g['share']:.0%})" for g in row["top_games"])
        lines.append(f"**Jeux les plus marqués** : {games}")
        lines.append("")

    if tree is not None and not tree.empty:
        here = tree[tree["k"] == model.k]
        if not here.empty and here["parent_k"].notna().any():
            lines += ["## Rattachement au k précédent", "", "| genre | parent | similarité |",
                      "|---|---|---|"]
            for row in here.itertuples(index=False):
                if row.parent_genre_id is None or (
                    isinstance(row.parent_genre_id, float) and np.isnan(row.parent_genre_id)
                ):
                    continue
                lines.append(
                    f"| {genre_name(int(row.genre_id))} | k={int(row.parent_k)} genre "
                    f"{int(row.parent_genre_id)} | {row.similarity:.2f} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        ("Dérivé de données client sous accord de confidentialité : ce rapport reste "
        "dans `data/processed/genres/` et n'est pas versionné."),
        "",
    ]
    return "\n".join(line for line in lines if line is not None) + "\n"


def write_report(model: GenreModel, output_dir: Path, thresholds: list[float]) -> Path:
    tree = hierarchy(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"report_k{model.k}.md"
    path.write_text(report_markdown(model, thresholds, tree), encoding="utf-8")
    log.info("written to %s: %s", display(output_dir), path.name)
    return path


def final_k(config: GenresConfig) -> int:
    """``k_final`` from the configuration, or a refusal to guess it."""
    value = (config.selection or {}).get("k_final")
    if value in (None, "", "null"):
        raise GenresError(
            "k_final is null in config/genres.yml. `select` writes the tables the "
            "study joins on, so the value has to be a decision: read k_selection.csv "
            "and `ytc genres report --k K` with the team and PulluP first "
            "(SPEC_GENRES section 12)."
        )
    return int(value)


def thresholds_of(config: GenresConfig) -> list[float]:
    raw = (config.selection or {}).get("membership_thresholds") or [0.2, 0.3, 0.5]
    return [float(t) for t in raw]
