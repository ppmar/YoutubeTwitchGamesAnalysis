"""Joining the battle royale study to the genres — SPEC_GENRES section 7.

Two commands sit on top of this module, and neither writes anything the team has
not decided: ``suggest-appids`` proposes matches in a CSV and touches neither the
study file nor the database, and ``check-study`` only reads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ytcollector.genres.load import GenresConfig, GenresError, display

log = logging.getLogger(__name__)

SUGGESTIONS_FILE = "appid_suggestions.csv"
BATTLE_ROYALE_TAG = "Battle Royale"
CANDIDATES_PER_GAME = 5
MIN_SCORE = 0.55
# Below this, a candidate is a coincidence of words far more often than a match.
CONFIDENT_SCORE = 0.9
_NOISE = re.compile(r"[^a-z0-9]+")


def simplify(name: str | None) -> str:
    """Lower-case, strip punctuation and the edition noise Steam names carry.

    The trademark marks go first: NFKC turns U+2122 into the letters "tm", which
    would otherwise glue itself to the name ("apex legendstm").
    """
    from ytcollector.textnorm import normalize_name

    text = (name or "")
    for mark in ("\u2122", "\u00ae", "\u00a9", "\u2117"):
        text = text.replace(mark, " ")
    text = normalize_name(text).casefold()
    for word in (
        "game of the year edition", "goty edition", "definitive edition",
        "complete edition", "deluxe edition", "standard edition", "remastered",
    ):
        text = text.replace(word, " ")
    return _NOISE.sub(" ", text).strip()


def score(left: str, right: str) -> float:
    """Similarity of two simplified names, in [0, 1].

    A prefix match counts for a lot here: Steam names carry subtitles the Twitch
    labels do not ("PUBG: BATTLEGROUNDS" against "PUBG").
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if right.startswith(left + " ") or left.startswith(right + " "):
        ratio = max(ratio, 0.9)
    left_words, right_words = set(left.split()), set(right.split())
    if left_words and right_words:
        overlap = len(left_words & right_words) / len(left_words | right_words)
        ratio = max(ratio, 0.75 * overlap + 0.25 * ratio)
    return round(float(ratio), 4)


@dataclass
class Suggestion:
    game_id: str
    candidate_appid: int
    candidate_name: str
    score: float
    matched_on: str


def suggest_appids(
    study: Any, reference: Any, *, per_game: int = CANDIDATES_PER_GAME, min_score: float = MIN_SCORE
) -> list[Suggestion]:
    """Propose Steam appids for the games of a study. Nothing is written here.

    Each game is compared on its own name and on every Twitch label it merges,
    because the two differ often enough to matter.
    """
    import numpy as np

    names = reference["name"].fillna("").astype(str).to_numpy()
    appids = reference["appid"].to_numpy()
    simplified = np.array([simplify(n) for n in names])

    # One pass to index word -> rows. Scanning 208k names per probe instead
    # turns a handful of games into minutes of work.
    index: dict[str, list[int]] = {}
    for i, candidate in enumerate(simplified):
        for word in set(candidate.split()):
            index.setdefault(word, []).append(i)

    out: list[Suggestion] = []
    for game in study.games:
        probes = {"game_name": simplify(game.game_name)}
        for label in game.twitch_names:
            probes[f"twitch:{label}"] = simplify(label)
        probes = {k: v for k, v in probes.items() if v}

        best: dict[int, Suggestion] = {}
        for origin, probe in probes.items():
            # Only names sharing a word are worth scoring.
            close: set[int] = set()
            for word in set(probe.split()):
                close.update(index.get(word, ()))
            for i in close:
                value = score(probe, simplified[i])
                if value < min_score:
                    continue
                appid = int(appids[i])
                if appid not in best or value > best[appid].score:
                    best[appid] = Suggestion(
                        game_id=game.game_id,
                        candidate_appid=appid,
                        candidate_name=str(names[i]),
                        score=value,
                        matched_on=origin,
                    )
        ranked = sorted(best.values(), key=lambda s: (-s.score, s.candidate_name))[:per_game]
        if not ranked:
            log.info("%s: no Steam candidate above %.2f", game.game_id, min_score)
        out.extend(ranked)
    return out


def write_suggestions(suggestions: list[Suggestion], output_dir: Path) -> Path:
    """Write the candidates for the team to pick from. The YAML stays untouched."""
    import pandas as pd

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / SUGGESTIONS_FILE
    pd.DataFrame(
        [
            {
                "game_id": s.game_id,
                "candidate_appid": s.candidate_appid,
                "candidate_name": s.candidate_name,
                "score": s.score,
                "matched_on": s.matched_on,
            }
            for s in suggestions
        ],
        columns=["game_id", "candidate_appid", "candidate_name", "score", "matched_on"],
    ).to_csv(path, index=False)
    log.info("written to %s: %s", display(output_dir), SUGGESTIONS_FILE)
    return path


@dataclass
class StudyCheck:
    """What ``check-study`` found, ready to be printed or asserted on."""

    study: str
    k: int
    with_appid: list[dict[str, Any]]
    without_appid: list[str]
    unknown_appid: list[str]
    leading_genre: int | None
    leading_genre_label: str | None
    leading_genre_count: int
    tag_games: int
    tag_and_study: int
    genre_outside_study: int
    study_missing_from_genre: list[str]


def check_study(model: Any, study: Any, *, tag: str = BATTLE_ROYALE_TAG, top: int = 2) -> StudyCheck:
    """Read the study through the genres, and against the plain tag criterion."""
    import numpy as np

    by_appid = {int(a): i for i, a in enumerate(model.rows["appid"])}
    labels = model.labels

    with_appid: list[dict[str, Any]] = []
    without: list[str] = []
    unknown: list[str] = []
    for game in study.games:
        if game.steam_appid is None:
            without.append(game.game_id)
            continue
        row = by_appid.get(int(game.steam_appid))
        if row is None:
            unknown.append(game.game_id)
            continue
        shares = np.asarray(model.shares)[row]
        order = np.argsort(shares)[::-1][:top]
        with_appid.append(
            {
                "game_id": game.game_id,
                "game_name": game.game_name,
                "appid": int(game.steam_appid),
                "genres": [
                    {"genre_id": int(j), "label": labels[int(j)], "share": round(float(shares[j]), 4)}
                    for j in order
                    if shares[j] > 0
                ],
            }
        )

    leading, leading_count = None, 0
    if with_appid:
        counts: dict[int, int] = {}
        for item in with_appid:
            if item["genres"]:
                first = item["genres"][0]["genre_id"]
                counts[first] = counts.get(first, 0) + 1
        if counts:
            leading = max(counts, key=lambda g: counts[g])
            leading_count = counts[leading]

    # The simple criterion the study has used so far: "carries the tag".
    tag_rows = model.tag_rows(tag)
    study_rows = {by_appid[int(g.steam_appid)] for g in study.games
                  if g.steam_appid is not None and int(g.steam_appid) in by_appid}

    genre_rows: set[int] = set()
    if leading is not None:
        dominant = np.asarray(model.shares).argmax(axis=1)
        carries = np.asarray(model.shares).sum(axis=1) > 0
        genre_rows = set(np.nonzero((dominant == leading) & carries)[0].tolist())

    return StudyCheck(
        study=study.study,
        k=model.k,
        with_appid=with_appid,
        without_appid=without,
        unknown_appid=unknown,
        leading_genre=leading,
        leading_genre_label=labels[leading] if leading is not None else None,
        leading_genre_count=leading_count,
        tag_games=len(tag_rows),
        tag_and_study=len(study_rows & set(tag_rows.tolist())),
        genre_outside_study=len(genre_rows - study_rows),
        study_missing_from_genre=sorted(
            item["game_id"]
            for item in with_appid
            if by_appid[item["appid"]] not in genre_rows
        ),
    )


def load_study_or_fail(name: str) -> Any:
    from ytcollector.config import ConfigError, load_study

    try:
        return load_study(name)
    except ConfigError as error:
        raise GenresError(str(error)) from None


def reference_frame(config: GenresConfig) -> Any:
    from ytcollector.genres.load import read_reference

    return read_reference(config, ignore_expected=True)
