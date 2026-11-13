"""Derived video fields (SPEC section 5, step 2, milestone 3)."""

from __future__ import annotations

import pytest

from ytcollector.derive import (
    classify_video_type,
    duration_to_seconds,
    matches_terms,
    parse_statistic,
)


class TestDurationToSeconds:
    @pytest.mark.parametrize(
        ("iso", "seconds"),
        [
            ("PT30S", 30),
            ("PT1M", 60),
            ("PT1M30S", 90),
            ("PT2H5M3S", 7503),
            ("P1DT2H", 93600),
            ("PT0S", 0),
        ],
    )
    def test_parses_iso_8601(self, iso: str, seconds: int) -> None:
        assert duration_to_seconds(iso) == seconds

    def test_unparseable_gives_none(self) -> None:
        assert duration_to_seconds("") is None
        assert duration_to_seconds(None) is None
        assert duration_to_seconds("nonsense") is None


class TestParseStatistic:
    def test_reads_the_string_counter(self) -> None:
        assert parse_statistic({"viewCount": "1234"}, "viewCount") == 1234

    def test_a_hidden_statistic_is_none_not_zero(self) -> None:
        # Channels and videos can hide counters; 0 would be a lie.
        assert parse_statistic({}, "likeCount") is None

    def test_a_real_zero_stays_zero(self) -> None:
        assert parse_statistic({"likeCount": "0"}, "likeCount") == 0


class TestMatchesTerms:
    def test_finds_a_term_in_the_title(self) -> None:
        assert matches_terms(["Apex Legends"], title="New APEX LEGENDS season")

    def test_ignores_case_and_accents(self) -> None:
        assert matches_terms(["Pokémon"], title="POKEMON gameplay")
        assert matches_terms(["Pokemon"], title="Pokémon gameplay")

    def test_looks_in_tags_and_description(self) -> None:
        assert matches_terms(["PUBG"], title="road to ace", tags=["pubg", "fps"])
        assert matches_terms(["PUBG"], title="road to ace", description="playing pubg today")

    def test_rejects_when_no_term_appears(self) -> None:
        assert not matches_terms(["Fortnite"], title="Minecraft survival")

    def test_requires_a_whole_word(self) -> None:
        # "SOS" must not match "SOSpital" or similar noise.
        assert not matches_terms(["SOS"], title="Unboxing my new SOSpital kit")
        assert matches_terms(["SOS"], title="SOS gameplay")


class TestClassifyVideoType:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Apex Legends Season 20 Launch Trailer", "trailer"),
            ("ALGS Championship Grand Finals", "esport"),
            ("Is Fortnite worth playing in 2024? Review", "review"),
            ("PUBG beginner guide: best settings", "guide"),
            ("Insane 20 kills solo squad gameplay", "gameplay"),
            ("random thoughts about nothing", "other"),
        ],
    )
    def test_classifies_from_the_title(self, title: str, expected: str) -> None:
        assert classify_video_type(title) == expected

    def test_trailer_wins_over_gameplay(self) -> None:
        # A "gameplay trailer" is a trailer; order decides.
        assert classify_video_type("Official gameplay trailer") == "trailer"

    def test_empty_title_is_other(self) -> None:
        assert classify_video_type("") == "other"


class TestSeparatorInsensitiveMatching:
    """Non-English titles write the game name without or with another separator."""

    def test_matches_a_name_written_without_its_space(self) -> None:
        assert matches_terms(["Apex Legends"], title="【ApexLegends】現役アイドルが魅せた神ムーブ")

    def test_matches_a_name_written_with_a_hyphen(self) -> None:
        assert matches_terms(["Apex Legends"], title="【APEX-LEGENDS-】ヒットボックス")

    def test_matches_across_tags_too(self) -> None:
        assert matches_terms(["Call of Duty"], title="clip", tags=["callofduty"])

    def test_a_single_word_term_keeps_its_boundaries(self) -> None:
        # Relaxing this one would match SOS inside any word containing those letters.
        assert not matches_terms(["SOS"], title="Unboxing my new SOSpital kit")
        assert not matches_terms(["PUBG"], title="pubgames review")

    def test_a_multi_word_term_still_rejects_unrelated_text(self) -> None:
        assert not matches_terms(["Apex Legends"], title="Minecraft survival series")
