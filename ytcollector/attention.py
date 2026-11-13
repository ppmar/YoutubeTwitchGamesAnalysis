"""Monthly attention score per game (SPEC section 6).

The problem: YouTube gives a cumulative view count and no history, so a video
published in 2018 with 10 million views tells you nothing about *when* those views
happened. The only natively dated signal is the timestamp of each comment.

The answer is to spread each video's views over time using its comment
distribution as the shape:

- **Method A**, for videos with a complete comment history: the views of month *t*
  are proportional to the comments of month *t*, weighted by ``r(age)``, the number
  of views per comment at that age. Version 1 assumes ``r = 1``; version 2 estimates
  it from weekly snapshots.
- **Method B**, for everything else: the views follow ``k``, the average normalised
  curve of the method-A videos in the same stratum.

The result is a **relative index**, never a view count. It is called ``attention``
everywhere, never ``views``.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ytcollector.models import Comment, GameMonth, GameVideo, VideoSnapshot
from ytcollector.planning import month_range, shift_month

logger = logging.getLogger("ytcollector.attention")

MIN_COMMENTS_FOR_METHOD_A = 30
# How far past publication the decay kernel is allowed to spread attention.
KERNEL_HORIZON_MONTHS = 36
VIEW_BUCKETS = (10_000, 100_000, 1_000_000, 10_000_000)

METHOD_A = "a"
METHOD_B = "b"


def view_bucket(views: int | None) -> int:
    """Index of the order-of-magnitude bucket a video falls in."""
    value = views or 0
    return sum(1 for edge in VIEW_BUCKETS if value >= edge)


def stratum_of(video_type: str | None, is_short: bool | None, views: int | None) -> tuple:
    """Videos behave differently by format and by size; group like with like."""
    return (video_type or "other", bool(is_short), view_bucket(views))


def month_of(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m")


def months_between(start: str, end: str) -> int:
    def index(month: str) -> int:
        year, mon = (int(p) for p in month.split("-"))
        return year * 12 + mon

    return index(end) - index(start)


@dataclass
class VideoCurve:
    """One video's attention spread over months."""

    video_id: str
    method: str
    series: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.series.values())


# Method A ------------------------------------------------------------------


def constant_rate(_age_months: int) -> float:
    """``r = 1``: the comment rate is assumed flat over a video's life.

    This is the version 1 assumption and it is optimistic. Viewers comment early,
    so a flat rate pulls attention towards publication. Version 2 replaces it with
    a rate measured from the weekly snapshots.
    """
    return 1.0


def spread_by_comments(
    views: int,
    published_month: str,
    comment_counts: dict[str, int],
    rate: Callable[[int], float] = constant_rate,
) -> dict[str, float]:
    """Method A: distribute ``views`` across months in proportion to comments.

    The result always sums back to ``views``: this is a redistribution, not an
    estimate of extra traffic.
    """
    weights = {
        month: count * rate(months_between(published_month, month))
        for month, count in comment_counts.items()
        if count > 0
    }
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {month: views * weight / total for month, weight in weights.items()}


# Method B ------------------------------------------------------------------


def normalise_curve(series: dict[str, float], published_month: str) -> dict[int, float]:
    """Re-express a curve as shares by age in months, so curves can be averaged."""
    total = sum(series.values())
    if total <= 0:
        return {}
    by_age: dict[int, float] = defaultdict(float)
    for month, value in series.items():
        age = months_between(published_month, month)
        if age >= 0:
            by_age[age] += value / total
    return dict(by_age)


def average_kernel(curves: Iterable[dict[int, float]]) -> list[float]:
    """Average several normalised curves into one decay kernel."""
    buckets: dict[int, list[float]] = defaultdict(list)
    count = 0
    for curve in curves:
        count += 1
        for age, share in curve.items():
            if age < KERNEL_HORIZON_MONTHS:
                buckets[age].append(share)
    if not count:
        return []
    kernel = [sum(buckets.get(age, [])) / count for age in range(KERNEL_HORIZON_MONTHS)]
    total = sum(kernel)
    return [v / total for v in kernel] if total > 0 else []


def default_kernel() -> list[float]:
    """Fallback when no method-A video exists yet: a plain geometric decay.

    Deliberately crude. A run that leans on this should say so, which is what
    ``share_method_a`` is for.
    """
    raw = [0.5**i for i in range(KERNEL_HORIZON_MONTHS)]
    total = sum(raw)
    return [v / total for v in raw]


def spread_by_kernel(
    views: int, published_month: str, kernel: list[float], last_month: str | None = None
) -> dict[str, float]:
    """Method B: distribute ``views`` along the average decay curve."""
    if not kernel:
        return {}
    horizon = len(kernel)
    if last_month is not None:
        horizon = min(horizon, max(1, months_between(published_month, last_month) + 1))
    usable = kernel[:horizon]
    total = sum(usable)
    if total <= 0:
        return {}
    return {
        shift_month(published_month, age): views * share / total
        for age, share in enumerate(usable)
        if share > 0
    }


# Calibration ---------------------------------------------------------------


def snapshot_deltas(session: Session) -> list[tuple[int, float, float]]:
    """``(age_months, delta_views, delta_comments)`` between consecutive snapshots."""
    rows = session.execute(
        select(
            VideoSnapshot.video_id,
            VideoSnapshot.collected_at,
            VideoSnapshot.published_at,
            VideoSnapshot.view_count,
            VideoSnapshot.comment_count,
        ).order_by(VideoSnapshot.video_id, VideoSnapshot.collected_at)
    ).all()

    deltas: list[tuple[int, float, float]] = []
    previous: dict[str, tuple[datetime, int | None, int | None]] = {}
    for video_id, collected_at, published_at, views, comments in rows:
        last = previous.get(video_id)
        previous[video_id] = (collected_at, views, comments)
        if last is None or published_at is None:
            continue
        _, last_views, last_comments = last
        if None in (views, comments, last_views, last_comments):
            continue
        delta_views = float(views - last_views)
        delta_comments = float(comments - last_comments)
        if delta_comments <= 0 or delta_views < 0:
            continue
        age = max(0, months_between(month_of(published_at), month_of(collected_at)))
        deltas.append((age, delta_views, delta_comments))
    return deltas


def calibrate_rate(session: Session, min_observations: int = 4) -> Callable[[int], float]:
    """Estimate ``r(age)`` from the weekly snapshots.

    For each age bucket, the median of ``delta views / delta comments``. Ages with
    too few observations fall back to the overall median, and an empty history
    falls back to ``r = 1``.
    """
    deltas = snapshot_deltas(session)
    if len(deltas) < min_observations:
        logger.info("not enough snapshots to calibrate (%d); using r = 1", len(deltas))
        return constant_rate

    by_age: dict[int, list[float]] = defaultdict(list)
    for age, delta_views, delta_comments in deltas:
        by_age[age].append(delta_views / delta_comments)

    overall = statistics.median([v for values in by_age.values() for v in values])
    table = {
        age: statistics.median(values) for age, values in by_age.items() if len(values) >= 2
    }

    def rate(age_months: int) -> float:
        return table.get(age_months, overall) or 1.0

    return rate


def validation_report(session: Session) -> dict[str, float | int]:
    """Compare predicted weekly views against what the snapshots actually observed.

    Only meaningful once several weekly snapshots exist; it is the honest check on
    whether the comment proxy holds.
    """
    deltas = snapshot_deltas(session)
    if len(deltas) < 2:
        return {"observations": len(deltas)}

    rate = calibrate_rate(session)
    predicted = [rate(age) * delta_comments for age, _, delta_comments in deltas]
    observed = [delta_views for _, delta_views, _ in deltas]
    errors = [
        abs(p - o) / o for p, o in zip(predicted, observed, strict=True) if o > 0
    ]

    report: dict[str, float | int] = {
        "observations": len(deltas),
        "median_relative_error": float(statistics.median(errors)) if errors else float("nan"),
    }
    if len(observed) >= 2 and len(set(observed)) > 1 and len(set(predicted)) > 1:
        report["rank_correlation"] = float(_spearman(predicted, observed))
    return report


def _spearman(a: list[float], b: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, index in enumerate(order, start=1):
            out[index] = float(rank)
        return out

    return statistics.correlation(ranks(a), ranks(b))


# Building ------------------------------------------------------------------


def _video_rows(session: Session, study: str) -> list:
    newest = (
        select(VideoSnapshot.video_id, func.max(VideoSnapshot.collected_at).label("collected_at"))
        .group_by(VideoSnapshot.video_id)
        .subquery()
    )
    return session.execute(
        select(VideoSnapshot)
        .join(
            newest,
            (VideoSnapshot.video_id == newest.c.video_id)
            & (VideoSnapshot.collected_at == newest.c.collected_at),
        )
        .where(
            VideoSnapshot.title_match.is_(True),
            VideoSnapshot.video_id.in_(
                select(GameVideo.video_id).where(GameVideo.study == study)
            ),
        )
    ).scalars().all()


def _comment_counts(session: Session) -> dict[str, dict[str, int]]:
    rows = session.execute(
        select(Comment.video_id, Comment.comment_published_at)
    ).all()
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for video_id, published in rows:
        counts[video_id][month_of(published)] += 1
    return {k: dict(v) for k, v in counts.items()}


def build_curves(
    session: Session, study: str, calibrated: bool = False
) -> tuple[list[VideoCurve], dict[str, str]]:
    """Spread every matched video's views over months. Returns curves and methods."""
    videos = _video_rows(session, study)
    comments = _comment_counts(session)
    rate = calibrate_rate(session) if calibrated else constant_rate

    curves: list[VideoCurve] = []
    method_a_by_stratum: dict[tuple, list[dict[int, float]]] = defaultdict(list)
    deferred: list = []

    for video in videos:
        if video.published_at is None or not video.view_count:
            continue
        published = month_of(video.published_at)
        counts = comments.get(video.video_id, {})
        usable = (
            not video.comments_truncated
            and sum(counts.values()) >= MIN_COMMENTS_FOR_METHOD_A
        )
        if usable:
            series = spread_by_comments(video.view_count, published, counts, rate)
            if series:
                curve = VideoCurve(video.video_id, METHOD_A, series)
                curves.append(curve)
                method_a_by_stratum[
                    stratum_of(video.video_type, video.is_short_proxy, video.view_count)
                ].append(normalise_curve(series, published))
                continue
        deferred.append(video)

    overall = average_kernel(
        [c for curves_ in method_a_by_stratum.values() for c in curves_]
    ) or default_kernel()
    kernels = {
        stratum: average_kernel(items) or overall
        for stratum, items in method_a_by_stratum.items()
    }

    today = datetime.now(UTC).strftime("%Y-%m")
    for video in deferred:
        published = month_of(video.published_at)
        stratum = stratum_of(video.video_type, video.is_short_proxy, video.view_count)
        series = spread_by_kernel(
            video.view_count, published, kernels.get(stratum, overall), last_month=today
        )
        if series:
            curves.append(VideoCurve(video.video_id, METHOD_B, series))

    methods = {c.video_id: c.method for c in curves}
    return curves, methods


def build(session: Session, study: str, calibrated: bool = False) -> int:
    """Compute and store the monthly attention score. Returns rows written."""
    curves, _ = build_curves(session, study, calibrated=calibrated)
    if not curves:
        return 0

    by_video = {c.video_id: c for c in curves}
    links = session.execute(
        select(GameVideo.game_id, GameVideo.video_id)
        .where(GameVideo.study == study)
        .distinct()
    ).all()

    raw: dict[tuple[str, str], float] = defaultdict(float)
    from_a: dict[tuple[str, str], float] = defaultdict(float)
    videos_per_game: dict[str, set[str]] = defaultdict(set)

    for game_id, video_id in links:
        curve = by_video.get(video_id)
        if curve is None:
            continue
        videos_per_game[game_id].add(video_id)
        for month, value in curve.series.items():
            raw[(game_id, month)] += value
            if curve.method == METHOD_A:
                from_a[(game_id, month)] += value

    comment_counts = session.execute(
        select(Comment.video_id, func.count()).group_by(Comment.video_id)
    ).all()
    comments_by_video = dict(comment_counts)

    peaks: dict[str, float] = defaultdict(float)
    for (game_id, _), value in raw.items():
        peaks[game_id] = max(peaks[game_id], value)

    written = 0
    for (game_id, month), value in raw.items():
        year, month_number = (int(p) for p in month.split("-"))
        row = session.get(GameMonth, (study, game_id, year, month_number))
        if row is None:
            row = GameMonth(study=study, game_id=game_id, year=year, month=month_number)
            session.add(row)
        row.attention_raw = value
        # Rescaled to 0-100 per game, like Google Trends, so the two can be compared.
        row.attention_index = 100.0 * value / peaks[game_id] if peaks[game_id] else 0.0
        row.share_method_a = (from_a[(game_id, month)] / value) if value else 0.0
        row.n_comments_dated = sum(
            comments_by_video.get(v, 0) for v in videos_per_game[game_id]
        )
        row.sample_basis = len(videos_per_game[game_id])
        written += 1

    session.commit()
    return written


def month_span(curves: Iterable[VideoCurve]) -> list[str]:
    """Every month covered by a set of curves, for reports."""
    months = sorted({m for c in curves for m in c.series})
    return month_range(months[0], months[-1]) if months else []
