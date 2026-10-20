"""Retry-path demo: one shot whose first attempt is forced to fail drift.

Mirrors tests/test_executor.py::test_drift_fail_creates_second_attempt_and_passes
using only public directoragent imports: a single FACE shot, the mock Higgsfield
client, MockDriftScorer(fail_first=1), and a temp SQLite store driven through
executor.run_all. Zero credentials, zero cost, no files left behind.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timezone

from directoragent.clients.higgsfield_mock import MockHiggsfieldClient
from directoragent.drift.mock_scorer import MockDriftScorer
from directoragent.phases.executor import run_all
from directoragent.schema import (
    Model,
    QualityTier,
    Reference,
    ReferenceType,
    RenderClass,
    RunState,
    RunStatus,
    SceneModel,
    Shot,
)
from directoragent.state.sqlite_store import SqliteStateStore

RUN_ID = "retry-demo"
PHOTO = "assets/test.png"


def _shot() -> Shot:
    # model / min_drift_score are what the planner's route()/drift_threshold()
    # assign for FACE; hard-coded here because only the planner may call them.
    return Shot(
        shot_id="shot_01",
        shot_name="Arrival",
        shot_style="intimate portrait",
        render_class=RenderClass.FACE,
        narrative_beat="introduce subject",
        model=Model.SEEDANCE_2,
        model_reason="face -> seedance_2",
        camera_motion="locked-off close framing",
        motion_preset="STATIC",
        prompt="Medium close-up of a figure in a long coat under neon rim light.",
        reference=Reference(type=ReferenceType.SOURCE_PHOTO, source=PHOTO),
        duration_s=10,
        quality=QualityTier.STANDARD,
        min_drift_score=0.78,  # FACE threshold
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStateStore(os.path.join(tmp, "retry_demo.db"))
        shot = _shot()
        state = RunState(
            run_id=RUN_ID,
            status=RunStatus.EXECUTING,
            scene=SceneModel(
                source_photo_path=PHOTO,
                subject="figure",
                environment="street",
                lighting="neon",
                mood="noir",
                objects=[],
                color_palette=[],
            ),
            input_description="retry-path demo",
            shots=[shot],
            total_cost=0.0,
            created_at=datetime.now(timezone.utc),
        )
        await store.create_run(state)
        await store.save_shot(RUN_ID, shot)

        print(
            f"retry-path demo: attempt 1 is forced to fail drift "
            f"(MockDriftScorer fail_first=1, threshold {shot.min_drift_score:.2f})"
        )
        await run_all(
            state, store, MockHiggsfieldClient(), MockDriftScorer(fail_first=1),
            max_cost_usd=100.0,
        )

        # Read back what was persisted, not the in-memory state.
        loaded = await store.load_run(RUN_ID)
        await store.close()

    attempts = loaded.attempts[shot.shot_id]
    print(f"{'att':>3}  {'idem_key':<20} {'status':<13} {'drift':>5} {'cost':>6}  job_id")
    print("-" * 72)
    for a in attempts:
        drift = f"{a.drift_score:.3f}" if a.drift_score is not None else "-"
        print(
            f"{a.attempt_number:>3}  {a.idem_key:<20} {a.status.value:<13} "
            f"{drift:>5} {a.cost:>6.2f}  {a.job_id}"
        )
    print()
    print(f"run total_cost: {loaded.total_cost:.2f}")
    first_try = sum(1 for a in attempts if a.attempt_number == 1 and a.status.value == "passed")
    print(
        f"1 shot, {len(attempts)} attempts, first-try yield {first_try * 100}%, "
        f"cost = {len(attempts)} submissions"
    )


if __name__ == "__main__":
    asyncio.run(main())
