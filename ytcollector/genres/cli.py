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


@app.command("fit")
@_guard
def fit(
    config_path: ConfigOption = None,
    k: Annotated[
        str | None,
        typer.Option("--k", help="Grid of k: start:stop:step, a list, or one value."),
    ] = None,
    seeds: Annotated[
        int | None, typer.Option("--seeds", help="Seeds per k (default: 5).")
    ] = None,
    bootstrap: Annotated[
        int | None,
        typer.Option("--bootstrap", help="Subsample fits per k, for stability (default: 5)."),
    ] = None,
    max_iter: Annotated[int | None, typer.Option("--max-iter")] = None,
    beta_loss: Annotated[
        str | None, typer.Option("--beta-loss", help="frobenius or kullback-leibler.")
    ] = None,
    alpha_w: Annotated[
        float | None, typer.Option("--alpha-w", help="L1 penalty weight on W.")
    ] = None,
    l1_ratio: Annotated[float | None, typer.Option("--l1-ratio")] = None,
) -> None:
    """Fit NMF over a grid of k and write k_selection.csv (SPEC_GENRES G2)."""
    from dataclasses import replace

    from ytcollector.genres.load import display, load_config
    from ytcollector.genres.matrix import load_matrix
    from ytcollector.genres.nmf import (
        NmfParams,
        best_k,
        fit_grid,
        parse_grid,
        write_k_selection,
    )

    _logging()
    config = load_config(config_path)
    built = load_matrix(config.output_dir)

    params = NmfParams.from_config(config)
    overrides = {
        "max_iter": max_iter,
        "beta_loss": beta_loss,
        "alpha_W": alpha_w,
        "l1_ratio": l1_ratio,
    }
    params = replace(params, **{key: v for key, v in overrides.items() if v is not None})

    grid = parse_grid(k, default=str(config.nmf.get("k_grid", "10:60:5")))
    n_seeds = seeds if seeds is not None else int(config.nmf.get("seeds", 5))
    n_bootstrap = bootstrap if bootstrap is not None else int(config.nmf.get("n_bootstrap", 5))
    fraction = float(config.nmf.get("bootstrap_fraction", 0.8))

    typer.echo(
        f"fit: {len(grid)} value(s) of k ({grid[0]}..{grid[-1]}), {n_seeds} seed(s) and "
        f"{n_bootstrap} subsample(s) each, on {built.shape[0]:,} games x "
        f"{built.shape[1]:,} tags. Loss {params.beta_loss}, alpha_W {params.alpha_W}."
    )
    reports = fit_grid(
        built.matrix,
        ks=grid,
        seeds=n_seeds,
        params=params,
        n_bootstrap=n_bootstrap,
        bootstrap_fraction=fraction,
        output_dir=config.output_dir,
    )
    write_k_selection(reports, config.output_dir)

    suggestion = best_k(reports)
    typer.echo(
        f"fit: {len(reports)} value(s) of k scored, written to "
        f"{display(config.output_dir)}/k_selection.csv."
    )
    if suggestion is not None:
        typer.echo(
            f"fit: k={suggestion} is the most stable of the grid. Read the four "
            "indicators with PulluP before setting k_final — a k that is not stable "
            "must not be interpreted."
        )
