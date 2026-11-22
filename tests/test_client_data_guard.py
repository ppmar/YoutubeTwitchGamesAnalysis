"""Milestone G0 of SPEC_GENRES: client data must never reach the public repo.

The two CSV exports in ``data/client/`` are held under a confidentiality
agreement. The repo is public and the image is built from the working tree, so
the guard has to hold in git and in Docker alike. These tests check the rules,
not the files: they pass whether or not the client data is present locally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _is_git_repo() -> bool:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:  # pragma: no cover - git missing from the environment
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


requires_git = pytest.mark.skipif(not _is_git_repo(), reason="not a git work tree")


def _check_ignore(relative: str) -> int:
    return subprocess.run(
        ["git", "check-ignore", "-q", relative], cwd=ROOT, capture_output=True
    ).returncode


@requires_git
@pytest.mark.parametrize(
    "relative",
    [
        "data/client/steam_tags.csv",
        "data/client/steam_reference.csv",
        # Ignoring the directory has to cover anything derived underneath it.
        "data/client/sous/dossier/x.parquet",
    ],
)
def test_git_ignores_client_data(relative: str) -> None:
    assert _check_ignore(relative) == 0, f"{relative} is not ignored by git"


@requires_git
def test_no_client_data_is_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "data/client"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    assert tracked == "", f"client data is tracked: {tracked}"


@requires_git
def test_no_client_data_in_the_history() -> None:
    """If this ever fails, do not rewrite anything: the repo is public."""
    past = subprocess.run(
        ["git", "log", "--all", "--oneline", "--", "data/client"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert past == "", f"client data appears in the history: {past}"


def test_dockerignore_excludes_client_data() -> None:
    lines = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "data/client/" in lines or "data/client" in lines
