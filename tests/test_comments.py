"""Comment collection (SPEC section 5 step 4, milestone 4).

Privacy is a hard requirement here: no author, no author id, no text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import responses
from sqlalchemy import func, select

from ytcollector.client import API_ROOT, YouTubeClient
from ytcollector.models import (
    Comment,
    Game,
    GameVideo,
    VideoCommentState,
    VideoSnapshot,
)
from ytcollector.quota import QuotaLimits, QuotaTracker
from ytcollector.stages import comments as stage
from ytcollector.tasks import TaskQueue

NOW = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "TEST-KEY"


@pytest.fixture
def tracker(session: object) -> QuotaTracker:
    return QuotaTracker(session, QuotaLimits(100, 10_000, 0.0))  # type: ignore[arg-type]


@pytest.fixture
def client(session: object, tracker: QuotaTracker) -> YouTubeClient:
    return YouTubeClient(session=session, tracker=tracker, api_key=KEY, backoff_factor=0.0)  # type: ignore[arg-type]


def add_video(session: object, video_id: str, game_id: str = "apex", comments: int = 100,
              views: int = 1000, match: bool = True) -> None:
    session.add(GameVideo(study="s", game_id=game_id, video_id=video_id,  # type: ignore[attr-defined]
                          period_start="2020-01-01", source="search"))
    session.add(VideoSnapshot(video_id=video_id, collected_at=NOW, channel_id="UC1",  # type: ignore[attr-defined]
                              comment_count=comments, view_count=views, title_match=match))


@pytest.fixture
def seeded(session: object) -> object:
    for game_id in ("apex", "pubg"):
        session.add(Game(study="s", game_id=game_id, game_name=game_id,  # type: ignore[attr-defined]
                         twitch_names=[game_id], yt_query=game_id, match_terms=[game_id],
                         role="leader", enabled=True))
    session.commit()  # type: ignore[attr-defined]
    return session


def thread(comment_id: str, published: str, likes: int = 1, replies: int = 0) -> dict:
    return {
        "id": comment_id,
        "snippet": {
            "totalReplyCount": replies,
            "topLevelComment": {
                "snippet": {
                    "publishedAt": published,
                    "likeCount": likes,
                    # Fields the collector must never keep:
                    "authorDisplayName": "Some Person",
                    "authorChannelId": {"value": "UCsecret"},
                    "textOriginal": "a comment body",
                },
            },
        },
    }


def page(items: list[dict], next_token: str | None = None) -> dict:
    body: dict = {"items": items}
    if next_token:
        body["nextPageToken"] = next_token
    return body


class TestDataMinimisation:
    def test_the_table_has_no_author_or_text_column(self) -> None:
        columns = {c.name for c in Comment.__table__.columns}
        assert columns == {
            "video_id", "comment_id_hash", "comment_published_at",
            "like_count", "reply_count", "collected_at",
        }

    @responses.activate
    def test_nothing_identifying_is_stored(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                      json=page([thread("c1", "2020-02-01T00:00:00Z")]))

        stage.run(seeded, client, "s")  # type: ignore[arg-type]

        row = seeded.scalar(select(Comment))  # type: ignore[attr-defined]
        stored = {c.name: getattr(row, c.name) for c in Comment.__table__.columns}
        blob = repr(stored)
        assert "Some Person" not in blob
        assert "UCsecret" not in blob
        assert "a comment body" not in blob

    def test_the_comment_id_is_hashed_not_kept(self) -> None:
        digest = stage.hash_comment_id("UgzABC123")
        assert digest != "UgzABC123"
        assert len(digest) == 64
        assert stage.hash_comment_id("UgzABC123") == digest


class TestEligibility:
    def test_skips_videos_below_the_comment_threshold(self, seeded: object) -> None:
        add_video(seeded, "many", comments=100)
        add_video(seeded, "few", comments=5)
        seeded.commit()  # type: ignore[attr-defined]
        assert [v[1] for v in stage.eligible_videos(seeded, "s", min_comments=30)] == ["many"]  # type: ignore[arg-type]

    def test_skips_videos_that_do_not_match_the_game(self, seeded: object) -> None:
        add_video(seeded, "yes", match=True)
        add_video(seeded, "no", match=False)
        seeded.commit()  # type: ignore[attr-defined]
        assert [v[1] for v in stage.eligible_videos(seeded, "s")] == ["yes"]  # type: ignore[arg-type]

    def test_orders_by_views_inside_a_game(self, seeded: object) -> None:
        add_video(seeded, "small", views=10)
        add_video(seeded, "big", views=1_000_000)
        seeded.commit()  # type: ignore[attr-defined]
        assert [v[1] for v in stage.eligible_videos(seeded, "s")] == ["big", "small"]  # type: ignore[arg-type]

    def test_alternates_between_games(self, seeded: object) -> None:
        add_video(seeded, "a1", game_id="apex", views=100)
        add_video(seeded, "a2", game_id="apex", views=90)
        add_video(seeded, "p1", game_id="pubg", views=50)
        seeded.commit()  # type: ignore[attr-defined]
        # No game may monopolise the budget.
        assert [v[1] for v in stage.eligible_videos(seeded, "s")] == ["a1", "p1", "a2"]  # type: ignore[arg-type]


class TestPagination:
    @responses.activate
    def test_follows_every_page(self, seeded: object, client: YouTubeClient) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                      json=page([thread("c1", "2020-02-01T00:00:00Z")], "TOKEN2"))
        responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                      json=page([thread("c2", "2020-01-01T00:00:00Z")]))

        result = stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert result.items == 2
        assert len(responses.calls) == 2

    @responses.activate
    def test_the_cap_marks_the_video_truncated(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        for _ in range(5):
            responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                          json=page([thread("c", "2020-02-01T00:00:00Z")], "MORE"))

        # 200 threads = 2 pages, then stop.
        stage.run(seeded, client, "s", max_threads_per_video=200)  # type: ignore[arg-type]

        state = seeded.get(VideoCommentState, "v1")  # type: ignore[attr-defined]
        assert state.truncated is True
        assert len(responses.calls) == 2

    @responses.activate
    def test_a_resumed_video_sends_its_page_token(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        queue = TaskQueue(seeded, "s")  # type: ignore[arg-type]
        stage.enqueue(queue, stage.eligible_videos(seeded, "s"))  # type: ignore[arg-type]
        task = queue.claim(stage.KIND)
        queue.pause(task, cursor="RESUME_HERE")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                      json=page([thread("c1", "2020-02-01T00:00:00Z")]))

        stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert "pageToken=RESUME_HERE" in responses.calls[0].request.url


class TestFailureModes:
    @responses.activate
    def test_disabled_comments_mark_the_video_and_continue(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1", game_id="apex")
        add_video(seeded, "v2", game_id="pubg")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", status=403, json={
            "error": {"code": 403, "message": "disabled",
                      "errors": [{"reason": "commentsDisabled"}]}})
        responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                      json=page([thread("c1", "2020-02-01T00:00:00Z")]))

        result = stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert result.processed == 2
        assert result.errors == 0
        flagged = [  # type: ignore[attr-defined]
            st for st in seeded.scalars(select(VideoCommentState)) if st.disabled
        ]
        assert len(flagged) == 1

    @responses.activate
    def test_the_unit_reserve_is_respected(
        self, seeded: object, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", json=page([]))
        # 9500 spent of 10000: the 1000-unit reserve is already breached.
        tracker.record_units(9_500)

        result = stage.run(seeded, client, "s", reserve=1_000)  # type: ignore[arg-type]

        assert result.stopped_on_quota
        assert len(responses.calls) == 0

    @responses.activate
    def test_dry_run_emits_no_request(self, seeded: object, client: YouTubeClient) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        result = stage.run(seeded, client, "s", dry_run=True)  # type: ignore[arg-type]
        assert result.processed == 1
        assert len(responses.calls) == 0


class TestMonthlyCounts:
    @responses.activate
    def test_counts_comments_per_month(self, seeded: object, client: YouTubeClient) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", json=page([
            thread("c1", "2020-01-05T00:00:00Z"),
            thread("c2", "2020-01-20T00:00:00Z"),
            thread("c3", "2020-02-02T00:00:00Z"),
        ]))

        stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert stage.monthly_counts(seeded, "v1") == {"2020-01": 2, "2020-02": 1}  # type: ignore[arg-type]

    @responses.activate
    def test_duplicate_comments_are_stored_once(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", json=page([
            thread("c1", "2020-01-05T00:00:00Z"),
            thread("c1", "2020-01-05T00:00:00Z"),
        ]))

        stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert len(list(seeded.scalars(select(Comment)))) == 1  # type: ignore[attr-defined]


class TestMemoryAndQueries:
    """Regression: the container was killed partway through a popular video.

    Storing each comment with `session.merge` issued one SELECT per row and kept
    every object in the identity map, so memory grew with the comment count until
    the process died. Pages are now bulk-inserted and released.
    """

    @responses.activate
    def test_the_session_does_not_grow_with_the_comment_count(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        for page_index in range(5):
            items = [
                thread(f"c{page_index}-{i}", "2020-02-01T00:00:00Z") for i in range(100)
            ]
            responses.add(responses.GET, f"{API_ROOT}/commentThreads",
                          json=page(items, "NEXT" if page_index < 4 else None))

        stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert seeded.scalar(  # type: ignore[attr-defined]
            select(func.count()).select_from(Comment)
        ) == 500
        # 500 rows stored, but nothing like 500 objects kept around.
        assert len(seeded.identity_map) < 50  # type: ignore[attr-defined]

    @responses.activate
    def test_a_page_is_one_statement_not_one_per_comment(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        from sqlalchemy import event

        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", json=page(
            [thread(f"c{i}", "2020-02-01T00:00:00Z") for i in range(100)]))

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            if "comments" in statement.lower():
                statements.append(statement)

        engine = seeded.get_bind()  # type: ignore[attr-defined]
        event.listen(engine, "before_cursor_execute", record)
        try:
            stage.run(seeded, client, "s")  # type: ignore[arg-type]
        finally:
            event.remove(engine, "before_cursor_execute", record)

        inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT")]
        assert len(inserts) == 1, f"expected one bulk insert, got {len(inserts)}"

    @responses.activate
    def test_repeated_ids_inside_one_page_are_stored_once(
        self, seeded: object, client: YouTubeClient
    ) -> None:
        add_video(seeded, "v1")
        seeded.commit()  # type: ignore[attr-defined]
        responses.add(responses.GET, f"{API_ROOT}/commentThreads", json=page([
            thread("same", "2020-02-01T00:00:00Z"),
            thread("same", "2020-02-01T00:00:00Z"),
            thread("other", "2020-03-01T00:00:00Z"),
        ]))

        stage.run(seeded, client, "s")  # type: ignore[arg-type]

        assert seeded.scalar(  # type: ignore[attr-defined]
            select(func.count()).select_from(Comment)
        ) == 2
