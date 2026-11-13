"""YouTube API client (SPEC sections 2 and 8, milestone 2).

Every request is mocked: the real search quota is 100 calls a day and one careless
test burns the day.
"""

from __future__ import annotations

import gzip
import json

import pytest
import responses
from sqlalchemy import select

from ytcollector.client import API_ROOT, ApiError, QuotaExceededError, YouTubeClient
from ytcollector.models import RawResponse
from ytcollector.quota import QuotaExhausted, QuotaLimits, QuotaTracker

KEY = "TEST-KEY-NEVER-REAL"


@pytest.fixture
def tracker(session: object) -> QuotaTracker:
    limits = QuotaLimits(search_calls_per_day=100, units_per_day=10_000, safety_margin=0.0)
    return QuotaTracker(session, limits)  # type: ignore[arg-type]


@pytest.fixture
def client(session: object, tracker: QuotaTracker) -> YouTubeClient:
    # backoff_factor=0 keeps the retry tests instant; the delay itself is not under test.
    return YouTubeClient(
        session=session, tracker=tracker, api_key=KEY, backoff_factor=0.0
    )  # type: ignore[arg-type]


def stub(endpoint: str, body: dict, status: int = 200) -> None:
    responses.add(responses.GET, f"{API_ROOT}/{endpoint}", json=body, status=status)


def quota_error(reason: str = "quotaExceeded") -> dict:
    return {"error": {"code": 403, "message": "quota", "errors": [{"reason": reason}]}}


class TestQuotaEnforcement:
    @responses.activate
    def test_counts_each_search_call(self, client: YouTubeClient, tracker: QuotaTracker) -> None:
        stub("search", {"items": []})
        client.search(q="fortnite")
        assert tracker.remaining_search_calls() == 99

    @responses.activate
    def test_the_101st_search_is_never_sent(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        for _ in range(100):
            stub("search", {"items": []})
            client.search(q="fortnite")
        assert tracker.remaining_search_calls() == 0

        before = len(responses.calls)
        with pytest.raises(QuotaExhausted):
            client.search(q="fortnite")
        # The point: no request left the process.
        assert len(responses.calls) == before

    @responses.activate
    def test_other_endpoints_spend_units_not_search_calls(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        stub("videos", {"items": []})
        client.videos(ids=["a", "b"])
        assert tracker.remaining_search_calls() == 100
        assert tracker.remaining_units() == 9_999

    @responses.activate
    def test_a_failed_call_still_counts(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        # The request was emitted, so Google counted it whatever came back.
        stub("search", {"error": {"code": 404}}, status=404)
        with pytest.raises(ApiError):
            client.search(q="fortnite")
        assert tracker.remaining_search_calls() == 99


class TestQuotaExceeded:
    @responses.activate
    def test_403_quota_exceeded_stops_the_day(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        stub("search", quota_error(), status=403)
        with pytest.raises(QuotaExceededError):
            client.search(q="fortnite")
        assert not tracker.can_search()

    @responses.activate
    def test_rate_limit_exceeded_is_treated_the_same(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        stub("search", quota_error("rateLimitExceeded"), status=403)
        with pytest.raises(QuotaExceededError):
            client.search(q="fortnite")
        assert not tracker.can_search()

    @responses.activate
    def test_quota_exceeded_is_not_retried(self, client: YouTubeClient) -> None:
        stub("search", quota_error(), status=403)
        with pytest.raises(QuotaExceededError):
            client.search(q="fortnite")
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_403_for_another_reason_does_not_stop_the_day(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        stub("commentThreads", quota_error("commentsDisabled"), status=403)
        with pytest.raises(ApiError):
            client.comment_threads(video_id="abc")
        assert tracker.can_search()


class TestRetries:
    @responses.activate
    def test_retries_server_errors_then_succeeds(self, client: YouTubeClient) -> None:
        stub("videos", {"error": {}}, status=503)
        stub("videos", {"error": {}}, status=500)
        stub("videos", {"items": [{"id": "x"}]})
        assert client.videos(ids=["x"])["items"] == [{"id": "x"}]
        assert len(responses.calls) == 3

    @responses.activate
    def test_gives_up_after_the_attempt_budget(self, client: YouTubeClient) -> None:
        for _ in range(10):
            stub("videos", {"error": {}}, status=500)
        with pytest.raises(ApiError):
            client.videos(ids=["x"])
        assert len(responses.calls) == client.max_retries

    @responses.activate
    def test_every_attempt_is_counted(
        self, client: YouTubeClient, tracker: QuotaTracker
    ) -> None:
        stub("videos", {"error": {}}, status=503)
        stub("videos", {"items": []})
        client.videos(ids=["x"])
        # Two requests reached Google, so two units were spent.
        assert tracker.remaining_units() == 9_998

    @responses.activate
    def test_client_errors_are_not_retried(self, client: YouTubeClient) -> None:
        stub("videos", {"error": {"code": 400}}, status=400)
        with pytest.raises(ApiError):
            client.videos(ids=["x"])
        assert len(responses.calls) == 1


class TestKeyRedaction:
    @responses.activate
    def test_the_key_is_sent_but_never_stored(
        self, client: YouTubeClient, session: object
    ) -> None:
        stub("search", {"items": []})
        client.search(q="fortnite")

        assert KEY in responses.calls[0].request.url  # it must reach Google

        rows = list(session.scalars(select(RawResponse)))  # type: ignore[attr-defined]
        assert rows, "the response should have been cached"
        for row in rows:
            assert KEY not in json.dumps(row.params)
            assert "key" not in row.params
            assert KEY not in gzip.decompress(row.body_gzip).decode()

    @responses.activate
    def test_the_key_never_reaches_an_error_message(self, client: YouTubeClient) -> None:
        stub("search", {"error": {"code": 400}}, status=400)
        with pytest.raises(ApiError) as caught:
            client.search(q="fortnite")
        assert KEY not in str(caught.value)
        assert KEY not in repr(caught.value)

    @responses.activate
    def test_the_key_never_reaches_the_logs(
        self, client: YouTubeClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        caplog.set_level(logging.DEBUG, logger="ytcollector")
        stub("search", {"items": []})
        client.search(q="fortnite")
        assert KEY not in caplog.text

    @responses.activate
    def test_the_request_hash_ignores_the_key(self, session: object, tracker: QuotaTracker) -> None:
        # Rotating the key must not invalidate the whole cache.
        stub("videos", {"items": []})
        stub("videos", {"items": []})
        first = YouTubeClient(session=session, tracker=tracker, api_key="KEY-ONE")  # type: ignore[arg-type]
        second = YouTubeClient(session=session, tracker=tracker, api_key="KEY-TWO")  # type: ignore[arg-type]
        first.videos(ids=["x"])
        second.videos(ids=["x"])
        assert session.scalar(  # type: ignore[attr-defined]
            select(__import__("sqlalchemy").func.count()).select_from(RawResponse)
        ) == 1


class TestRawCache:
    @responses.activate
    def test_stores_the_body_for_offline_rebuilds(
        self, client: YouTubeClient, session: object
    ) -> None:
        stub("videos", {"items": [{"id": "x"}]})
        client.videos(ids=["x"])
        row = session.scalar(select(RawResponse))  # type: ignore[attr-defined]
        assert json.loads(gzip.decompress(row.body_gzip)) == {"items": [{"id": "x"}]}
        assert row.endpoint == "videos"
        assert row.status_code == 200
