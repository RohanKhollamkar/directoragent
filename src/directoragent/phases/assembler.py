"""Phase 5 — the assembler (STEP 8).

Collapses the execution log into a Storyboard: one ShotResult per shot that
reached a PASSED attempt, plus run-level metrics (first-try yield, mean drift,
total cost). Shots with no PASSED attempt (e.g. budget exhausted) are failures,
not crashes — they are reported and omitted from results. Always returns a
valid Storyboard, even if every shot failed.

Side effects: writes .directoragent/runs/{run_id}/storyboard.json and prints a
shot grid + summary line.
"""

import logging
from pathlib import Path

from directoragent.schema import (
    AttemptStatus,
    RunState,
    ShotResult,
    Storyboard,
)

log = logging.getLogger(__name__)

_RUNS_DIR = Path(".directoragent/runs")


def _passed_attempt(attempts):
    for a in attempts:
        if a.status == AttemptStatus.PASSED:
            return a
    return None


def assemble(state: RunState) -> Storyboard:
    results: list[ShotResult] = []
    failures: list[str] = []

    for shot in state.shots:
        attempts = state.attempts.get(shot.shot_id, [])
        passed = _passed_attempt(attempts)
        if passed is None:
            failures.append(shot.shot_id)  # budget exhausted / never cleared drift
            continue
        results.append(
            ShotResult(
                shot=shot,
                result_url=passed.result_url or "",
                drift_score=passed.drift_score if passed.drift_score is not None else 0.0,
                model=shot.model,
                attempts_used=passed.attempt_number,
                first_try=passed.attempt_number == 1,
                cost=sum(a.cost for a in attempts),  # all attempts for this shot
            )
        )

    total_shots = len(state.shots)
    first_try_yield = (
        sum(1 for r in results if r.first_try) / total_shots if total_shots else 0.0
    )
    drifts = [r.drift_score for r in results]
    mean_drift = sum(drifts) / len(drifts) if drifts else 0.0

    storyboard = Storyboard(
        run_id=state.run_id,
        results=results,
        total_cost=state.total_cost,
        first_try_yield=first_try_yield,
        mean_drift=mean_drift,
    )

    if failures:
        log.warning("run %s: %d shot(s) failed: %s", state.run_id, len(failures), failures)

    _write_storyboard(storyboard)
    _print_report(storyboard, failures)
    return storyboard


def _write_storyboard(storyboard: Storyboard) -> Path:
    path = _RUNS_DIR / storyboard.run_id / "storyboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(storyboard.model_dump_json(indent=2))
    return path


def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_report(storyboard: Storyboard, failures: list[str]) -> None:
    header = (
        f"{'shot_name':<16} {'shot_style':<22} {'render_class':<16} "
        f"{'model':<9} {'drift':>6} {'att':>3} {'cost':>8}  result_url"
    )
    print(header)
    print("-" * len(header))
    for r in storyboard.results:
        print(
            f"{_trunc(r.shot.shot_name, 16):<16} "
            f"{_trunc(r.shot.shot_style, 22):<22} "
            f"{r.shot.render_class.value:<16} "
            f"{r.model.value:<9} "
            f"{r.drift_score:>6.3f} "
            f"{r.attempts_used:>3} "
            f"{'$' + format(r.cost, '.2f'):>8}  "
            f"{r.result_url}"
        )
    print(
        f"\nfirst-try yield: {storyboard.first_try_yield * 100:.0f}% | "
        f"mean drift: {storyboard.mean_drift:.3f} | "
        f"total cost: ${storyboard.total_cost:.2f} | "
        f"failures: {', '.join(failures) if failures else 'none'}"
    )
