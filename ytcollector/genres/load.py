"""Reading the client exports, and the two configuration files around them.

SPEC_GENRES sections 2 and 4. Nothing here writes, and no path from the local
machine is ever printed back: a message names a file the way the configuration
names it, so a copied traceback carries nothing about where the data sits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ytcollector.config import CONFIG_DIR, PROJECT_ROOT, read_yaml

log = logging.getLogger(__name__)

TAGS_FILE = "steam_tags.csv"
REFERENCE_FILE = "steam_reference.csv"
OTHER_FAMILY = "other"
# `used_for_factorization: [all]` keeps every tag, which is what lets the
# pipeline run before the team has classified anything.
ALL_FAMILIES = "all"


class GenresError(RuntimeError):
    """Raised when the inputs or the configuration cannot carry the method."""


def display(path: Path) -> str:
    """How a path is named back to the operator: relative, or just its name.

    An absolute path says where this machine keeps client data, so it never
    reaches a log line or an error message.
    """
    for base in (Path.cwd(), PROJECT_ROOT):
        try:
            return str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            continue
    return path.name


@dataclass(frozen=True)
class Expected:
    """What the team's notebook counted, and how far off is acceptable."""

    tag_rows: int = 0
    tag_games: int = 0
    tags: int = 0
    reference_games: int = 0
    tolerance: float = 0.05


@dataclass(frozen=True)
class GenresConfig:
    client_dir: Path
    output_dir: Path
    min_total_votes: int = 100
    min_tag_share: float = 0.02
    expected: Expected = field(default_factory=Expected)
    nmf: dict[str, Any] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)

    @property
    def tags_csv(self) -> Path:
        return self.client_dir / TAGS_FILE

    @property
    def reference_csv(self) -> Path:
        return self.client_dir / REFERENCE_FILE


def load_config(path: Path | None = None) -> GenresConfig:
    """Load ``config/genres.yml``."""
    data = read_yaml(path or CONFIG_DIR / "genres.yml")
    matrix = data.get("matrix") or {}
    expected = data.get("expected") or {}
    return GenresConfig(
        client_dir=Path(str(data.get("client_dir") or "data/client")),
        output_dir=Path(str(data.get("output_dir") or "data/processed/genres")),
        min_total_votes=int(matrix.get("min_total_votes", 100)),
        min_tag_share=float(matrix.get("min_tag_share", 0.02)),
        expected=Expected(
            tag_rows=int(expected.get("tag_rows", 0)),
            tag_games=int(expected.get("tag_games", 0)),
            tags=int(expected.get("tags", 0)),
            reference_games=int(expected.get("reference_games", 0)),
            tolerance=float(expected.get("tolerance", 0.05)),
        ),
        nmf=dict(data.get("nmf") or {}),
        selection=dict(data.get("selection") or {}),
    )


@dataclass(frozen=True)
class TagFamilies:
    """``config/tag_families.yml``: one family per tag, and which ones factorise."""

    families: list[str]
    used_for_factorization: list[str]
    tags: dict[str, str]

    @property
    def uses_every_family(self) -> bool:
        return ALL_FAMILIES in self.used_for_factorization

    def family_of(self, tag: str) -> str:
        return self.tags.get(tag, OTHER_FAMILY)

    def keeps(self, tag: str) -> bool:
        """Whether a tag becomes a column of the matrix."""
        return self.uses_every_family or self.family_of(tag) in set(self.used_for_factorization)

    def check_covers(self, tags: list[str]) -> None:
        """Fail with the full list of tags the file does not classify."""
        missing = sorted({tag for tag in tags if tag not in self.tags})
        if missing:
            raise GenresError(
                f"{len(missing)} tag(s) are missing from config/tag_families.yml: "
                + ", ".join(missing)
                + ". Classify them, or run `ytc genres init-tag-families` again."
            )


def tag_families_path(path: Path | None = None) -> Path:
    return path or CONFIG_DIR / "tag_families.yml"


def load_tag_families(path: Path | None = None) -> TagFamilies:
    where = tag_families_path(path)
    if not where.is_file():
        raise GenresError(
            f"{display(where)} does not exist. Run `ytc genres init-tag-families` "
            "and classify the tags with the team (SPEC_GENRES section 12)."
        )
    data = read_yaml(where)
    families = [str(f) for f in (data.get("families") or [])]
    used = [str(f) for f in (data.get("used_for_factorization") or [])]
    raw_tags = data.get("tags") or {}
    if not isinstance(raw_tags, dict) or not raw_tags:
        raise GenresError(f"{display(where)}: 'tags' must be a non-empty mapping")
    tags = {str(tag): str(family) for tag, family in raw_tags.items()}

    unknown = sorted({f for f in tags.values() if f not in families})
    if unknown:
        raise GenresError(
            f"{display(where)}: tags are assigned to families that are not declared: "
            + ", ".join(unknown)
        )
    if not used:
        raise GenresError(f"{display(where)}: 'used_for_factorization' must name a family")
    unknown_used = [f for f in used if f not in families and f != ALL_FAMILIES]
    if unknown_used:
        raise GenresError(
            f"{display(where)}: used_for_factorization names undeclared families: "
            + ", ".join(unknown_used)
        )
    return TagFamilies(families=families, used_for_factorization=used, tags=tags)


def require_client_data(config: GenresConfig) -> None:
    """Fail early, and in a way the operator can act on."""
    if not config.client_dir.is_dir():
        raise GenresError(
            f"{display(config.client_dir)} does not exist. The two client exports "
            f"({TAGS_FILE}, {REFERENCE_FILE}) are not versioned: copy them there. "
            "They stay local — never commit them, the repository is public."
        )
    missing = [f for f in (TAGS_FILE, REFERENCE_FILE) if not (config.client_dir / f).is_file()]
    if missing:
        raise GenresError(
            f"{display(config.client_dir)} is missing {', '.join(missing)}."
        )


def _check_expected(name: str, actual: int, expected: int, tolerance: float) -> None:
    """Stop rather than factorise something that is not the agreed input."""
    if not expected:
        return
    gap = abs(actual - expected) / expected
    if gap > tolerance:
        raise GenresError(
            f"{name}: found {actual:,}, the exploration notebook found {expected:,} "
            f"({gap:.1%} apart, tolerance {tolerance:.0%}). Stopping: check the export "
            "with the team before going further, or pass --ignore-expected once the "
            "difference is understood."
        )
    log.info("%s: %s (notebook: %s)", name, f"{actual:,}", f"{expected:,}")


def read_tag_votes(config: GenresConfig, *, ignore_expected: bool = False) -> Any:
    """One row per (appid, tag) with the votes summed.

    Tags carry stray spaces in the export, duplicated pairs exist, and some rows
    have no appid or no tag at all; all three are dealt with here so the rest of
    the pipeline can assume a clean pair.
    """
    import pandas as pd

    require_client_data(config)
    frame = pd.read_csv(config.tags_csv, low_memory=False)
    missing = [c for c in ("nb", "tag", "appid") if c not in frame.columns]
    if missing:
        raise GenresError(
            f"{display(config.tags_csv)} is missing column(s): {', '.join(missing)}"
        )
    raw_rows = len(frame)

    frame["tag"] = frame["tag"].astype("string").str.strip()
    frame["appid"] = pd.to_numeric(frame["appid"], errors="coerce")
    frame["nb"] = pd.to_numeric(frame["nb"], errors="coerce")
    frame = frame.dropna(subset=["appid", "tag", "nb"])
    frame = frame[(frame["tag"] != "") & (frame["nb"] > 0)]
    frame["appid"] = frame["appid"].astype("int64")
    frame["nb"] = frame["nb"].astype("int64")

    dropped = raw_rows - len(frame)
    frame = (
        frame.groupby(["appid", "tag"], as_index=False, observed=True)["nb"]
        .sum()
        .astype({"tag": "string"})
    )

    games = int(frame["appid"].nunique())
    tags = int(frame["tag"].nunique())
    log.info(
        "steam_tags: %s rows (%s unusable), %s games, %s tags, %.1f tags per game",
        f"{raw_rows:,}", f"{dropped:,}", f"{games:,}", f"{tags:,}", len(frame) / max(1, games),
    )
    if not ignore_expected:
        want = config.expected
        _check_expected("tag rows", raw_rows, want.tag_rows, want.tolerance)
        _check_expected("games with tags", games, want.tag_games, want.tolerance)
        _check_expected("distinct tags", tags, want.tags, want.tolerance)
    return frame


def read_reference(config: GenresConfig, *, ignore_expected: bool = False) -> Any:
    """``appid``, ``name`` and ``release_date`` from the reference export.

    The file mixes types in a column, so it is read with ``low_memory=False``;
    the sales and wishlist columns are snapshots and are left alone here.
    """
    import pandas as pd

    require_client_data(config)
    frame = pd.read_csv(config.reference_csv, low_memory=False)
    if "appid" not in frame.columns:
        raise GenresError(f"{display(config.reference_csv)} is missing the appid column")

    frame["appid"] = pd.to_numeric(frame["appid"], errors="coerce")
    frame = frame.dropna(subset=["appid"])
    frame["appid"] = frame["appid"].astype("int64")
    frame = frame.drop_duplicates(subset=["appid"], keep="first")

    out = pd.DataFrame({"appid": frame["appid"].to_numpy()})
    out["name"] = (
        frame["name"].astype("string").str.strip().to_numpy()
        if "name" in frame.columns
        else pd.array([None] * len(frame), dtype="string")
    )
    out["release_date"] = (
        frame["releaseDate"].astype("string").str.strip().to_numpy()
        if "releaseDate" in frame.columns
        else pd.array([None] * len(frame), dtype="string")
    )

    log.info("steam_reference: %s games", f"{len(out):,}")
    if not ignore_expected:
        want = config.expected
        _check_expected("reference games", len(out), want.reference_games, want.tolerance)
    return out


@dataclass
class GameTags:
    """The (game, tag) votes of the games kept, and what the filter removed."""

    pairs: Any  # appid, tag, nb
    games: Any  # appid, name, release_date, total_votes
    games_before_filter: int
    games_kept: int

    @property
    def games_dropped(self) -> int:
        return self.games_before_filter - self.games_kept


def load_game_tags(config: GenresConfig, *, ignore_expected: bool = False) -> GameTags:
    """Votes joined to names, keeping only games with enough of them."""
    import pandas as pd

    votes = read_tag_votes(config, ignore_expected=ignore_expected)
    reference = read_reference(config, ignore_expected=ignore_expected)

    totals = votes.groupby("appid", as_index=False, observed=True)["nb"].sum()
    totals = totals.rename(columns={"nb": "total_votes"})
    before = len(totals)
    kept = totals[totals["total_votes"] >= config.min_total_votes]

    games = kept.merge(reference, on="appid", how="left")
    games = games[["appid", "name", "release_date", "total_votes"]].sort_values("appid")
    games = games.reset_index(drop=True)

    pairs = votes[votes["appid"].isin(pd.Index(games["appid"]))].reset_index(drop=True)

    log.info(
        "games: %s with at least %s tag votes, out of %s (%s dropped); "
        "%s without a name in the reference",
        f"{len(games):,}", f"{config.min_total_votes:,}", f"{before:,}",
        f"{before - len(games):,}", f"{int(games['name'].isna().sum()):,}",
    )
    return GameTags(
        pairs=pairs, games=games, games_before_filter=before, games_kept=len(games)
    )


def tag_game_counts(votes: Any) -> Any:
    """How many games carry each tag, most common first — the draft's order."""
    counts = votes.groupby("tag", as_index=False, observed=True)["appid"].nunique()
    counts = counts.rename(columns={"appid": "games"})
    return counts.sort_values(["games", "tag"], ascending=[False, True]).reset_index(drop=True)


DRAFT_HEADER = """\
# Steam tag families — docs/SPEC_GENRES.md section 4.
#
# Tag names are public, so this file is versioned. The classification is not a
# decision for the collector: every tag starts in `other` and the team moves it,
# then chooses which families the factorisation uses (SPEC_GENRES section 12).
#
# Tags are listed by how many games carry them, most common first.
# `ytc genres build-matrix` fails while a tag is missing from this file.
"""


def write_tag_families_draft(
    counts: Any,
    path: Path,
    *,
    families: list[str] | None = None,
    used_for_factorization: list[str] | None = None,
) -> int:
    """Write the first version of ``config/tag_families.yml``. Returns the tag count."""
    families = families or ["gameplay", "players", "visuals", "themes", "business", "other"]
    used = used_for_factorization or ["gameplay", "players"]

    lines = [DRAFT_HEADER, f"families: [{', '.join(families)}]"]
    lines.append(f"used_for_factorization: [{', '.join(used)}]")
    lines.append("")
    lines.append("tags:")
    for row in counts.itertuples(index=False):
        tag = str(row.tag)
        # A plain scalar would break on ':' or a leading quote, and a tag may
        # hold either; double quotes with escaping always round-trip.
        safe = tag.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  "{safe}": {OTHER_FAMILY}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(counts)
