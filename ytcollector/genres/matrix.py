"""The game x tag matrix, weighted in TF-IDF — SPEC_GENRES section 4.

The order of the steps is the method, not an implementation detail:

1. TF is the share of a game's tag votes, computed over **all** its tags, before
   any family is dropped. Dropping first would re-weight what is left.
2. Shares below ``min_tag_share`` go to zero: a handful of votes on a game with
   thousands says nothing.
3. Only the families named in ``used_for_factorization`` stay as columns.
4. IDF is smoothed and computed over the games kept, so it follows the filter.
5. Each row is normalised to length 1, and rows left empty are dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytcollector.genres.load import GameTags, GenresConfig, GenresError, TagFamilies, display

log = logging.getLogger(__name__)

MATRIX_FILE = "matrix.npz"
ROWS_FILE = "rows.parquet"
COLS_FILE = "cols.parquet"


@dataclass
class BuildResult:
    """The matrix and the two label tables that make it readable."""

    matrix: Any  # scipy.sparse.csr_matrix
    rows: Any  # appid, name, release_date, total_votes
    cols: Any  # tag, family, df, idf
    games_in: int
    empty_rows: int
    tags_total: int

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.matrix.shape[0]), int(self.matrix.shape[1]))

    @property
    def nnz(self) -> int:
        return int(self.matrix.nnz)


def build_matrix(
    config: GenresConfig, families: TagFamilies, game_tags: GameTags
) -> BuildResult:
    """Weight, filter and normalise. Nothing is read or written here."""
    import numpy as np
    import pandas as pd
    from scipy import sparse

    pairs = game_tags.pairs
    games = game_tags.games
    if games.empty or pairs.empty:
        raise GenresError("no game passed the vote filter; lower min_total_votes")

    families.check_covers(sorted(set(pairs["tag"].astype(str))))

    # 1. TF over every tag of the game.
    pairs = pairs.merge(games[["appid", "total_votes"]], on="appid", how="inner")
    pairs = pairs.assign(tf=pairs["nb"] / pairs["total_votes"])

    # 2. then the threshold, 3. then the families.
    kept = pairs[pairs["tf"] >= config.min_tag_share].copy()
    kept["family"] = [families.family_of(str(tag)) for tag in kept["tag"]]
    used = set(families.used_for_factorization)
    kept = kept[kept["family"].isin(used)]
    if kept.empty:
        raise GenresError(
            "no (game, tag) pair survived min_tag_share and the family filter; "
            f"used_for_factorization is {sorted(used)}"
        )

    # Columns in a stable order, so two runs give the same matrix.
    tags = sorted(set(kept["tag"].astype(str)))
    column_of = {tag: j for j, tag in enumerate(tags)}
    row_of = {int(appid): i for i, appid in enumerate(games["appid"])}

    rows_index = np.fromiter(
        (row_of[int(a)] for a in kept["appid"]), dtype="int64", count=len(kept)
    )
    cols_index = np.fromiter(
        (column_of[str(t)] for t in kept["tag"]), dtype="int64", count=len(kept)
    )
    tf = kept["tf"].to_numpy(dtype="float64")

    # 4. Smoothed IDF over the games kept, df counted after the threshold.
    n_games = len(games)
    df = np.bincount(cols_index, minlength=len(tags)).astype("int64")
    idf = np.log((1.0 + n_games) / (1.0 + df)) + 1.0

    values = tf * idf[cols_index]
    matrix = sparse.csr_matrix(
        (values, (rows_index, cols_index)), shape=(n_games, len(tags))
    )

    # 5. L2 per row, and out go the rows that hold nothing.
    norms = np.sqrt(matrix.multiply(matrix).sum(axis=1)).A.ravel()
    alive = norms > 0
    scale = sparse.diags(np.where(alive, 1.0 / np.where(alive, norms, 1.0), 0.0))
    matrix = (scale @ matrix).tocsr()
    matrix.eliminate_zeros()

    empty = int((~alive).sum())
    if empty:
        matrix = matrix[alive]
    kept_games = games[alive].reset_index(drop=True)

    cols = pd.DataFrame(
        {
            "tag": pd.Series(tags, dtype="string"),
            "family": pd.Series([families.family_of(t) for t in tags], dtype="string"),
            "df": df,
            "idf": idf,
        }
    )

    log.info(
        "matrix: %s games x %s tags, %s non-zero (%.2f%% dense, %.1f tags per game), "
        "%s empty rows dropped",
        f"{matrix.shape[0]:,}", f"{matrix.shape[1]:,}", f"{matrix.nnz:,}",
        100.0 * matrix.nnz / max(1, matrix.shape[0] * matrix.shape[1]),
        matrix.nnz / max(1, matrix.shape[0]), f"{empty:,}",
    )
    return BuildResult(
        matrix=matrix,
        rows=kept_games,
        cols=cols,
        games_in=n_games,
        empty_rows=empty,
        tags_total=int(pairs["tag"].nunique()),
    )


def write_matrix(result: BuildResult, output_dir: Path) -> dict[str, Path]:
    """Write the matrix and its labels. Derived from client data: never versioned."""
    from scipy import sparse

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "matrix": output_dir / MATRIX_FILE,
        "rows": output_dir / ROWS_FILE,
        "cols": output_dir / COLS_FILE,
    }
    sparse.save_npz(paths["matrix"], result.matrix)
    result.rows.to_parquet(paths["rows"], index=False)
    result.cols.to_parquet(paths["cols"], index=False)
    log.info(
        "written to %s: %s, %s, %s",
        display(output_dir), MATRIX_FILE, ROWS_FILE, COLS_FILE,
    )
    return paths


def load_matrix(output_dir: Path) -> BuildResult:
    """Read back what ``write_matrix`` wrote, for the milestones that follow."""
    import pandas as pd
    from scipy import sparse

    matrix_path = output_dir / MATRIX_FILE
    if not matrix_path.is_file():
        raise GenresError(
            f"{display(matrix_path)} does not exist. Run `ytc genres build-matrix` first."
        )
    matrix = sparse.load_npz(matrix_path).tocsr()
    rows = pd.read_parquet(output_dir / ROWS_FILE)
    cols = pd.read_parquet(output_dir / COLS_FILE)
    if len(rows) != matrix.shape[0] or len(cols) != matrix.shape[1]:
        raise GenresError(
            f"{display(output_dir)} is inconsistent: matrix is {matrix.shape} but "
            f"there are {len(rows)} rows and {len(cols)} columns. Rebuild it."
        )
    return BuildResult(
        matrix=matrix,
        rows=rows,
        cols=cols,
        games_in=len(rows),
        empty_rows=0,
        tags_total=len(cols),
    )
