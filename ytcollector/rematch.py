"""Recompute matches and audit the queries, without touching the network.

Both commands here read only what is already stored, so a query can be corrected
and its effect measured without spending a single call (SPEC_FIXES 1.3 and 1.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ytcollector.derive import match_game
from ytcollector.models import Game, GameVideo, VideoSnapshot

logger = logging.getLogger("ytcollector.rematch")

LOW_MATCH_RATE = 0.70
HIGH_OVERLAP = 0.10


@dataclass
class RematchResult:
    """How the decisions changed."""

    pairs: int = 0
    now_matching: int = 0
    now_rejected: int = 0
    changed: int = 0
    by_game: dict[str, dict[str, int]] = field(default_factory=dict)


def rematch(session: Session, study: str, games: list[str] | None = None) -> RematchResult:
    """Re-decide every ``(game, video)`` pair from the stored snapshots.

    No request is made: the titles, tags and descriptions needed are already in
    ``video_snapshots``, and the untruncated descriptions in ``raw_responses``.
    """
    terms = {
        row.game_id: (row.match_terms or [], row.exclude_terms or [])
        for row in session.scalars(select(Game).where(Game.study == study))
    }
    newest = (
        select(VideoSnapshot.video_id, func.max(VideoSnapshot.collected_at).label("collected_at"))
        .group_by(VideoSnapshot.video_id)
        .subquery()
    )
    snapshots = {
        snap.video_id: snap
        for snap in session.scalars(
            select(VideoSnapshot).join(
                newest,
                (VideoSnapshot.video_id == newest.c.video_id)
                & (VideoSnapshot.collected_at == newest.c.collected_at),
            )
        )
    }

    query = select(GameVideo).where(GameVideo.study == study)
    if games:
        query = query.where(GameVideo.game_id.in_(games))

    result = RematchResult()
    for link in session.scalars(query):
        snap = snapshots.get(link.video_id)
        if snap is None:
            continue
        match_terms, exclude_terms = terms.get(link.game_id, ([], []))
        matched, reason = match_game(
            match_terms, exclude_terms,
            title=snap.title, tags=snap.tags, description=snap.description,
        )

        counts = result.by_game.setdefault(
            link.game_id, {"pairs": 0, "matching": 0, "excluded": 0, "changed": 0}
        )
        counts["pairs"] += 1
        result.pairs += 1
        if matched:
            counts["matching"] += 1
        elif reason and reason.startswith("-"):
            counts["excluded"] += 1

        if link.title_match is not matched:
            result.changed += 1
            counts["changed"] += 1
            if matched:
                result.now_matching += 1
            else:
                result.now_rejected += 1

        link.title_match = matched
        link.match_reason = reason

    session.commit()
    logger.info(
        "rematch: %d pairs, %d changed (%d now matching, %d now rejected)",
        result.pairs, result.changed, result.now_matching, result.now_rejected,
    )
    return result


@dataclass
class GameAudit:
    """What one game's query actually brought back."""

    game_id: str
    videos: int = 0
    matching: int = 0
    excluded: int = 0
    overlap: dict[str, float] = field(default_factory=dict)

    @property
    def match_rate(self) -> float:
        return self.matching / self.videos if self.videos else 0.0

    @property
    def excluded_rate(self) -> float:
        return self.excluded / self.videos if self.videos else 0.0

    @property
    def worst_overlap(self) -> tuple[str, float]:
        if not self.overlap:
            return ("", 0.0)
        return max(self.overlap.items(), key=lambda kv: kv[1])

    @property
    def flagged(self) -> bool:
        return self.match_rate < LOW_MATCH_RATE or self.worst_overlap[1] > HIGH_OVERLAP


def audit_queries(session: Session, study: str) -> list[GameAudit]:
    """Per game: what the query found, and how much of it belongs to another game.

    Overlap is the share of a game's matching videos that *also* match another
    game's terms — the symptom that made "PUBG" swallow PUBG Mobile.
    """
    games = list(session.scalars(select(Game).where(Game.study == study, Game.enabled)))
    terms = {g.game_id: (g.match_terms or [], g.exclude_terms or []) for g in games}

    newest = (
        select(VideoSnapshot.video_id, func.max(VideoSnapshot.collected_at).label("collected_at"))
        .group_by(VideoSnapshot.video_id)
        .subquery()
    )
    snapshots = {
        snap.video_id: snap
        for snap in session.scalars(
            select(VideoSnapshot).join(
                newest,
                (VideoSnapshot.video_id == newest.c.video_id)
                & (VideoSnapshot.collected_at == newest.c.collected_at),
            )
        )
    }

    by_game: dict[str, list[str]] = {}
    for game_id, video_id in session.execute(
        select(GameVideo.game_id, GameVideo.video_id)
        .where(GameVideo.study == study)
        .distinct()
    ).all():
        by_game.setdefault(game_id, []).append(video_id)

    audits: list[GameAudit] = []
    for game in games:
        audit = GameAudit(game_id=game.game_id)
        match_terms, exclude_terms = terms[game.game_id]
        matching: list[str] = []

        for video_id in by_game.get(game.game_id, []):
            snap = snapshots.get(video_id)
            if snap is None:
                continue
            audit.videos += 1
            matched, reason = match_game(
                match_terms, exclude_terms,
                title=snap.title, tags=snap.tags, description=snap.description,
            )
            if matched:
                audit.matching += 1
                matching.append(video_id)
            elif reason and reason.startswith("-"):
                audit.excluded += 1

        for other in games:
            if other.game_id == game.game_id or not matching:
                continue
            other_terms, other_exclude = terms[other.game_id]
            hits = sum(
                1
                for video_id in matching
                if match_game(
                    other_terms, other_exclude,
                    title=snapshots[video_id].title,
                    tags=snapshots[video_id].tags,
                    description=snapshots[video_id].description,
                )[0]
            )
            if hits:
                audit.overlap[other.game_id] = hits / len(matching)

        audits.append(audit)

    return sorted(audits, key=lambda a: -a.videos)
