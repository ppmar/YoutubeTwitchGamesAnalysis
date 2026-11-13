"""Fields derived from an API payload (SPEC section 5, step 2).

Kept apart from the collection stages so they can be tested on their own and
reapplied by `build-tables` without any network call.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any

from ytcollector.config import CONFIG_DIR, read_yaml

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

# Everything that is not a letter or a digit, for separator-insensitive matching.
_COMPACT = re.compile(r"[^0-9a-z]+")

SHORT_MAX_SECONDS = 60


def duration_to_seconds(iso: str | None) -> int | None:
    """Convert an ISO 8601 duration to seconds, or None if it cannot be read."""
    if not iso:
        return None
    match = _DURATION.match(iso)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    total = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return total


def parse_statistic(statistics: dict[str, Any], field: str) -> int | None:
    """Read a counter, returning None when it is hidden.

    A hidden statistic is not a zero, and storing it as one would silently
    understate engagement (SPEC section 5).
    """
    raw = statistics.get(field)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fold(text: str | None) -> str:
    """Lowercase and strip accents, for comparing titles against match terms."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def find_term(
    terms: list[str],
    haystack: str,
    compact_haystack: str | None = None,
) -> str | None:
    """Return the first term found in ``haystack``, or None.

    Whole words only: a bare substring test turns generic names like ``SOS``
    into a flood of false positives (SPEC section 12). Multi-word terms are
    additionally compared with separators removed, because non-English titles
    write the name as ``ApexLegends`` or ``APEX-LEGENDS``.
    """
    compact_haystack = (
        _COMPACT.sub("", haystack) if compact_haystack is None else compact_haystack
    )
    for term in terms:
        needle = fold(term)
        if not needle:
            continue
        if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
            return term
        if " " in needle and _COMPACT.sub("", needle) in compact_haystack:
            return term
    return None


def match_game(
    terms: list[str],
    exclude_terms: list[str] | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> tuple[bool, str | None]:
    """Whether a video is about one game, and what decided it.

    A match needs one of ``terms`` somewhere in the title, tags or description,
    **and** no ``exclude_terms`` in the title or tags. The description is left out
    of the exclusion check on purpose: it is long and often lists every game a
    channel covers, so excluding on it would throw away good videos
    (SPEC_FIXES 1.2).

    Returns ``(matched, reason)`` where the reason names the term that decided,
    prefixed with ``-`` when an exclusion blocked the match.
    """
    haystack = " ".join(
        [fold(title), " ".join(fold(t) for t in (tags or [])), fold(description)]
    )
    if not haystack.strip():
        return False, None

    hit = find_term(terms, haystack)
    if hit is None:
        return False, None

    if exclude_terms:
        # Title and tags only.
        narrow = " ".join([fold(title), " ".join(fold(t) for t in (tags or []))])
        blocked = find_term(exclude_terms, narrow)
        if blocked is not None:
            return False, f"-{blocked}"

    return True, hit


def matches_terms(
    terms: list[str],
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> bool:
    """Backwards-compatible boolean form of :func:`match_game`."""
    return match_game(terms, None, title, tags, description)[0]


@lru_cache(maxsize=1)
def _video_type_rules() -> tuple[list[str], dict[str, list[str]]]:
    data = read_yaml(CONFIG_DIR / "video_types.yml")
    order = [str(t) for t in data.get("order", [])]
    types = {k: [fold(v) for v in vs] for k, vs in (data.get("types") or {}).items()}
    return order, types


def classify_video_type(title: str | None) -> str:
    """Classify a video from its title, following ``config/video_types.yml``.

    The first type in the configured order whose keywords appear wins, so
    "gameplay trailer" is a trailer.
    """
    order, types = _video_type_rules()
    folded = fold(title)
    if not folded:
        return "other"
    for video_type in order:
        for keyword in types.get(video_type, []):
            if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", folded):
                return video_type
    return "other"
