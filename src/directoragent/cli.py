"""Command-line interface (STEP 10) — the `da` entry point.

A thin typer shell over pipeline.run: it builds Settings (flags override env),
generates/echoes the run_id, and drives run / resume / status / list. All the
real work lives behind pipeline.run; this layer only parses and reports.
"""

import asyncio
import uuid
from typing import Optional

import typer
from pydantic import ValidationError

from directoragent import pipeline
from directoragent.config import Settings
from directoragent.phases.arcs import ARC_LIBRARY
from directoragent.pipeline import CostCeilingError
from directoragent.state.sqlite_store import SqliteStateStore

app = typer.Typer(help="DirectorAgent — one photo + scene -> 6-shot storyboard.")


def _build_settings(
    mock: bool, max_cost: Optional[float], provider: Optional[str]
) -> Settings:
    overrides: dict = {}
    if mock:
        overrides["mock_mode"] = True
    if max_cost is not None:
        overrides["max_cost_usd"] = max_cost
    if provider is not None:
        overrides["vision_provider"] = provider
    try:
        return Settings(**overrides)
    except ValidationError as e:
        typer.echo(f"invalid settings: {e}", err=True)
        raise typer.Exit(code=2)


async def _load(run_id: str, settings: Settings):
    store = SqliteStateStore(settings.state_db_path)
    try:
        return await store.load_run(run_id)
    finally:
        await store.close()


async def _list(settings: Settings):
    store = SqliteStateStore(settings.state_db_path)
    try:
        return await store.list_runs()
    finally:
        await store.close()


@app.command()
def run(
    photo: str = typer.Argument(..., help="Path to the source photo."),
    description: str = typer.Argument(..., help="Scene description."),
    mock: bool = typer.Option(False, "--mock", help="Run fully offline with mocks."),
    max_cost: Optional[float] = typer.Option(None, "--max-cost", help="USD ceiling."),
    provider: Optional[str] = typer.Option(
        None, "--provider", help="openai | anthropic | gemini"
    ),
    arc: Optional[str] = typer.Option(
        None, "--arc", help="dramatic | observational | reveal | mood-piece"
    ),
    review: bool = typer.Option(
        False, "--review", help="Plan only; print the plan and stop before spend."
    ),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Reuse/resume this id."),
) -> None:
    if arc is not None and arc not in ARC_LIBRARY:
        typer.echo(
            f"unknown arc {arc!r}; choose from: {', '.join(ARC_LIBRARY)}", err=True
        )
        raise typer.Exit(code=2)

    settings = _build_settings(mock, max_cost, provider)
    rid = run_id or uuid.uuid4().hex
    typer.echo(f"run_id: {rid}")  # printed immediately so it's recoverable

    try:
        asyncio.run(
            pipeline.run(photo, description, rid, settings, arc_name=arc, plan_only=review)
        )
    except CostCeilingError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1)

    if review:
        typer.echo(
            f"\nPlan ready — review above, then run `da resume {rid}` to generate."
        )


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="The run to resume."),
    mock: bool = typer.Option(False, "--mock", help="Run fully offline with mocks."),
    max_cost: Optional[float] = typer.Option(None, "--max-cost", help="USD ceiling."),
) -> None:
    settings = _build_settings(mock, max_cost, None)
    if asyncio.run(_load(run_id, settings)) is None:
        typer.echo(f"no such run: {run_id}", err=True)
        raise typer.Exit(code=1)
    # Same code path as run: pipeline loads the persisted plan and executes.
    try:
        asyncio.run(pipeline.run("", "", run_id, settings))
    except CostCeilingError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def status(run_id: str = typer.Argument(..., help="The run to inspect.")) -> None:
    settings = _build_settings(False, None, None)
    state = asyncio.run(_load(run_id, settings))
    if state is None:
        typer.echo(f"no such run: {run_id}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"run {state.run_id}  status={state.status.value}  total_cost=${state.total_cost:.2f}")
    header = (
        f"{'shot_id':<9} {'shot_style':<24} {'render_class':<16} "
        f"{'model':<9} {'status':<13} {'drift':>6}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for shot in state.shots:
        last = state.latest_attempt(shot.shot_id)
        st = last.status.value if last else "pending"
        drift = f"{last.drift_score:.3f}" if last and last.drift_score is not None else "-"
        style = shot.shot_style if len(shot.shot_style) <= 24 else shot.shot_style[:23] + "…"
        typer.echo(
            f"{shot.shot_id:<9} {style:<24} {shot.render_class.value:<16} "
            f"{shot.model.value:<9} {st:<13} {drift:>6}"
        )


@app.command("list")
def list_runs() -> None:
    settings = _build_settings(False, None, None)
    rows = asyncio.run(_list(settings))
    if not rows:
        typer.echo("(no runs)")
        return
    header = f"{'run_id':<34} {'status':<11} {'cost':>9}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for run_id, run_status, cost in rows:
        typer.echo(f"{run_id:<34} {run_status:<11} {'$' + format(cost, '.2f'):>9}")


if __name__ == "__main__":
    app()
