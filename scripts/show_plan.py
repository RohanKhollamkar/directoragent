"""Inputs and plan for a persisted run (read-only).

Usage: python scripts/show_plan.py <run_id>

Prints, from the state store, what went into the run and what the first two
phases made of it:
  1. inputs: the reference photo (path, size, dimensions) and the scene description
  2. vision: the SceneModel extracted from the photo
  3. planner: the arc, then per shot the beat, render_class (and whether it
     follows the arc's render_lean), the routed model and why, drift threshold,
     duration, camera motion, and the static cost estimate; then the projected
     total. It only reads the run.
"""

import asyncio
import struct
import sys
from pathlib import Path

from directoragent.config import Settings
from directoragent.phases.arcs import ARC_LIBRARY
from directoragent.routing import estimate_cost
from directoragent.state.sqlite_store import SqliteStateStore


def _photo_facts(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return "(file not found)"
    facts = f"{p.stat().st_size:,} bytes"
    head = p.read_bytes()[:24]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", head[16:24])
        facts += f", PNG {w}x{h}"
    return facts


def _arc_of(shots):
    beats = [s.narrative_beat for s in shots]
    for name, arc in ARC_LIBRARY.items():
        if [b.name for b in arc] == beats:
            return name, arc
    return None, None


async def main(run_id: str) -> int:
    store = SqliteStateStore(Settings().state_db_path)
    try:
        state = await store.load_run(run_id)
    finally:
        await store.close()
    if state is None:
        print(f"no run {run_id!r} in the state store", file=sys.stderr)
        return 1

    scene = state.scene
    print(f"run_id={run_id}  run_status={state.status.value}")
    print("\n[1] inputs")
    print(f"  reference photo  : {scene.source_photo_path} ({_photo_facts(scene.source_photo_path)})")
    print(f"  scene description: {state.input_description}")

    print("\n[2] vision -> SceneModel")
    for field in ("subject", "environment", "lighting", "mood"):
        print(f"  {field:<12}: {getattr(scene, field)}")
    print(f"  {'objects':<12}: {', '.join(scene.objects) or '-'}")
    print(f"  {'palette':<12}: {', '.join(scene.color_palette) or '-'}")

    arc_name, arc = _arc_of(state.shots)
    print(f"\n[3] planner -> {len(state.shots)} shots, arc: {arc_name or '(custom)'}")
    projected = 0.0
    for i, shot in enumerate(state.shots):
        est = estimate_cost(shot.model, shot.duration_s)
        projected += est
        lean = arc[i].render_lean.value if arc else None
        follows = "" if lean is None else (
            " (follows arc lean)" if lean == shot.render_class.value
            else f" (overrides arc lean {lean})"
        )
        print(f"  {shot.shot_id}  {shot.shot_name}  [beat: {shot.narrative_beat}]")
        if arc:
            print(f"    intent      : {arc[i].intent}")
        print(f"    render_class: {shot.render_class.value}{follows}")
        print(f"    model       : {shot.model.value}  ({shot.model_reason})")
        print(f"    drift min   : {shot.min_drift_score:.2f}   duration: {shot.duration_s:g}s"
              f"   motion: {shot.motion_preset or '-'}")
        print(f"    est. cost   : {est:.2f}")
    print(f"\n  projected total (static estimate): {projected:.2f}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
