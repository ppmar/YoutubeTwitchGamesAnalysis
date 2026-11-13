"""YouTube Data API v3 client.

Three responsibilities beyond sending requests:

- **Pay before you send.** Every call is checked against the daily budget first, so
  a 101st search is never emitted (SPEC section 8). Every request that does leave is
  counted, successful or not — Google counted it too.
- **Never leak the key.** ``key`` is added at the last moment, and is stripped from
  anything stored, hashed, logged or raised (SPEC section 2).
- **Keep the raw body.** Responses are cached so `build-tables` can rebuild every
  analysis table without touching the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ytcollector.models import RawResponse
from ytcollector.quota import QuotaTracker

API_ROOT = "https://www.googleapis.com/youtube/v3"

logger = logging.getLogger("ytcollector.client")

# search.list has its own counter; the others are priced in units.
SEARCH_ENDPOINT = "search"
UNIT_COST = {
    "videos": 1,
    "channels": 1,
    "playlistItems": 1,
    "commentThreads": 1,
}

QUOTA_REASONS = {"quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"}


class ApiError(RuntimeError):
    """An API error whose message is safe to log: the key is never in it."""

    def __init__(self, endpoint: str, status: int, reason: str, message: str) -> None:
        super().__init__(f"{endpoint} failed with {status} ({reason}): {message}")
        self.endpoint = endpoint
        self.status = status
        self.reason = reason


class QuotaExceededError(ApiError):
    """The API itself says the budget is gone. Stop the day, do not retry."""


class YouTubeClient:
    """Thin, quota-aware wrapper over the endpoints the collector needs."""

    def __init__(
        self,
        session: Session,
        tracker: QuotaTracker,
        api_key: str,
        timeout: int = 30,
        max_retries: int = 5,
        backoff_factor: float = 2.0,
        http: requests.Session | None = None,
    ) -> None:
        self.session = session
        self.tracker = tracker
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.http = http or requests.Session()

    # Internals -------------------------------------------------------------

    @staticmethod
    def _request_hash(endpoint: str, params: dict[str, Any]) -> str:
        """Hash the request without the key, so rotating it keeps the cache valid."""
        canonical = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _cache(self, endpoint: str, params: dict[str, Any], status: int, body: dict) -> None:
        request_hash = self._request_hash(endpoint, params)
        if self.session.get(RawResponse, request_hash) is not None:
            return
        self.session.add(
            RawResponse(
                request_hash=request_hash,
                endpoint=endpoint,
                # `params` has no key in it, and must never gain one.
                params=params,
                status_code=status,
                body_gzip=gzip.compress(json.dumps(body).encode()),
            )
        )
        self.session.flush()

    def cached(self, endpoint: str, params: dict[str, Any]) -> dict | None:
        """Return a previously stored body, or None."""
        row = self.session.get(RawResponse, self._request_hash(endpoint, params))
        if row is None:
            return None
        return json.loads(gzip.decompress(row.body_gzip))

    @staticmethod
    def _reason(body: dict) -> str:
        errors = (body.get("error") or {}).get("errors") or [{}]
        return str(errors[0].get("reason", ""))

    def _charge(self, endpoint: str) -> None:
        """Count a request that is about to be, or has just been, emitted."""
        if endpoint == SEARCH_ENDPOINT:
            self.tracker.record_search_call()
        else:
            self.tracker.record_units(UNIT_COST.get(endpoint, 1))

    def _check_budget(self, endpoint: str, reserve: int = 0) -> None:
        if endpoint == SEARCH_ENDPOINT:
            self.tracker.check_search_call()
        else:
            self.tracker.check_units(UNIT_COST.get(endpoint, 1), reserve=reserve)

    def call(self, endpoint: str, params: dict[str, Any], reserve: int = 0) -> dict:
        """Send one request, honouring the budget, retries and the cache.

        ``params`` never contains the key: it is added to the query string here and
        nowhere else.
        """
        clean = {k: v for k, v in params.items() if v is not None and k != "key"}

        # Refuse before spending, not after.
        self._check_budget(endpoint, reserve=reserve)

        url = f"{API_ROOT}/{endpoint}"
        last_error: ApiError | None = None

        for attempt in range(1, self.max_retries + 1):
            query = urlencode({**clean, "key": self._api_key})
            logger.debug("GET %s/%s params=%s", API_ROOT, endpoint, clean)

            response = self.http.get(f"{url}?{query}", timeout=self.timeout)
            # The request reached Google, so it is paid for whatever comes back.
            self._charge(endpoint)

            try:
                body = response.json()
            except ValueError:
                body = {}

            if response.ok:
                self._cache(endpoint, clean, response.status_code, body)
                return body

            reason = self._reason(body)
            message = (body.get("error") or {}).get("message", "")

            if response.status_code == 403 and reason in QUOTA_REASONS:
                budget = "search" if endpoint == SEARCH_ENDPOINT else "units"
                self.tracker.mark_exhausted(budget)
                logger.warning("%s budget exhausted according to the API", budget)
                raise QuotaExceededError(endpoint, 403, reason, message)

            last_error = ApiError(endpoint, response.status_code, reason, message)

            # Only server-side trouble is worth repeating.
            if response.status_code < 500:
                raise last_error

            if attempt < self.max_retries:
                delay = self.backoff_factor ** (attempt - 1)
                logger.info("%s returned %s, retrying in %.1fs", endpoint, response.status_code, delay)
                time.sleep(delay)

        assert last_error is not None
        raise last_error

    # Endpoints -------------------------------------------------------------

    def search(
        self,
        q: str,
        published_after: str | None = None,
        published_before: str | None = None,
        page_token: str | None = None,
        max_results: int = 50,
        order: str = "viewCount",
        video_category_id: str | None = "20",
        region_code: str | None = None,
        relevance_language: str | None = None,
    ) -> dict:
        """One page of search.list. Each page costs one call from the search counter."""
        return self.call(
            SEARCH_ENDPOINT,
            {
                "part": "snippet",
                "type": "video",
                "q": q,
                "maxResults": max_results,
                "order": order,
                "videoCategoryId": video_category_id,
                "publishedAfter": published_after,
                "publishedBefore": published_before,
                "pageToken": page_token,
                "regionCode": region_code,
                "relevanceLanguage": relevance_language,
            },
        )

    def videos(self, ids: list[str]) -> dict:
        return self.call(
            "videos",
            {
                "part": "snippet,statistics,contentDetails,topicDetails,"
                "liveStreamingDetails,status",
                "id": ",".join(ids),
                "maxResults": 50,
            },
        )

    def channels(self, ids: list[str]) -> dict:
        return self.call(
            "channels",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(ids), "maxResults": 50},
        )

    def comment_threads(
        self,
        video_id: str,
        page_token: str | None = None,
        max_results: int = 100,
        order: str = "time",
        reserve: int = 0,
    ) -> dict:
        return self.call(
            "commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max_results,
                "order": order,
                "pageToken": page_token,
            },
            reserve=reserve,
        )

    def playlist_items(self, playlist_id: str, page_token: str | None = None) -> dict:
        return self.call(
            "playlistItems",
            {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
                "pageToken": page_token,
            },
        )

    def known_responses(self, endpoint: str | None = None) -> list[RawResponse]:
        query = select(RawResponse)
        if endpoint:
            query = query.where(RawResponse.endpoint == endpoint)
        return list(self.session.scalars(query))
