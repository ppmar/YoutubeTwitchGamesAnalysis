"""The matrix tab of the web view, and the guard that keeps it local.

The matrix is derived from client data held under a confidentiality agreement,
so the first property tested is that the endpoint serves nothing until the
operator opts in. Nothing here reads ``data/client/``: the matrix is synthetic,
two blocks of tags drawn with a fixed seed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ytcollector.web import matrix_view

GAMES = 12
TAGS = 8


@pytest.fixture
def built_matrix(tmp_path: Path) -> Path:
    """A block-structured matrix written the way milestone G1 will write it."""
    pytest.importorskip("scipy")
    import numpy as np
    import pandas as pd
    from scipy import sparse

    rng = np.random.default_rng(7)
    dense = np.zeros((GAMES, TAGS))
    for i in range(GAMES):
        columns = range(4) if i % 2 == 0 else range(4, TAGS)
        for j in columns:
            dense[i, j] = rng.random() + 0.2
    dense /= np.linalg.norm(dense, axis=1, keepdims=True)

    sparse.save_npz(tmp_path / "matrix.npz", sparse.csr_matrix(dense))
    pd.DataFrame(
        {
            "appid": [100 + i for i in range(GAMES)],
            "name": [f"Jeu {i}" for i in range(GAMES)],
            "release_date": ["2020-01-01"] * GAMES,
            "total_votes": [1000 - 10 * i for i in range(GAMES)],
        }
    ).to_parquet(tmp_path / "rows.parquet")
    pd.DataFrame(
        {
            "tag": [f"Tag {j}" for j in range(TAGS)],
            "family": ["gameplay"] * 4 + ["players"] * 4,
            "df": [GAMES // 2] * TAGS,
            "idf": [1.5] * TAGS,
        }
    ).to_parquet(tmp_path / "cols.parquet")
    return tmp_path


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch, built_matrix: Path) -> Path:
    monkeypatch.setenv("YTC_GENRES_LOCAL", "1")
    monkeypatch.setenv("YTC_GENRES_DIR", str(built_matrix))
    return built_matrix


class TestTheGuard:
    def test_nothing_is_served_without_the_opt_in(
        self, monkeypatch: pytest.MonkeyPatch, built_matrix: Path
    ) -> None:
        monkeypatch.delenv("YTC_GENRES_LOCAL", raising=False)
        monkeypatch.setenv("YTC_GENRES_DIR", str(built_matrix))
        assert matrix_view.read_submatrix() == {"available": False, "reason": "disabled"}

    def test_the_endpoint_is_quiet_rather_than_broken_without_the_opt_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YTC_GENRES_LOCAL", raising=False)
        payload = client.get("/api/genres/matrix").json()
        assert payload == {"available": False, "reason": "disabled"}

    def test_a_missing_build_names_what_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("YTC_GENRES_LOCAL", "1")
        monkeypatch.setenv("YTC_GENRES_DIR", str(tmp_path / "nothing-here"))
        payload = matrix_view.read_submatrix()
        assert payload["available"] is False
        assert payload["reason"] == "missing"
        assert set(payload["missing"]) == {"matrix.npz", "rows.parquet", "cols.parquet"}

    def test_an_unreadable_file_does_not_take_the_page_down(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for name in ("matrix.npz", "rows.parquet", "cols.parquet"):
            (tmp_path / name).write_bytes(b"not a matrix")
        monkeypatch.setenv("YTC_GENRES_LOCAL", "1")
        monkeypatch.setenv("YTC_GENRES_DIR", str(tmp_path))
        response = client.get("/api/genres/matrix")
        assert response.status_code == 200
        assert response.json()["available"] is False


class TestTheSubmatrix:
    def test_it_returns_the_heaviest_games_first(self, local: Path) -> None:
        payload = matrix_view.read_submatrix(games=5, tags=6)
        assert [g["name"] for g in payload["games"]] == [f"Jeu {i}" for i in range(5)]
        assert [g["totalVotes"] for g in payload["games"]] == [1000, 990, 980, 970, 960]

    def test_it_is_capped_by_the_request(self, local: Path) -> None:
        payload = matrix_view.read_submatrix(games=5, tags=6)
        assert len(payload["games"]) == 5
        assert len(payload["tags"]) == 6
        assert all(0 <= cell[0] < 5 and 0 <= cell[1] < 6 for cell in payload["cells"])

    def test_cells_carry_the_matrix_values(self, local: Path) -> None:
        from scipy import sparse

        full = sparse.load_npz(local / "matrix.npz").toarray()
        payload = matrix_view.read_submatrix(games=GAMES, tags=TAGS)
        appids = [g["appid"] for g in payload["games"]]
        tags = [t["tag"] for t in payload["tags"]]
        for row, col, value in payload["cells"]:
            i = appids[row] - 100
            j = int(tags[col].split()[1])
            assert value == pytest.approx(full[i, j], abs=5e-4)
        assert payload["scale"] == pytest.approx(full.max(), abs=5e-4)

    def test_every_row_of_the_source_is_normalised(self, local: Path) -> None:
        # Not a property of this view, but of what G1 writes; the view reports a
        # scale and the page claims rows are comparable, so it is worth holding.
        import numpy as np
        from scipy import sparse

        full = sparse.load_npz(local / "matrix.npz").toarray()
        assert np.allclose(np.linalg.norm(full, axis=1), 1.0)

    def test_a_tag_filter_narrows_the_columns(self, local: Path) -> None:
        payload = matrix_view.read_submatrix(tag_query="tag 3")
        assert [t["tag"] for t in payload["tags"]] == ["Tag 3"]

    def test_a_filter_matching_nothing_is_empty_not_an_error(self, local: Path) -> None:
        payload = matrix_view.read_submatrix(tag_query="zzz")
        assert payload["available"] is True
        assert payload["tags"] == [] and payload["cells"] == []

    def test_a_focused_game_is_kept_even_when_it_is_light(self, local: Path) -> None:
        payload = matrix_view.read_submatrix(games=2, tags=TAGS, appid=100 + GAMES - 1)
        assert payload["games"][0]["appid"] == 100 + GAMES - 1

    def test_the_source_summary_describes_the_whole_matrix(self, local: Path) -> None:
        source = matrix_view.read_submatrix(games=3, tags=3)["source"]
        assert source["games"] == GAMES
        assert source["tags"] == TAGS
        assert source["tagsPerGame"] == pytest.approx(4.0)
        assert source["families"] == {"gameplay": 4, "players": 4}

    def test_the_endpoint_passes_its_parameters_through(
        self, client: TestClient, local: Path
    ) -> None:
        payload = client.get("/api/genres/matrix?games=4&tags=3&tag=tag").json()
        assert payload["available"] is True
        assert len(payload["games"]) == 4
        assert len(payload["tags"]) == 3
        assert payload["query"] == "tag"

    def test_a_silly_size_is_clamped_not_honoured(self, client: TestClient, local: Path) -> None:
        payload = client.get("/api/genres/matrix?games=100000&tags=100000").json()
        assert len(payload["games"]) == GAMES  # the whole synthetic matrix, no more
        assert len(payload["tags"]) == TAGS

    def test_a_size_that_is_not_a_number_is_refused_cleanly(
        self, client: TestClient, local: Path
    ) -> None:
        assert client.get("/api/genres/matrix?tags=abc").status_code == 422


class TestThePage:
    def test_the_state_says_whether_the_matrix_tab_can_load(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YTC_GENRES_LOCAL", raising=False)
        assert client.get("/api/state").json()["genresLocal"] is False
        monkeypatch.setenv("YTC_GENRES_LOCAL", "1")
        assert client.get("/api/state").json()["genresLocal"] is True

    def test_the_tabs_are_in_the_markup_and_hidden_until_the_script_runs(
        self, client: TestClient
    ) -> None:
        body = client.get("/").text
        # Hidden in the markup: without JavaScript the page stays one scroll.
        assert '<nav class="tabs" data-tabs hidden' in body
        for tab in ("t-collecte", "t-twitch", "t-youtube", "t-jeux", "t-matrice", "t-donnees"):
            assert f'id="{tab}"' in body

    def test_every_tab_points_at_a_panel_that_exists(self, client: TestClient) -> None:
        import re

        body = client.get("/").text
        controlled = re.findall(r'role="tab" id="t-[a-z]+" aria-controls="(p-[a-z]+)"', body)
        assert len(controlled) == 7
        for panel in controlled:
            assert f'class="panel" id="{panel}"' in body

    def test_no_panel_is_hidden_server_side(self, client: TestClient) -> None:
        """Readable without JavaScript: the script hides panels, not the server."""
        import re

        body = client.get("/").text
        for attrs in re.findall(r'<div class="panel"([^>]*)>', body):
            assert "hidden" not in attrs

    def test_the_matrix_tab_explains_itself_before_anything_is_built(
        self, client: TestClient
    ) -> None:
        body = client.get("/").text
        assert "YTC_GENRES_LOCAL=1" in body
        assert "data-matrix-empty" in body


def test_the_view_never_writes(local: Path) -> None:
    before = {p.name: p.stat().st_mtime_ns for p in local.iterdir()}
    matrix_view.read_submatrix(games=GAMES, tags=TAGS)
    after: dict[str, Any] = {p.name: p.stat().st_mtime_ns for p in local.iterdir()}
    assert before == after
