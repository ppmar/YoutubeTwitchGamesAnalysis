"""Game-name normalisation.

The Twitch export carries two kinds of broken accents (SPEC section 3):

- literal escape sequences, ``Pok<U+00E9>mon``;
- mojibake from a UTF-8/latin-1 round trip, ``PokÃ©mon``.

Both must converge on the same name before games are grouped, otherwise a single
game is split across several rows.
"""

from __future__ import annotations

import re
import unicodedata

import ftfy

_ESCAPE = re.compile(r"<U\+([0-9A-Fa-f]{4,6})>")
_WHITESPACE = re.compile(r"\s+")


def unescape_unicode(value: str) -> str:
    """Replace ``<U+XXXX>`` escape sequences with the character they denote.

    Malformed sequences are left untouched rather than raising.
    """

    def _replace(match: re.Match[str]) -> str:
        code_point = int(match.group(1), 16)
        if code_point > 0x10FFFF:
            return match.group(0)
        return chr(code_point)

    return _ESCAPE.sub(_replace, value)


def normalize_name(value: str | None) -> str:
    """Return the display form of a game name: escapes resolved, mojibake repaired.

    Case is preserved; use :func:`grouping_key` to compare two names.
    """
    if not value:
        return ""
    text = unescape_unicode(value)
    text = ftfy.fix_text(text)
    text = unicodedata.normalize("NFKC", text)
    return _WHITESPACE.sub(" ", text).strip()


def grouping_key(value: str | None) -> str:
    """Return the case-insensitive key used to merge Twitch labels into one game."""
    return normalize_name(value).casefold()
