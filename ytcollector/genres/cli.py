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


@app.command("report")
@_guard
def report(
    config_path: ConfigOption = None,
    k: Annotated[int, typer.Option("--k", help="Any k of the grid.")] = 0,
    seed: Annotated[
        int | None, typer.Option("--seed", help="Force a seed (default: the best one).")
    ] = None,
) -> None:
    """Write report_k{K}.md for one k of the grid (SPEC_GENRES G3)."""
    from ytcollector.genres.load import GenresError, display, load_config
    from ytcollector.genres.select import (
        available_ks,
        load_model,
        thresholds_of,
        write_report,
    )

    _logging()
    config = load_config(config_path)
    if k < 2:
        known = available_ks(config.output_dir)
        raise GenresError(
            "--k is required and must be a k of the grid"
            + (f" ({', '.join(str(x) for x in known)})" if known else "; run `ytc genres fit` first")
            + "."
        )

    model = load_model(config.output_dir, k, seed=seed)
    path = write_report(model, config.output_dir, thresholds_of(config))
    typer.echo(
        f"report: k={model.k}, seed {model.seed} (relative error {model.rel_error:.4f}), "
        f"{model.k} genres over {len(model.rows):,} games. Written to "
        f"{display(config.output_dir)}/{path.name}. The genre names are provisional."
    )


@app.command("select")
@_guard
def select(
    config_path: ConfigOption = None,
) -> None:
    """Write the final tables for k_final (SPEC_GENRES G3).

    Refuses while ``k_final`` is null: which k the study runs on is a decision,
    not a default.
    """
    from ytcollector.genres.load import display, load_config
    from ytcollector.genres.select import (
        final_k,
        load_model,
        thresholds_of,
        write_report,
        write_tables,
        write_tree,
    )

    _logging()
    config = load_config(config_path)
    k = final_k(config)
    thresholds = thresholds_of(config)

    model = load_model(config.output_dir, k)
    write_tables(model, config.output_dir, thresholds)
    tree = write_tree(config.output_dir)
    write_report(model, config.output_dir, thresholds)

    typer.echo(
        f"select: k_final={k}, seed {model.seed}. genre_definition.parquet and "
        f"game_genre.parquet written to {display(config.output_dir)}"
        + (", genre_tree.csv as well" if tree else "")
        + f". {model.games_without_weight:,} game(s) carry no genre weight and are left out."
    )


StudyOption = Annotated[str, typer.Option("--study", help="Study name, e.g. battle_royale.")]


@app.command("suggest-appids")
@_guard
def suggest_appids_command(
    study: StudyOption = "battle_royale",
    config_path: ConfigOption = None,
    per_game: Annotated[int, typer.Option("--per-game")] = 5,
    min_score: Annotated[float, typer.Option("--min-score")] = 0.55,
) -> None:
    """Propose a Steam appid for each game of a study (SPEC_GENRES G4).

    Writes a CSV of candidates and nothing else: the study file and the database
    are the team's to edit, once a human has read the proposals.
    """
    from ytcollector.genres.load import display, load_config
    from ytcollector.genres.study import (
        CONFIDENT_SCORE,
        load_study_or_fail,
        reference_frame,
        suggest_appids,
        write_suggestions,
    )

    _logging()
    config = load_config(config_path)
    roster = load_study_or_fail(study)
    suggestions = suggest_appids(
        roster, reference_frame(config), per_game=per_game, min_score=min_score
    )
    path = write_suggestions(suggestions, config.output_dir)

    best: dict[str, float] = {}
    for item in suggestions:
        best[item.game_id] = max(best.get(item.game_id, 0.0), item.score)
    proposed = set(best)
    silent = [g.game_id for g in roster.games if g.game_id not in proposed]
    doubtful = sorted(g for g, value in best.items() if value < CONFIDENT_SCORE)
    typer.echo(
        f"suggest-appids: {len(suggestions)} candidate(s) for "
        f"{len(proposed)}/{len(roster.games)} games, written to "
        f"{display(config.output_dir)}/{path.name}. Nothing else was touched: copy the "
        "appid you validate into config/studies/" + study + ".yml yourself."
    )
    if silent:
        typer.echo(
            f"suggest-appids: no candidate for {', '.join(silent)} — a game with no "
            "Steam page (a mobile version, Fortnite) simply stays empty."
        )
    if doubtful:
        typer.echo(
            f"suggest-appids: best candidate below {CONFIDENT_SCORE:g} for "
            f"{', '.join(doubtful)} — most likely games that are not on Steam at all. "
            "Read those rows before copying anything."
        )


@app.command("check-study")
@_guard
def check_study_command(
    study: StudyOption = "battle_royale",
    k: Annotated[int, typer.Option("--k", help="Any k of the grid.")] = 0,
    config_path: ConfigOption = None,
) -> None:
    """Read a study's games through the genres of one k (SPEC_GENRES G4)."""
    from ytcollector.genres.load import GenresError, load_config
    from ytcollector.genres.select import available_ks, load_model
    from ytcollector.genres.study import BATTLE_ROYALE_TAG, check_study, load_study_or_fail

    _logging()
    config = load_config(config_path)
    if k < 2:
        known = available_ks(config.output_dir)
        raise GenresError(
            "--k is required and must be a k of the grid"
            + (f" ({', '.join(str(x) for x in known)})" if known else "; run `ytc genres fit` first")
            + "."
        )

    roster = load_study_or_fail(study)
    model = load_model(config.output_dir, k)
    found = check_study(model, roster)

    typer.echo(f"check-study: {study} at k={k}, seed {model.seed}.")
    if not found.with_appid:
        typer.echo(
            "check-study: not one game of the study carries a steam_appid yet. "
            "Run `ytc genres suggest-appids` and fill the validated ones into the "
            "study file."
        )
        return

    for item in found.with_appid:
        genres = ", ".join(
            f"{g['label']} ({g['share']:.0%})" for g in item["genres"]
        ) or "no genre weight"
        typer.echo(f"  {item['game_name']}: {genres}")

    if found.leading_genre is not None:
        typer.echo(
            f"check-study: genre {found.leading_genre} ({found.leading_genre_label}) is "
            f"dominant for {found.leading_genre_count}/{len(found.with_appid)} of them."
        )
        typer.echo(
            f"check-study: against the plain '{BATTLE_ROYALE_TAG}' tag — the tag is on "
            f"{found.tag_games:,} games, {found.tag_and_study} of which are in the study; "
            f"the genre holds {found.genre_outside_study:,} games the study does not list"
            + (
                f", and misses {len(found.study_missing_from_genre)} it does: "
                + ", ".join(found.study_missing_from_genre)
                if found.study_missing_from_genre
                else ""
            )
            + "."
        )
    if found.without_appid:
        typer.echo(
            f"check-study: {len(found.without_appid)} game(s) without a steam_appid: "
            + ", ".join(found.without_appid)
        )
    if found.unknown_appid:
        typer.echo(
            f"check-study: {len(found.unknown_appid)} appid(s) absent from the matrix "
            "(below min_total_votes, or not in the export): "
            + ", ".join(found.unknown_appid)
        )


@app.command("supply")
@_guard
def supply_command(
    k: Annotated[int, typer.Option("--k", help="Any k of the grid.")] = 0,
    config_path: ConfigOption = None,
) -> None:
    """Count releases per genre, by month and by year (SPEC_GENRES G5)."""
    from ytcollector.genres.load import GenresError, display, load_config
    from ytcollector.genres.select import available_ks, load_model
    from ytcollector.genres.supply import genre_supply, write_supply

    _logging()
    config = load_config(config_path)
    if k < 2:
        known = available_ks(config.output_dir)
        raise GenresError(
            "--k is required and must be a k of the grid"
            + (f" ({', '.join(str(x) for x in known)})" if known else "; run `ytc genres fit` first")
            + "."
        )

    model = load_model(config.output_dir, k)
    supply = genre_supply(model)
    write_supply(supply, config.output_dir)

    span = ""
    if not supply.by_month.empty:
        first, last = supply.by_month["year"].min(), supply.by_month["year"].max()
        span = f", {first}–{last}"
    typer.echo(
        f"supply: k={k}, {supply.games_dated:,} dated game(s) spread over {model.k} "
        f"genres{span}. {supply.games_undated:,} game(s) have no usable release date "
        "and are left out, never imputed. Written to "
        f"{display(config.output_dir)}: genre_supply_month.parquet, "
        "genre_supply_year.parquet."
    )
