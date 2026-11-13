"""The read-only web view (outside SPEC section 13, added on request).

The property that matters most: it must never write and never reach the API.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import responses
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ytcollector.config import load_study
from ytcollector.db import create_all
from ytcollector.games import prepare_games
from ytcollector.models import Game, GameVideo, VideoSnapshot
from ytcollector.quota import PACIFIC
from ytcollector.web import app as web


@pytest.fixture
def client(tmp_path: object, monkeypatch: pytest.MonkeyPatch, twitch_csv: object) -> TestClient:
    url = f"sqlite:///{tmp_path}/web.db"  # type: ignore[str-bytes-safe]
    engine = create_engine(url, future=True)
    create_all(engine)
    with sessionmaker(bind=engine, future=True)() as session:
        prepare_games(session, load_study("battle_royale"), csv_path=twitch_csv)  # type: ignore[arg-type]
        session.add(GameVideo(study="battle_royale", game_id="fortnite", video_id="v1",
                              period_start="2020-01-01", source="search"))
        session.add(VideoSnapshot(
            video_id="v1", collected_at=datetime.now(UTC), channel_id="UC1",
            published_at=datetime(2020, 1, 5, tzinfo=UTC),
            view_count=1000, title_match=True, video_type="gameplay",
        ))
        session.commit()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("YTC_STUDY", "battle_royale")
    web.twitch_frame.cache_clear()
    return TestClient(web.app)


class TestEndpoints:
    def test_healthz_is_cheap_and_green(self, client: TestClient) -> None:
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_the_page_renders_with_its_figures_already_in_the_html(
        self, client: TestClient
    ) -> None:
        body = client.get("/").text
        assert body.startswith("<!doctype html>")
        # Readable without JavaScript: every figure is already in the markup, so
        # no element is left holding only its em-dash placeholder.
        head = body.split('<script id="state"')[0]
        assert ">—<" not in head
        assert "39" in head
        assert "1 600" in head  # the slice total, grouped the French way

    def test_state_reports_the_collection(self, client: TestClient) -> None:
        state = client.get("/api/state").json()
        assert state["counts"]["games"] == 39
        assert state["counts"]["gamesTotal"] == 43
        assert state["counts"]["videos"] == 1
        assert state["counts"]["matched"] == 1
        assert state["progress"]["totalSlices"] > 1000
        assert state["progress"]["daysRemaining"] >= 1

    def test_state_carries_the_twitch_series(self, client: TestClient) -> None:
        series = client.get("/api/state").json()["series"]
        fortnite = next(s for s in series if s["game_id"] == "fortnite")
        assert fortnite["leader"] is True
        assert len(fortnite["points"]) == 87

    def test_attention_is_empty_until_the_score_is_built(self, client: TestClient) -> None:
        assert client.get("/api/state").json()["attention"] == []


class TestItNeverSpends:
    @responses.activate
    def test_no_request_reaches_the_api(self, client: TestClient) -> None:
        # responses with no registered mock turns any outbound call into an error.
        client.get("/")
        client.get("/api/state")
        assert len(responses.calls) == 0

    def test_it_does_not_need_an_api_key(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        assert client.get("/api/state").status_code == 200

    def test_nothing_is_written(self, client: TestClient) -> None:
        engine = create_engine(os.environ["DATABASE_URL"], future=True)
        with sessionmaker(bind=engine, future=True)() as session:
            before = len(list(session.scalars(__import__("sqlalchemy").select(Game))))
        client.get("/api/state")
        with sessionmaker(bind=engine, future=True)() as session:
            after = len(list(session.scalars(__import__("sqlalchemy").select(Game))))
        assert before == after


class TestQuotaReset:
    def test_the_reset_is_the_next_pacific_midnight(self) -> None:
        moment = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)  # 01:00 Pacific
        reset = web.next_quota_reset(moment)
        local = reset.astimezone(PACIFIC)
        assert (local.hour, local.minute) == (0, 0)
        assert local.date().isoformat() == "2026-01-16"

    def test_it_is_always_in_the_future(self) -> None:
        assert web.next_quota_reset() > datetime.now(UTC)
