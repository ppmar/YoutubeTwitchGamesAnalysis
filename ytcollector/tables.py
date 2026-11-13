"""Rebuild the analysis tables from what is already stored (SPEC section 7).

No network call happens here: everything comes from the snapshots and the raw
response cache, so the tables can be rebuilt at will after a logic change.

Aggregates are keyed by *publication* month, which is the only date the API gives
for a video. The attention score, which spreads views over time, is computed
separately in :mod:`ytcollector.attention`.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ytcollector.models import ChannelSnapshot, Game, GameMonth, GameVideo, VideoSnapshot

logger = logging.getLogger("ytcollector.tables")

BIG_CHANNEL_THRESHOLD = 1_000_000


def latest_video_snapshots(session: Session) -> pd.DataFrame:
    """One row per video: its most recent snapshot."""
    newest = (
        select(VideoSnapshot.video_id, func.max(VideoSnapshot.collected_at).label("collected_at"))
        .group_by(VideoSnapshot.video_id)
        .subquery()
    )
    rows = session.execute(
        select(VideoSnapshot).join(
            newest,
            (VideoSnapshot.video_id == newest.c.video_id)
            & (VideoSnapshot.collected_at == newest.c.collected_at),
        )
    ).scalars()
    return pd.DataFrame(
        [
            {
                "video_id": r.video_id,
                "channel_id": r.channel_id,
                "published_at": r.published_at,
                "view_count": r.view_count,
                "like_count": r.like_count,
                "comment_count": r.comment_count,
                "is_short_proxy": r.is_short_proxy,
                "was_live": r.was_live,
                "title_match": r.title_match,
                "video_type": r.video_type,
            }
            for r in rows
        ]
    )


def latest_channel_snapshots(session: Session) -> pd.DataFrame:
    newest = (
        select(
            ChannelSnapshot.channel_id,
            func.max(ChannelSnapshot.collected_at).label("collected_at"),
        )
        .group_by(ChannelSnapshot.channel_id)
        .subquery()
    )
    rows = session.execute(
        select(ChannelSnapshot).join(
            newest,
            (ChannelSnapshot.channel_id == newest.c.channel_id)
            & (ChannelSnapshot.collected_at == newest.c.collected_at),
        )
    ).scalars()
    return pd.DataFrame(
        [{"channel_id": r.channel_id, "subscriber_count": r.subscriber_count} for r in rows]
    )


def game_video_links(session: Session, study: str) -> pd.DataFrame:
    """Distinct (game, video) pairs: a video found in several months counts once."""
    rows = session.execute(
        select(GameVideo.game_id, GameVideo.video_id)
        .where(GameVideo.study == study)
        .distinct()
    ).all()
    return pd.DataFrame(rows, columns=["game_id", "video_id"])


def _hhi(shares: pd.Series) -> float:
    """Herfindahl index of channel concentration, 0 to 1."""
    total = shares.sum()
    if not total:
        return 0.0
    return float(((shares / total) ** 2).sum())


def _aggregate_group(frame: pd.DataFrame) -> dict[str, Any]:
    views = frame["view_count"].fillna(0)
    by_channel = frame.groupby("channel_id")["view_count"].sum().fillna(0)
    total_views = float(views.sum())
    likes = float(frame["like_count"].fillna(0).sum())
    comments = float(frame["comment_count"].fillna(0).sum())
    trailers = frame.loc[frame["video_type"] == "trailer", "view_count"]
    big = frame[frame["subscriber_count"].fillna(0) >= BIG_CHANNEL_THRESHOLD]

    return {
        "n_videos_published": len(frame),
        "n_channels_publishing": int(frame["channel_id"].nunique()),
        "views_sum": int(total_views),
        "views_median": float(views.median()),
        "likes_sum": int(likes),
        "comments_sum": int(comments),
        "engagement_rate": float((likes + comments) / total_views) if total_views else None,
        "top_channel_view_share": float(by_channel.max() / total_views) if total_views else None,
        "channel_hhi": _hhi(by_channel),
        "share_shorts": float(frame["is_short_proxy"].fillna(False).mean()),
        "share_live": float(frame["was_live"].fillna(False).mean()),
        "share_by_video_type": {
            k: float(v) for k, v in frame["video_type"].value_counts(normalize=True).items()
        },
        "median_subscribers": (
            float(frame["subscriber_count"].median())
            if frame["subscriber_count"].notna().any()
            else None
        ),
        "first_video_at": frame["published_at"].min(),
        "first_big_channel_video_at": big["published_at"].min() if len(big) else None,
        "trailer_views_max": int(trailers.max()) if len(trailers) and trailers.notna().any() else None,
    }


def build_game_month(session: Session, study: str) -> pd.DataFrame:
    """Aggregate matched videos by (game_id, year, month of publication)."""
    videos = latest_video_snapshots(session)
    if videos.empty:
        return pd.DataFrame()

    videos = videos[videos["title_match"].fillna(False)]
    videos = videos[videos["published_at"].notna()]
    if videos.empty:
        return pd.DataFrame()

    links = game_video_links(session, study)
    if links.empty:
        return pd.DataFrame()

    frame = links.merge(videos, on="video_id", how="inner")
    channels = latest_channel_snapshots(session)
    frame = (
        frame.merge(channels, on="channel_id", how="left")
        if not channels.empty
        else frame.assign(subscriber_count=pd.NA)
    )

    published = pd.to_datetime(frame["published_at"], utc=True)
    frame["year"] = published.dt.year
    frame["month"] = published.dt.month

    records = []
    for (game_id, year, month), group in frame.groupby(["game_id", "year", "month"]):
        record = {"game_id": game_id, "year": int(year), "month": int(month)}
        record.update(_aggregate_group(group))
        records.append(record)
    return pd.DataFrame(records)


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    """Turn pandas' NaN/NaT and numpy scalars into values the database accepts.

    Building a frame from records with holes promotes integer columns to float, so
    a missing count arrives as NaN and would blow up on an integer column.
    """
    cleaned: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, dict):
            cleaned[key] = value
        elif value is None or (not isinstance(value, (list, str)) and pd.isna(value)):
            cleaned[key] = None
        elif hasattr(value, "item") and not isinstance(value, pd.Timestamp):
            cleaned[key] = value.item()
        else:
            cleaned[key] = value
    return cleaned


def twitch_key_map(session: Session, study: str) -> pd.DataFrame:
    """Deduplicated ``(game_id, key)`` pairs for joining Twitch rows to games.

    A game may list several Twitch labels that differ only by case; they share one
    grouping key, so the pairs must be deduplicated or the join fans out.
    """
    from ytcollector.textnorm import grouping_key

    rows = session.scalars(select(Game).where(Game.study == study))
    pairs = {
        (row.game_id, grouping_key(name)) for row in rows for name in (row.twitch_names or [])
    }
    return pd.DataFrame(sorted(pairs), columns=["game_id", "key"])


def twitch_game_month(session: Session, study: str, twitch: pd.DataFrame) -> pd.DataFrame:
    """Twitch rows folded to one row per ``(game_id, year, month)``.

    Two things make a naive join wrong:

    - a game can hold **two Twitch categories in the same month** (Fortnite and
      Fortnite Zero Build), so joining row by row fans out;
    - months before a game's ``valid_from`` belong to another game that shared the
      category (Warzone / Modern Warfare) and must be dropped.

    This is the frame to join against ``yt_game_month``; it is one to one.
    """
    keys = twitch_key_map(session, study)
    if keys.empty or twitch.empty:
        return pd.DataFrame(columns=["game_id", "year", "month", "hours_watched", "best_rank"])

    valid_from = {
        row.game_id: row.valid_from
        for row in session.scalars(select(Game).where(Game.study == study))
        if row.valid_from
    }

    joined = twitch.merge(keys, on="key", how="inner")
    if valid_from:
        floor = joined["game_id"].map(valid_from)
        joined = joined[floor.isna() | (joined["month"] >= floor)]

    folded = (
        joined.groupby(["game_id", "month"], as_index=False)
        .agg(hours_watched=("hours_watched", "sum"), best_rank=("rank", "min"))
    )
    folded["year"] = folded["month"].str[:4].astype(int)
    folded["month_number"] = folded["month"].str[5:7].astype(int)
    return folded.rename(columns={"month": "year_month", "month_number": "month"})[
        ["game_id", "year", "month", "year_month", "hours_watched", "best_rank"]
    ]


def write_game_month(session: Session, study: str, frame: pd.DataFrame) -> int:
    """Replace the study's rows in ``yt_game_month``.

    Attention columns written by `build-attention` are preserved: this rebuild
    only owns the publication-month aggregates.
    """
    if frame.empty:
        return 0

    kept = {
        (row.game_id, row.year, row.month): row
        for row in session.scalars(select(GameMonth).where(GameMonth.study == study))
    }
    attention = {
        key: (row.attention_raw, row.attention_index, row.share_method_a,
              row.n_comments_dated, row.sample_basis)
        for key, row in kept.items()
    }
    session.execute(delete(GameMonth).where(GameMonth.study == study))

    for raw in frame.to_dict("records"):
        record = _clean(raw)
        key = (record["game_id"], record["year"], record["month"])
        row = GameMonth(study=study, **record)
        if key in attention:
            (row.attention_raw, row.attention_index, row.share_method_a,
             row.n_comments_dated, row.sample_basis) = attention[key]
        session.add(row)

    session.commit()
    return len(frame)


def build(session: Session, study: str) -> dict[str, int]:
    """Rebuild every analysis table. Returns a row count per table."""
    frame = build_game_month(session, study)
    written = write_game_month(session, study, frame)
    logger.info("built yt_game_month: %d rows", written)
    return {"yt_game_month": written}


def join_key_conflicts(session: Session, study: str) -> int:
    """Count duplicate (game_id, year, month) keys. Must always be zero."""
    duplicates = session.execute(
        select(GameMonth.game_id, GameMonth.year, GameMonth.month, func.count())
        .where(GameMonth.study == study)
        .group_by(GameMonth.game_id, GameMonth.year, GameMonth.month)
        .having(func.count() > 1)
    ).all()
    return len(duplicates)


def games_frame(session: Session, study: str) -> pd.DataFrame:
    """The study roster as a frame, for exports and the Twitch join."""
    rows = session.scalars(select(Game).where(Game.study == study))
    return pd.DataFrame(
        [
            {
                "game_id": r.game_id,
                "game_name": r.game_name,
                "twitch_names": r.twitch_names,
                "role": r.role,
                "enabled": r.enabled,
                "valid_from": r.valid_from,
                "months_in_top200": r.months_in_top200,
                "first_month": r.first_month,
                "last_month": r.last_month,
                "peak_month": r.peak_month,
            }
            for r in rows
        ]
    )
