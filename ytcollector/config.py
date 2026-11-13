"""Configuration: package paths, settings file, study files, environment variables.

API limits live in ``config/settings.yml`` because they are documented defaults that
Google can change, not constants (SPEC section 2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent


def _find_dir(relative: str, override: str) -> Path:
    """Locate a runtime directory that ships beside the code, not inside it.

    In a source checkout these sit next to the package; in the container the
    package is installed into site-packages and the directories are copied to the
    working directory instead. Resolving only against the package would point at
    site-packages and fail at runtime, so the working directory is tried first.
    """
    from_env = os.environ.get(override)
    if from_env:
        return Path(from_env)
    for base in (Path.cwd(), PACKAGE_ROOT.parent):
        candidate = base / relative
        if candidate.is_dir():
            return candidate
    return PACKAGE_ROOT.parent / relative


PROJECT_ROOT = PACKAGE_ROOT.parent
CONFIG_DIR = _find_dir("config", "YTC_CONFIG_DIR")
STUDIES_DIR = CONFIG_DIR / "studies"
DATA_DIR = _find_dir("data/external", "YTC_DATA_DIR")

TWITCH_GAME_CSV = DATA_DIR / "Twitch_game_data.csv"
TWITCH_GLOBAL_CSV = DATA_DIR / "Twitch_global_data.csv"
TWITCH_CSV_ENCODING = "latin-1"

VALID_ROLES = {"precursor", "leader", "follower", "mode"}


class ConfigError(RuntimeError):
    """Raised when a configuration file is missing or malformed."""


@dataclass(frozen=True)
class GameConfig:
    """One game of a study, as declared in the study file."""

    game_id: str
    game_name: str
    twitch_names: list[str]
    yt_query: str
    match_terms: list[str]
    role: str
    # Optional: a game only needs exclusions when a neighbour shares its name.
    exclude_terms: list[str] = field(default_factory=list)
    enabled: bool = True
    release_date: str | None = None
    valid_from: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class StudyConfig:
    """A study: a named roster of games plus its collection window."""

    study: str
    title: str
    games: list[GameConfig]
    description: str = ""
    months_before_first: int = 12
    months_after_last: int = 6
    path: Path | None = None

    @property
    def enabled_games(self) -> list[GameConfig]:
        return [game for game in self.games if game.enabled]


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


@lru_cache(maxsize=1)
def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Load ``config/settings.yml``."""
    return read_yaml(path or CONFIG_DIR / "settings.yml")


def _game_from_mapping(raw: Any, index: int, source: Path) -> GameConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: games[{index}] must be a mapping")
    missing = [k for k in ("game_id", "game_name", "twitch_names") if not raw.get(k)]
    if missing:
        raise ConfigError(f"{source}: games[{index}] is missing {', '.join(missing)}")
    role = raw.get("role", "follower")
    if role not in VALID_ROLES:
        raise ConfigError(
            f"{source}: games[{index}] has role {role!r}, expected one of {sorted(VALID_ROLES)}"
        )
    name = str(raw["game_name"])
    return GameConfig(
        game_id=str(raw["game_id"]),
        game_name=name,
        twitch_names=[str(n) for n in raw["twitch_names"]],
        yt_query=str(raw.get("yt_query") or f'"{name}"'),
        match_terms=[str(t) for t in (raw.get("match_terms") or [name])],
        exclude_terms=[str(t) for t in (raw.get("exclude_terms") or [])],
        role=role,
        enabled=bool(raw.get("enabled", True)),
        release_date=raw.get("release_date"),
        valid_from=raw.get("valid_from"),
        notes=raw.get("notes"),
    )


def load_study(name: str, directory: Path | None = None) -> StudyConfig:
    """Load ``config/studies/<name>.yml`` and validate it."""
    path = (directory or STUDIES_DIR) / f"{name}.yml"
    data = read_yaml(path)
    raw_games = data.get("games")
    if not isinstance(raw_games, list) or not raw_games:
        raise ConfigError(f"{path}: 'games' must be a non-empty list")

    games = [_game_from_mapping(raw, i, path) for i, raw in enumerate(raw_games)]
    duplicates = sorted({g.game_id for g in games if [x.game_id for x in games].count(g.game_id) > 1})
    if duplicates:
        raise ConfigError(f"{path}: duplicate game_id: {', '.join(duplicates)}")

    window = data.get("window") or {}
    return StudyConfig(
        study=str(data.get("study", name)),
        title=str(data.get("title", name)),
        description=str(data.get("description", "")),
        games=games,
        months_before_first=int(window.get("months_before_first", 12)),
        months_after_last=int(window.get("months_after_last", 6)),
        path=path,
    )


def database_url() -> str:
    """Return ``DATABASE_URL``, which is the only supported way to reach the database."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ConfigError("DATABASE_URL is not set")
    return url


def youtube_api_key() -> str:
    """Return ``YOUTUBE_API_KEY``. The key must never be logged or persisted."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ConfigError("YOUTUBE_API_KEY is not set")
    return key
