"""The read-only web view (outside SPEC section 13, added on request).

The property that matters most: it must never write and never reach the API.
"""

from __future__ import annotations

import os
import re
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
        # Readable without JavaScript: every figure is already in the markup.
        head = body.split('<script id="state"')[0]
        assert "39" in head
        assert "1 600" in head  # the slice total, grouped the French way

        # The only placeholders left are the chart canvases, which a script draws.
        placeholders = re.findall(r"<([a-z]+)([^>]*)>—<", head)
        for tag, attrs in placeholders:
            assert 'class="canvas"' in attrs or "data-chart" in attrs, (tag, attrs)

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


class TestRuntimeDirectories:
    """Regression: the installed package must still find config/ and data/.

    In the container the package lives in site-packages while `config/` and
    `data/external/` are copied to the working directory. Resolving them against
    the package pointed at site-packages and crashed every command at runtime.
    """

    def test_the_config_directory_is_found_from_the_working_directory(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        root = tmp_path  # type: ignore[assignment]
        (root / "config" / "studies").mkdir(parents=True)  # type: ignore[operator]
        (root / "config" / "settings.yml").write_text("quota: {}\n")  # type: ignore[operator]
        (root / "data" / "external").mkdir(parents=True)  # type: ignore[operator]
        monkeypatch.chdir(root)  # type: ignore[arg-type]

        import ytcollector.config as config_module

        reloaded = importlib.reload(config_module)
        try:
            assert reloaded.CONFIG_DIR == root / "config"  # type: ignore[operator]
            assert reloaded.DATA_DIR == root / "data" / "external"  # type: ignore[operator]
        finally:
            monkeypatch.undo()
            importlib.reload(config_module)

    def test_an_explicit_override_wins(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        monkeypatch.setenv("YTC_CONFIG_DIR", str(tmp_path))
        import ytcollector.config as config_module

        reloaded = importlib.reload(config_module)
        try:
            assert reloaded.CONFIG_DIR == tmp_path
        finally:
            monkeypatch.undo()
            importlib.reload(config_module)


class TestExports:
    def test_every_export_downloads_as_csv(self, client: TestClient) -> None:
        for name in ("games", "twitch_game_month", "yt_game_month", "joined"):
            response = client.get(f"/export/{name}.csv")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith("text/csv")
            assert f"{name}.csv" in response.headers["content-disposition"]

    def test_games_export_has_a_row_per_entry(self, client: TestClient) -> None:
        body = client.get("/export/games.csv").text
        lines = [line for line in body.splitlines() if line.strip()]
        assert len(lines) == 44  # 43 games plus the header
        assert lines[0].startswith("game_id,")

    def test_twitch_export_is_one_row_per_game_month(self, client: TestClient) -> None:
        import csv
        import io as _io

        rows = list(csv.DictReader(_io.StringIO(client.get("/export/twitch_game_month.csv").text)))
        keys = [(r["game_id"], r["year"], r["month"]) for r in rows]
        assert len(keys) == len(set(keys))

    def test_an_unknown_export_is_not_found(self, client: TestClient) -> None:
        assert client.get("/export/secrets.csv").status_code == 404

    def test_the_joined_export_keeps_the_twitch_side_when_youtube_is_empty(
        self, client: TestClient
    ) -> None:
        # Before any collection there is no attention; the join must not come back
        # empty, or the page would offer a file with nothing in it.
        body = client.get("/export/joined.csv").text
        assert len(body.splitlines()) > 100
