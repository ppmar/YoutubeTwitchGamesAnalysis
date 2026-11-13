"""Game-name normalisation (SPEC section 4, milestone 1)."""

import pytest

from ytcollector.textnorm import grouping_key, normalize_name, unescape_unicode


class TestUnescapeUnicode:
    def test_replaces_escape_with_character(self) -> None:
        assert unescape_unicode("Pok<U+00E9>mon GO") == "Pokémon GO"

    def test_replaces_every_occurrence(self) -> None:
        assert unescape_unicode("Werewolves <U+72FC><U+4EBA><U+6BBA>") == "Werewolves 狼人殺"

    def test_handles_six_digit_code_points(self) -> None:
        assert unescape_unicode("<U+01F600>") == "\U0001f600"

    def test_leaves_plain_text_alone(self) -> None:
        assert unescape_unicode("Apex Legends") == "Apex Legends"

    def test_leaves_malformed_escape_alone(self) -> None:
        assert unescape_unicode("Half<U+Life>") == "Half<U+Life>"


class TestNormalizeName:
    def test_fixes_the_escape_form(self) -> None:
        assert normalize_name("Pok<U+00E9>mon Trading Card Game") == "Pokémon Trading Card Game"

    def test_fixes_the_mojibake_form(self) -> None:
        assert normalize_name("PokÃ©mon Trading Card Game") == "Pokémon Trading Card Game"

    def test_both_damaged_forms_converge(self) -> None:
        assert normalize_name("Pok<U+00E9>mon GO") == normalize_name("PokÃ©mon GO") == "Pokémon GO"

    def test_strips_and_collapses_whitespace(self) -> None:
        assert normalize_name("  Apex   Legends \t") == "Apex Legends"

    def test_preserves_case(self) -> None:
        assert normalize_name("NARAKA: BLADEPOINT") == "NARAKA: BLADEPOINT"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_input_gives_empty_string(self, value: str | None) -> None:
        assert normalize_name(value) == ""


class TestGroupingKey:
    def test_case_variants_share_a_key(self) -> None:
        assert grouping_key("NARAKA: BLADEPOINT") == grouping_key("Naraka: Bladepoint")

    def test_elden_ring_variants_share_a_key(self) -> None:
        assert grouping_key("ELDEN RING") == grouping_key("Elden Ring")

    def test_damaged_encodings_share_a_key(self) -> None:
        assert grouping_key("Pok<U+00E9>mon GO") == grouping_key("PokÃ©mon GO")

    def test_different_games_do_not_collide(self) -> None:
        assert grouping_key("Fortnite") != grouping_key("Fortnite Zero Build")
