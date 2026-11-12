"""Per-shot cost ledger for a persisted run (read-only).

Usage: python scripts/shot_ledger.py <run_id>

The CLI's shot grid lists only shots that passed, and its `cost` column is the
shot's total across attempts. This prints every planned shot from the state
store with its attempt count, final status (success / failed / not run), the
cost of its last attempt, and its total cost. It only reads the run.
"""

import asyncio
import sys

from directoragent.config import Settings
from directoragent.schema import AttemptStatus
from directoragent.state.sqlite_store import SqliteStateStore


async def main(run_id: str) -> int:
    store = SqliteStateStore(Settings().state_db_path)
    try:
        state = await store.load_run(run_id)
    finally:
        await store.close()
    if state is None:
        print(f"no run {run_id!r} in the state store", file=sys.stderr)
        return 1

    header = (
        f"{'shot_id':<8} {'shot_name':<16} {'render_class':<16} {'model':<10} "
        f"{'att':>3}  {'status':<8} {'last_status':<13} {'drift':>5} "
        f"{'last_cost':>9} {'total_cost':>10}"
    )
    print(f"ledger  run_id={run_id}  run_status={state.status.value}")
    print(header)
    print("-" * len(header))

    passed = 0
    attempts_total = 0
    for shot in state.shots:
        attempts = state.attempts.get(shot.shot_id, [])
        last = attempts[-1] if attempts else None
        attempts_total += len(attempts)
        if last is None:
            status, last_status, drift, last_cost = "not run", "-", "-", 0.0
        else:
            ok = last.status == AttemptStatus.PASSED
            passed += ok
            status = "success" if ok else "failed"
            last_status = last.status.value
            drift = f"{last.drift_score:.2f}" if last.drift_score is not None else "-"
            last_cost = last.cost
        total = sum(a.cost for a in attempts)
        print(
            f"{shot.shot_id:<8} {shot.shot_name[:16]:<16} "
            f"{shot.render_class.value:<16} {shot.model.value:<10} "
            f"{len(attempts):>3}  {status:<8} {last_status:<13} {drift:>5} "
            f"{last_cost:>9.2f} {total:>10.2f}"
        )

    print(
        f"\n{passed}/{len(state.shots)} shots succeeded | {attempts_total} attempts | "
        f"run total_cost: {state.total_cost:.2f}"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
