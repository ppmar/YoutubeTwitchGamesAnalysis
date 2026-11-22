"""``ytc genres`` at milestone G1: the two commands, on synthetic data only.

The sub-command must never reach the network and must never be reachable from
``ytc daily``; both are held here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses
import yaml
from typer.testing import CliRunner

pytest.importorskip("scipy", reason="the genre tools are the optional `genres` extra")

from tests.test_genres_matrix import write_client_data, write_families
from ytcollector.cli import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A client directory, a genres.yml pointing at it, and an output directory."""
    client = tmp_path / "client"
    write_client_data(client)
    config = tmp_path / "genres.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "client_dir": str(client),
                "output_dir": str(tmp_path / "out"),
                "matrix": {"min_total_votes": 100, "min_tag_share": 0.02},
                "expected": {},
                "selection": {"k_final": None},
            }
        ),
        encoding="utf-8",
    )
    return {
        "config": config,
        "client": client,
        "out": tmp_path / "out",
        "families": tmp_path / "tag_families.yml",
    }


class TestInitTagFamilies:
    def test_it_writes_a_draft_with_every_tag_in_other(self, workspace: dict[str, Path]) -> None:
        result = runner.invoke(
            app,
            ["genres", "init-tag-families",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 0, result.output
        assert "4 tags" in result.output

        written = yaml.safe_load(workspace["families"].read_text())
        assert set(written["tags"].values()) == {"other"}
        assert written["used_for_factorization"] == ["gameplay", "players"]

    def test_it_refuses_to_overwrite_a_classification(
        self, workspace: dict[str, Path]
    ) -> None:
        write_families(workspace["families"])
        result = runner.invoke(
            app,
            ["genres", "init-tag-families",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 1
        assert "--force" in result.output
        # The classification is untouched.
        assert yaml.safe_load(workspace["families"].read_text())["tags"]["Shooter"] == "gameplay"

    def test_force_replaces_it(self, workspace: dict[str, Path]) -> None:
        write_families(workspace["families"])
        result = runner.invoke(
            app,
            ["genres", "init-tag-families", "--force",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 0, result.output
        assert set(yaml.safe_load(workspace["families"].read_text())["tags"].values()) == {"other"}


class TestBuildMatrix:
    def test_it_writes_the_three_files(self, workspace: dict[str, Path]) -> None:
        write_families(workspace["families"])
        result = runner.invoke(
            app,
            ["genres", "build-matrix",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 0, result.output
        assert "3 games x 3 tags" in result.output
        for name in ("matrix.npz", "rows.parquet", "cols.parquet"):
            assert (workspace["out"] / name).is_file()

    def test_an_unclassified_tag_fails_with_the_list(self, workspace: dict[str, Path]) -> None:
        write_families(
            workspace["families"], mapping={"Battle Royale": "gameplay", "Shooter": "gameplay"}
        )
        result = runner.invoke(
            app,
            ["genres", "build-matrix",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 1
        assert "Farming" in result.output and "Pixel Graphics" in result.output
        assert not (workspace["out"] / "matrix.npz").exists()

    def test_missing_client_data_is_a_message_not_a_traceback(
        self, tmp_path: Path, workspace: dict[str, Path]
    ) -> None:
        config = tmp_path / "empty.yml"
        config.write_text(
            yaml.safe_dump(
                {"client_dir": str(tmp_path / "absent"), "output_dir": str(tmp_path / "out")}
            )
        )
        write_families(workspace["families"])
        result = runner.invoke(
            app,
            ["genres", "build-matrix",
             "--config", str(config),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "steam_tags.csv" in result.output

    def test_a_missing_families_file_names_the_command_that_writes_it(
        self, workspace: dict[str, Path]
    ) -> None:
        result = runner.invoke(
            app,
            ["genres", "build-matrix",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 1
        assert "init-tag-families" in result.output


class TestItStaysOutOfTheWay:
    @responses.activate
    def test_no_command_reaches_the_api(self, workspace: dict[str, Path]) -> None:
        # responses with nothing registered turns any outbound call into an error.
        write_families(workspace["families"])
        for argv in (
            ["genres", "init-tag-families", "--force",
             "--config", str(workspace["config"]), "--families", str(workspace["families"])],
            ["genres", "build-matrix",
             "--config", str(workspace["config"]), "--families", str(workspace["families"])],
        ):
            runner.invoke(app, argv)
        assert len(responses.calls) == 0

    def test_it_does_not_need_an_api_key_or_a_database(
        self, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        write_families(workspace["families"])
        result = runner.invoke(
            app,
            ["genres", "build-matrix",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 0, result.output

    def test_the_daily_run_never_touches_the_genre_code(self) -> None:
        """`ytc daily` runs on Railway, where the client data must not exist."""
        from pathlib import Path as _Path

        source = _Path("ytcollector/daily.py").read_text()
        assert "genres" not in source

    def test_the_commands_are_listed_under_their_own_group(self) -> None:
        listing = runner.invoke(app, ["genres", "--help"]).output
        assert "init-tag-families" in listing
        assert "build-matrix" in listing


def write_block_client_data(directory: Path, games: int = 24, tags: int = 6) -> dict[str, str]:
    """A client export big enough to factorise: two blocks of tags."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(4)
    rows = []
    for game in range(games):
        block = game % 2
        for tag in range(block * (tags // 2), block * (tags // 2) + tags // 2):
            rows.append((int(rng.integers(30, 300)), f"Tag {tag}", 500 + game))
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["nb", "tag", "appid"]).to_csv(
        directory / "steam_tags.csv", index=False
    )
    pd.DataFrame(
        {
            "appid": [500 + g for g in range(games)],
            "name": [f"Jeu {g}" for g in range(games)],
            "releaseDate": ["2020-01-01"] * games,
        }
    ).to_csv(directory / "steam_reference.csv", index=False)
    return {f"Tag {i}": "gameplay" for i in range(tags)}


class TestFit:
    @pytest.fixture
    def built(self, workspace: dict[str, Path]) -> dict[str, Path]:
        """A matrix on disk, which is what `fit` reads."""
        mapping = write_block_client_data(workspace["client"])
        write_families(workspace["families"], mapping=mapping)
        result = runner.invoke(
            app,
            ["genres", "build-matrix",
             "--config", str(workspace["config"]),
             "--families", str(workspace["families"])],
        )
        assert result.exit_code == 0, result.output
        return workspace

    def test_it_scores_every_k_of_the_grid(self, built: dict[str, Path]) -> None:
        import pandas as pd

        result = runner.invoke(
            app,
            ["genres", "fit", "--config", str(built["config"]),
             "--k", "2,3", "--seeds", "2", "--bootstrap", "2"],
        )
        assert result.exit_code == 0, result.output
        frame = pd.read_csv(built["out"] / "k_selection.csv")
        assert list(frame["k"]) == [2, 3]
        assert (built["out"] / "fits" / "k2_s0.npz").is_file()

    def test_it_suggests_the_most_stable_k_without_deciding(
        self, built: dict[str, Path]
    ) -> None:
        result = runner.invoke(
            app,
            ["genres", "fit", "--config", str(built["config"]),
             "--k", "2", "--seeds", "2", "--bootstrap", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "most stable" in result.output
        assert "before setting k_final" in result.output

    def test_a_grid_that_makes_no_sense_is_a_message(self, built: dict[str, Path]) -> None:
        result = runner.invoke(
            app, ["genres", "fit", "--config", str(built["config"]), "--k", "nonsense"]
        )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "k grid" in result.output

    def test_it_says_to_build_the_matrix_first(self, workspace: dict[str, Path]) -> None:
        result = runner.invoke(
            app, ["genres", "fit", "--config", str(workspace["config"]), "--k", "2"]
        )
        assert result.exit_code == 1
        assert "build-matrix" in result.output

    def test_a_k_wider_than_the_matrix_is_refused_with_the_limit(
        self, built: dict[str, Path]
    ) -> None:
        """nndsvda needs k <= min(rows, tags), and a subsample has fewer rows."""
        result = runner.invoke(
            app,
            ["genres", "fit", "--config", str(built["config"]),
             "--k", "40", "--seeds", "1", "--bootstrap", "1"],
        )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "k cannot exceed" in result.output

    @responses.activate
    def test_fitting_reaches_no_api(self, built: dict[str, Path]) -> None:
        runner.invoke(
            app,
            ["genres", "fit", "--config", str(built["config"]),
             "--k", "2", "--seeds", "1", "--bootstrap", "1"],
        )
        assert len(responses.calls) == 0
