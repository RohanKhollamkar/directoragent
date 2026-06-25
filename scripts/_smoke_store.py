"""Throwaway smoke test for SqliteStateStore crash-window persistence.

Proves the open_attempt -> record_job_id ledger writes and the run/shot/cost
state round-trip through load_run(). Run: `python scripts/_smoke_store.py`.
Not a pytest test; safe to delete.
"""

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from directoragent.schema import (
    Attempt,
    AttemptStatus,
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


def _shot(shot_id: str, name: str, render_class: RenderClass, model: Model, drift: float) -> Shot:
    return Shot(
        shot_id=shot_id,
        shot_name=name,
        shot_style="establishing wide",
        render_class=render_class,
        narrative_beat="a beat",
        model=model,
        model_reason="because",
        camera_motion="push in",
        prompt="a prompt",
        reference=Reference(type=ReferenceType.SOURCE_PHOTO, source="photo.jpg"),
        duration_s=4.0,
        quality=QualityTier.STANDARD,
        min_drift_score=drift,
    )


async def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "run" / "state.db"
    store = SqliteStateStore(str(tmp))

    scene = SceneModel(
        source_photo_path="photo.jpg",
        subject="a lone figure",
        environment="neon street",
        lighting="high-contrast neon",
        mood="noir",
        objects=["umbrella"],
        color_palette=["#0a0e1a"],
    )
    shot1 = _shot("shot_1", "Establish", RenderClass.WIDE_ENVIRONMENT, Model.VEO_3_1, 0.70)
    shot2 = _shot("shot_2", "Close", RenderClass.FACE, Model.SOUL_V2, 0.78)

    state = RunState(
        run_id="run_smoke",
        status=RunStatus.EXECUTING,
        scene=scene,
        input_description="a noir city scene",
        shots=[shot1, shot2],
        total_cost=0.0,
        created_at=datetime.now(timezone.utc),
    )

    await store.create_run(state)
    await store.save_shot("run_smoke", shot1)
    await store.save_shot("run_smoke", shot2)

    attempt = Attempt(
        attempt_id="att_1",
        run_id="run_smoke",
        shot_id="shot_1",
        attempt_number=1,
        idem_key="run_smoke:shot_1:1",
    )
    await store.open_attempt(attempt)              # -> SUBMITTING (durable pre-network)
    await store.record_job_id("att_1", "job_abc")  # -> RUNNING + job_id
    await store.add_cost("run_smoke", 1.80)

    # --- reconstruct from disk and assert -----------------------------------
    loaded = await store.load_run("run_smoke")
    assert loaded is not None, "load_run returned None"

    shot_ids = {s.shot_id for s in loaded.shots}
    assert shot_ids == {"shot_1", "shot_2"}, f"expected both shots, got {shot_ids}"

    att = loaded.latest_attempt("shot_1")
    assert att is not None, "no attempt reconstructed for shot_1"
    assert att.status == AttemptStatus.RUNNING, f"status was {att.status}"
    assert att.job_id == "job_abc", f"job_id was {att.job_id!r}"

    assert abs(loaded.total_cost - 1.80) < 1e-9, f"total_cost was {loaded.total_cost}"

    await store.close()

    print("db path        :", tmp)
    print("shots loaded   :", sorted(shot_ids))
    print("attempt status :", att.status.value)
    print("attempt job_id :", att.job_id)
    print("total_cost     :", loaded.total_cost, "(added 1.80)")
    print("OK: crash-window persistence round-trips")


if __name__ == "__main__":
    asyncio.run(main())
