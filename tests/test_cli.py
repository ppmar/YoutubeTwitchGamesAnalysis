"""Milestone 0: the CLI surface exists and exposes every documented command."""

from typer.testing import CliRunner

from ytcollector.cli import app

runner = CliRunner()

EXPECTED_COMMANDS = {
    "prepare-games",
    "plan",
    "search",
    "videos",
    "channels",
    "comments",
    "channel-panel",
    "refresh-stats",
    "build-tables",
    "build-attention",
    "export",
    "status",
    "daily",
}


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in EXPECTED_COMMANDS:
        assert command in result.output
