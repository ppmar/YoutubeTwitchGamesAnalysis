"""build-tables and the Twitch join (SPEC section 7, milestone 3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from sqlalchemy import select

from ytcollector import tables
from ytcollector.config import load_study
from ytcollector.games import load_twitch_months, prepare_games
from ytcollector.models import ChannelSnapshot, Game, GameMonth, GameVideo, VideoSnapshot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def seeded(session: object) -> object:
    session.add(Game(  # type: ignore[attr-defined]
        study="s", game_id="apex", game_name="Apex Legends", twitch_names=["Apex Legends"],
        yt_query='"Apex Legends"', match_terms=["Apex Legends"], role="leader", enabled=True,
    ))
    videos = [
        ("v1", "UC1", "2020-01-10", 1000, 100, 50, False, False, "gameplay"),
        ("v2", "UC1", "2020-01-20", 3000, 300, 150, True, False, "trailer"),
        ("v3", "UC2", "2020-02-05", 500, 10, 5, False, True, "esport"),
        # Not a match: must never reach the aggregates.
        ("v4", "UC3", "2020-01-25", 999_999, 1, 1, False, False, "other"),
    ]
    for vid, chan, day, views, likes, comments, short, live, vtype in videos:
        session.add(GameVideo(study="s", game_id="apex", video_id=vid,  # type: ignore[attr-defined]
                              period_start=f"{day[:7]}-01", source="search",
                              title_match=vid != "v4"))
        session.add(VideoSnapshot(  # type: ignore[attr-defined]
            video_id=vid, collected_at=NOW, channel_id=chan,
            published_at=datetime.fromisoformat(f"{day}T12:00:00+00:00"),
            view_count=views, like_count=likes, comment_count=comments,
            is_short_proxy=short, was_live=live, video_type=vtype,
        ))
    session.add(ChannelSnapshot(channel_id="UC1", collected_at=NOW, subscriber_count=2_000_000))  # type: ignore[attr-defined]
    session.add(ChannelSnapshot(channel_id="UC2", collected_at=NOW, subscriber_count=5_000))  # type: ignore[attr-defined]
    session.commit()  # type: ignore[attr-defined]
    return session


class TestBuildGameMonth:
    def test_groups_by_publication_month(self, seeded: object) -> None:
        frame = tables.build_game_month(seeded, "s")  # type: ignore[arg-type]
        assert sorted(frame[["year", "month"]].itertuples(index=False, name=None)) == [
            (2020, 1), (2020, 2)
        ]

    def test_excludes_videos_that_do_not_match(self, seeded: object) -> None:
        frame = tables.build_game_month(seeded, "s")  # type: ignore[arg-type]
        january = frame[(frame["year"] == 2020) & (frame["month"] == 1)].iloc[0]
        # v4 alone has a million views; if it leaked in, this would not be 4000.
        assert january["views_sum"] == 4000
        assert january["n_videos_published"] == 2

    def test_computes_the_documented_aggregates(self, seeded: object) -> None:
        frame = tables.build_game_month(seeded, "s")  # type: ignore[arg-type]
        january = frame[(frame["year"] == 2020) & (frame["month"] == 1)].iloc[0]
        assert january["n_channels_publishing"] == 1
        assert january["views_median"] == 2000
        assert january["likes_sum"] == 400
        assert january["comments_sum"] == 200
        assert january["engagement_rate"] == pytest.approx(600 / 4000)
        assert january["top_channel_view_share"] == pytest.approx(1.0)
        assert january["channel_hhi"] == pytest.approx(1.0)
        assert january["share_shorts"] == pytest.approx(0.5)
        assert january["trailer_views_max"] == 3000
        assert january["median_subscribers"] == 2_000_000

    def test_flags_the_first_big_channel_video(self, seeded: object) -> None:
        frame = tables.build_game_month(seeded, "s")  # type: ignore[arg-type]
        january = frame[(frame["year"] == 2020) & (frame["month"] == 1)].iloc[0]
        february = frame[(frame["year"] == 2020) & (frame["month"] == 2)].iloc[0]
        assert pd.notna(january["first_big_channel_video_at"])
        # UC2 has 5k subscribers, well under the million threshold.
        assert pd.isna(february["first_big_channel_video_at"])

    def test_a_channel_mix_lowers_concentration(self, seeded: object) -> None:
        frame = tables.build_game_month(seeded, "s")  # type: ignore[arg-type]
        february = frame[(frame["year"] == 2020) & (frame["month"] == 2)].iloc[0]
        assert february["share_live"] == pytest.approx(1.0)


class TestWriteAndKeys:
    def test_writes_unique_keys(self, seeded: object) -> None:
        tables.build(seeded, "s")  # type: ignore[arg-type]
        assert tables.join_key_conflicts(seeded, "s") == 0  # type: ignore[arg-type]
        assert len(list(seeded.scalars(select(GameMonth)))) == 2  # type: ignore[attr-defined]

    def test_a_rebuild_is_idempotent(self, seeded: object) -> None:
        tables.build(seeded, "s")  # type: ignore[arg-type]
        tables.build(seeded, "s")  # type: ignore[arg-type]
        assert len(list(seeded.scalars(select(GameMonth)))) == 2  # type: ignore[attr-defined]
        assert tables.join_key_conflicts(seeded, "s") == 0  # type: ignore[arg-type]

    def test_attention_columns_survive_a_rebuild(self, seeded: object) -> None:
        tables.build(seeded, "s")  # type: ignore[arg-type]
        row = seeded.scalar(select(GameMonth).where(GameMonth.month == 1))  # type: ignore[attr-defined]
        row.attention_index = 100.0
        seeded.commit()  # type: ignore[attr-defined]

        tables.build(seeded, "s")  # type: ignore[arg-type]

        row = seeded.scalar(select(GameMonth).where(GameMonth.month == 1))  # type: ignore[attr-defined]
        assert row.attention_index == 100.0

    def test_empty_input_produces_no_rows(self, session: object) -> None:
        assert tables.build(session, "s") == {"yt_game_month": 0}  # type: ignore[arg-type]


class TestTwitchJoin:
    def test_case_variants_collapse_to_one_key(self, session: object, twitch_csv: object) -> None:
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        keys = tables.twitch_key_map(session, "battle_royale")  # type: ignore[arg-type]
        naraka = keys[keys["game_id"] == "naraka-bladepoint"]
        # The study file lists NARAKA: BLADEPOINT and Naraka: Bladepoint; one key.
        assert len(naraka) == 1


    def test_a_raw_join_fans_out_on_multi_category_games(
        self, session: object, twitch_csv: object
    ) -> None:
        # Documents the trap: Fortnite and Fortnite Zero Build are both in the top
        # 200 in some months, so a row-level join duplicates those months.
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        keys = tables.twitch_key_map(session, "battle_royale")  # type: ignore[arg-type]
        twitch = load_twitch_months(twitch_csv)  # type: ignore[arg-type]

        raw = twitch.merge(keys, on="key", how="inner")

        assert raw.duplicated(["game_id", "month"]).any()

    def test_twitch_game_month_is_one_row_per_game_month(
        self, session: object, twitch_csv: object
    ) -> None:
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        twitch = load_twitch_months(twitch_csv)  # type: ignore[arg-type]

        folded = tables.twitch_game_month(session, "battle_royale", twitch)  # type: ignore[arg-type]

        assert not folded.duplicated(["game_id", "year", "month"]).any()
        assert len(folded) > 0

    def test_the_fold_keeps_every_months_hours(
        self, session: object, twitch_csv: object
    ) -> None:
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        twitch = load_twitch_months(twitch_csv)  # type: ignore[arg-type]
        folded = tables.twitch_game_month(session, "battle_royale", twitch)  # type: ignore[arg-type]

        fortnite = folded[folded["game_id"] == "fortnite"]
        raw = twitch[twitch["key"].isin({"fortnite", "fortnite zero build"})]
        # Folding sums the two categories, it does not drop one of them.
        assert fortnite["hours_watched"].sum() == raw["hours_watched"].sum()
        assert len(fortnite) == raw["month"].nunique()

    def test_valid_from_is_applied_at_join_time(
        self, session: object, twitch_csv: object
    ) -> None:
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        twitch = load_twitch_months(twitch_csv)  # type: ignore[arg-type]
        folded = tables.twitch_game_month(session, "battle_royale", twitch)  # type: ignore[arg-type]

        warzone = folded[folded["game_id"] == "call-of-duty-warzone"]
        # Months before 2020-03 are Modern Warfare, not Warzone.
        assert warzone["year_month"].min() == "2020-03"

    def test_every_study_game_matches_at_least_one_twitch_label(
        self, session: object, twitch_csv: object
    ) -> None:
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        rows = list(session.scalars(select(Game).where(Game.study == "battle_royale", Game.enabled)))  # type: ignore[attr-defined]
        assert [r.game_id for r in rows if not r.months_in_top200] == []
