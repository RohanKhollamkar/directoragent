"""Pipeline wiring (STEP 9).

The single composition root: it constructs the store, selects providers and
adapters (the ONE place mock vs real is decided — phases never branch on
mock_mode), then drives vision -> plan -> (cost gate) -> execute -> assemble.

Real adapters (HiggsfieldClient, ClipDriftScorer) are imported lazily inside
the non-mock branch so that mock mode — and therefore CI — never imports torch
or open_clip.
"""

from datetime import datetime, timezone

from directoragent.config import Settings
from directoragent.phases import assembler, executor, planner
from directoragent.phases.arcs import ARC_LIBRARY
from directoragent.phases.mock_plan_provider import MockPlanProvider
from directoragent.phases.vision import VisionExtractor
from directoragent.clients.higgsfield_mock import MockHiggsfieldClient
from directoragent.drift.mock_scorer import MockDriftScorer
from directoragent.schema import RunState, RunStatus, Storyboard
from directoragent.state.sqlite_store import SqliteStateStore
from directoragent.vision_providers import MockVisionProvider, make_provider


class CostCeilingError(RuntimeError):
    """Projected plan cost exceeds the configured ceiling — raised before any
    job is submitted, so no spend occurs."""

    def __init__(self, projected: float, max_cost: float):
        self.projected = projected
        self.max_cost = max_cost
        super().__init__(
            f"projected plan cost ${projected:.2f} exceeds ceiling "
            f"${max_cost:.2f}; raise --max-cost or trim the plan"
        )


def _infer_arc(shots) -> str | None:
    """Best-effort recovery of the arc name from the shots' narrative beats
    (RunState doesn't store the arc; the beat names usually identify it)."""
    beats = [s.narrative_beat for s in shots]
    for name, arc_beats in ARC_LIBRARY.items():
        if [b.name for b in arc_beats] == beats:
            return name
    return None


def print_plan(state: RunState, projected: float, per_shot_costs: list[float]) -> None:
    print(f"=== PLAN  run_id={state.run_id} ===")
    print(f"arc: {_infer_arc(state.shots) or '(custom)'}")
    for shot, cost in zip(state.shots, per_shot_costs):
        print(
            f"\n{shot.shot_id}  beat={shot.narrative_beat}  "
            f"render_class={shot.render_class.value}  model={shot.model.value}  "
            f"dur={shot.duration_s:g}s  cost=${cost:.2f}"
        )
        print(f"  style : {shot.shot_style}")
        print(f"  motion: {shot.camera_motion} [{shot.motion_preset}]")
        print(f"  prompt: {shot.prompt}")
    print(f"\nprojected total cost: ${projected:.2f}")
    print(f"plan persisted. run `da resume {state.run_id}` to execute.")


async def run(
    photo_path: str,
    description: str,
    run_id: str,
    settings: Settings,
    arc_name: str | None = None,
    plan_only: bool = False,
) -> Storyboard | None:
    store = SqliteStateStore(settings.state_db_path)

    # --- Provider/adapter selection — the only mock-vs-real branch ----------
    if settings.mock_mode:
        vision_provider = MockVisionProvider()
        plan_provider = MockPlanProvider()
        hf = MockHiggsfieldClient()
        scorer = MockDriftScorer()
    else:
        # Imported lazily so mock mode never drags in torch/open_clip.
        from directoragent.clients.higgsfield import HiggsfieldClient
        from directoragent.drift.clip_scorer import ClipDriftScorer

        shared = make_provider(settings.vision_provider, settings.vision_model)
        vision_provider = plan_provider = shared
        hf = HiggsfieldClient(settings.higgsfield_api_key)
        scorer = ClipDriftScorer()

    vision = VisionExtractor(vision_provider)

    try:
        state = await store.load_run(run_id)
        if state is None:
            scene = await vision.extract_scene(photo_path)
            shots = await planner.plan(
                scene, description, plan_provider, run_id, arc_name=arc_name
            )
            per_shot_costs = [await hf.preflight_cost(s) for s in shots]
            projected = sum(per_shot_costs)
            if projected > settings.max_cost_usd:
                raise CostCeilingError(projected, settings.max_cost_usd)

            status = RunStatus.PLANNING if plan_only else RunStatus.EXECUTING
            state = RunState(
                run_id=run_id,
                status=status,
                scene=scene,
                input_description=description,
                shots=shots,
                created_at=datetime.now(timezone.utc),
            )
            await store.create_run(state)
            for shot in shots:
                await store.save_shot(run_id, shot)

            if plan_only:
                print_plan(state, projected, per_shot_costs)
                return None  # stop before any spend; `da resume <run_id>` executes

        # Resume from PLANNING or EXECUTING both fall through to execution.
        await executor.run_all(state, store, hf, scorer, settings.max_cost_usd)
        return assembler.assemble(await store.load_run(run_id))
    finally:
        await store.close()
