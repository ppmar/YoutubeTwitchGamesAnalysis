"""A read-only view of the game x tag matrix, for the web page's matrix tab.

The matrix is derived from the client exports held under a confidentiality
agreement (SPEC_GENRES section 3), so this view is **off by default**: it serves
nothing unless ``YTC_GENRES_LOCAL`` is set in the environment, which only ever
happens on a local machine. Nothing here is reachable from a deployed service
that does not opt in, and nothing is ever written.

Reading is done with numpy alone, not scipy, so the Railway image keeps its
current dependency set: ``scipy.sparse.save_npz`` writes a plain npz whose keys
are ``format``, ``shape``, ``data``, ``indices`` and ``indptr``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path("data/processed/genres")
MAX_GAMES = 200
MAX_TAGS = 120


def is_enabled() -> bool:
    """Whether the operator opted in to serving client-derived data."""
    return os.environ.get("YTC_GENRES_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}


def genres_dir() -> Path:
    return Path(os.environ.get("YTC_GENRES_DIR", str(DEFAULT_DIR)))


@dataclass(frozen=True)
class Paths:
    matrix: Path
    rows: Path
    cols: Path

    @property
    def missing(self) -> list[str]:
        return [p.name for p in (self.matrix, self.rows, self.cols) if not p.exists()]


def paths() -> Paths:
    base = genres_dir()
    return Paths(base / "matrix.npz", base / "rows.parquet", base / "cols.parquet")


def _load_csr(path: Path) -> tuple[Any, Any, Any, tuple[int, int]]:
    """Load a CSR matrix written by ``scipy.sparse.save_npz``, with numpy only."""
    import numpy as np

    with np.load(path, allow_pickle=False) as blob:
        fmt = blob["format"] if "format" in blob.files else b"csr"
        fmt = bytes(fmt).decode() if not isinstance(fmt, str) else fmt
        if fmt.lower().strip("b'\" ") != "csr":
            raise ValueError(f"expected a CSR matrix, found {fmt!r}")
        shape = tuple(int(v) for v in blob["shape"])
        return blob["data"], blob["indices"], blob["indptr"], (shape[0], shape[1])


def _clamp(value: Any, default: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(high, n))


def read_submatrix(
    games: int = 60,
    tags: int = 40,
    tag_query: str = "",
    appid: int | None = None,
) -> dict[str, Any]:
    """A corner of the matrix small enough to draw: the heaviest games and tags.

    Returned as sparse triplets ``[row, col, value]``, which is what the heatmap
    needs and keeps the payload proportional to what is actually drawn.
    """
    if not is_enabled():
        return {"available": False, "reason": "disabled"}

    where = paths()
    if where.missing:
        return {"available": False, "reason": "missing", "missing": where.missing}

    import numpy as np
    import pandas as pd

    n_games = _clamp(games, 60, MAX_GAMES)
    n_tags = _clamp(tags, 40, MAX_TAGS)

    data, indices, indptr, shape = _load_csr(where.matrix)
    rows = pd.read_parquet(where.rows)
    cols = pd.read_parquet(where.cols)
    if len(rows) != shape[0] or len(cols) != shape[1]:
        return {
            "available": False,
            "reason": "mismatch",
            "detail": f"matrix {shape} against {len(rows)} rows and {len(cols)} columns",
        }

    # Which games to show: the heaviest by tag votes, plus the focused one.
    order = rows["total_votes"].to_numpy(dtype="float64", na_value=0.0).argsort()[::-1]
    picked = list(order[:n_games])
    if appid is not None:
        match = np.flatnonzero(rows["appid"].to_numpy() == appid)
        if len(match) and int(match[0]) not in picked:
            picked = [int(match[0])] + picked[: n_games - 1]
    picked_rows = np.array(picked, dtype="int64")

    # Column mass inside those games only, so the tags shown are the tags that
    # actually weigh on them rather than the most common tags overall.
    mass = np.zeros(shape[1], dtype="float64")
    for row in picked_rows:
        start, end = int(indptr[row]), int(indptr[row + 1])
        np.add.at(mass, indices[start:end], data[start:end])

    allowed = np.ones(shape[1], dtype=bool)
    query = tag_query.strip().lower()
    if query:
        allowed = cols["tag"].str.lower().str.contains(query, regex=False).to_numpy()
        if not allowed.any():
            return {
                "available": True,
                "games": [],
                "tags": [],
                "cells": [],
                "scale": 0.0,
                "query": tag_query,
                "source": _source(shape, data, rows, cols),
            }

    mass = np.where(allowed, mass, -1.0)
    picked_cols = mass.argsort()[::-1][:n_tags]
    picked_cols = np.array([c for c in picked_cols if mass[c] > 0], dtype="int64")

    col_position = {int(c): j for j, c in enumerate(picked_cols)}
    cells: list[list[float]] = []
    scale = 0.0
    for i, row in enumerate(picked_rows):
        start, end = int(indptr[row]), int(indptr[row + 1])
        for k in range(start, end):
            j = col_position.get(int(indices[k]))
            if j is None:
                continue
            value = float(data[k])
            scale = max(scale, value)
            cells.append([i, j, round(value, 4)])

    game_rows = rows.iloc[picked_rows]
    tag_rows = cols.iloc[picked_cols]
    return {
        "available": True,
        "games": [
            {
                "appid": int(r.appid),
                "name": (r.name if isinstance(r.name, str) else None) or f"appid {int(r.appid)}",
                "releaseDate": _text(getattr(r, "release_date", None)),
                "totalVotes": int(r.total_votes or 0),
            }
            for r in game_rows.itertuples(index=False)
        ],
        "tags": [
            {
                "tag": str(r.tag),
                "family": _text(getattr(r, "family", None)) or "other",
                "df": int(getattr(r, "df", 0) or 0),
                "idf": round(float(getattr(r, "idf", 0.0) or 0.0), 3),
            }
            for r in tag_rows.itertuples(index=False)
        ],
        "cells": cells,
        "scale": round(scale, 4),
        "query": tag_query,
        "source": _source(shape, data, rows, cols),
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    return str(value)


def _source(shape: tuple[int, int], data: Any, rows: Any, cols: Any) -> dict[str, Any]:
    """What the whole matrix looks like, so the page can say what it is showing."""
    nnz = len(data)
    families: dict[str, int] = {}
    if "family" in getattr(cols, "columns", []):
        families = {str(k): int(v) for k, v in cols["family"].value_counts().items()}
    return {
        "games": int(shape[0]),
        "tags": int(shape[1]),
        "nnz": nnz,
        "density": round(nnz / max(1, shape[0] * shape[1]), 6),
        "tagsPerGame": round(nnz / max(1, shape[0]), 2),
        "families": families,
        "votesMedian": int(rows["total_votes"].median()) if "total_votes" in rows else 0,
    }
