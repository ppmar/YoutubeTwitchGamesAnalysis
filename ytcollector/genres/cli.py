"""``ytc genres`` — SPEC_GENRES section 9.

No command here reaches the network, and none belongs to ``ytc daily``: they run
on a local machine, over client data held under a confidentiality agreement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="genres",
    help="Genres from the game x tag matrix (local only, no API call).",
    no_args_is_help=True,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Path to genres.yml (default: config/genres.yml)."),
]
FamiliesOption = Annotated[
    Path | None,
    typer.Option("--families", help="Path to tag_families.yml."),
]
IgnoreExpected = Annotated[
    bool,
    typer.Option(
        "--ignore-expected",
        help="Carry on when the export does not match the notebook's counts.",
    ),
]


def _logging() -> None:
    import logging
    import sys

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _guard(function):  # type: ignore[no-untyped-def]
    """Turn an input problem into a one-line message, not a traceback."""
    from functools import wraps

    @wraps(function)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        from ytcollector.genres.load import GenresError

        try:
            return function(*args, **kwargs)
        except GenresError as error:
            typer.secho(str(error), err=True, fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
        except ImportError as error:
            typer.secho(
                f"{error}. The genre tools are an optional extra: "
                'pip install -e ".[dev,genres]"',
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1) from None

    return wrapper


@app.command("init-tag-families")
@_guard
def init_tag_families(
    config_path: ConfigOption = None,
    output: FamiliesOption = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite the file.")] = False,
    ignore_expected: IgnoreExpected = False,
) -> None:
    """Write the first draft of config/tag_families.yml, every tag in `other`.

    The classification itself is the team's call, not the collector's.
    """
    from ytcollector.genres.load import (
        display,
        load_config,
        read_tag_votes,
        tag_families_path,
        tag_game_counts,
        write_tag_families_draft,
    )

    _logging()
    config = load_config(config_path)
    where = tag_families_path(output)
    if where.exists() and not force:
        typer.secho(
            f"{display(where)} already exists; pass --force to replace it "
            "(you would lose the classification it holds).",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    votes = read_tag_votes(config, ignore_expected=ignore_expected)
    counts = tag_game_counts(votes)
    written = write_tag_families_draft(counts, where)
    typer.echo(
        f"init-tag-families: {written} tags written to {display(where)}, all in 'other'. "
        "Classify them with the team, then pick used_for_factorization."
    )


@app.command("build-matrix")
@_guard
def build_matrix(
    config_path: ConfigOption = None,
    families_path: FamiliesOption = None,
    ignore_expected: IgnoreExpected = False,
) -> None:
    """Build the TF-IDF game x tag matrix and write it to the output directory."""
    from ytcollector.genres.load import display, load_config, load_game_tags, load_tag_families
    from ytcollector.genres.matrix import build_matrix as build
    from ytcollector.genres.matrix import write_matrix

    _logging()
    config = load_config(config_path)
    families = load_tag_families(families_path)
    game_tags = load_game_tags(config, ignore_expected=ignore_expected)
    result = build(config, families, game_tags)
    write_matrix(result, config.output_dir)

    typer.echo(
        f"build-matrix: {result.shape[0]:,} games x {result.shape[1]:,} tags, "
        f"{result.nnz:,} non-zero weights, {result.empty_rows:,} empty row(s) dropped. "
        f"Written to {display(config.output_dir)} — derived from client data, "
        "never commit it."
    )
