"""Planner behavior (STEP 11) — render_class guard, normalization, routing."""

import json

from directoragent.phases.arcs import get_arc
from directoragent.phases.mock_plan_provider import MockPlanProvider
from directoragent.phases.planner import plan
from directoragent.routing import drift_threshold, route
from directoragent.schema import Model, RenderClass, SceneModel

SCENE = SceneModel(
    source_photo_path="assets/p.png",
    subject="a lone figure",
    environment="neon street",
    lighting="high-contrast neon",
    mood="noir",
    objects=["umbrella"],
    color_palette=["#0a0e1a"],
)


def _shot(**over):
    base = dict(
        shot_name="n",
        shot_style="cinematic",
        render_class="FACE",
        narrative_beat="beat",
        camera_motion="push in",
        motion_preset="PUSH_IN",
        prompt="a filmable prompt",
        duration_s=15,
        quality="standard",
    )
    base.update(over)
    return base


class FakePlanProvider:
    """Returns a scripted plan (and arc choice) so tests control the payload."""

    def __init__(self, shots, arc="dramatic"):
        self._shots = shots
        self._arc = arc

    async def complete(self, image_path, prompt):
        if "SELECT_ARC" in prompt:
            return json.dumps({"arc": self._arc, "reason": "test"})
        return json.dumps({"shots": self._shots})


async def test_plan_returns_six_valid_shots():
    shots = await plan(SCENE, "noir chase", MockPlanProvider(), run_id="r")
    assert len(shots) == 6
    for s in shots:
        assert isinstance(s.render_class, RenderClass)
        assert s.model is route(s.render_class)[0]            # routed, not from provider
        assert s.min_drift_score == drift_threshold(s.render_class)
        assert 8 <= s.duration_s <= 30
    total = sum(s.duration_s for s in shots)
    assert 60 <= total <= 180


async def test_invalid_render_class_falls_back_to_beat_lean():
    shots_payload = [_shot(render_class="BOGUS")] + [_shot() for _ in range(5)]
    shots = await plan(SCENE, "d", FakePlanProvider(shots_payload, arc="dramatic"), run_id="r")
    lean = get_arc("dramatic")[0].render_lean  # WIDE_ENVIRONMENT
    assert shots[0].render_class == lean
    assert shots[0].model is route(lean)[0]
    assert "fallback" in shots[0].model_reason


async def test_motion_preset_normalizes_bad_value_to_static():
    shots_payload = [_shot(motion_preset="twirl")] + [_shot(motion_preset="ORBIT") for _ in range(5)]
    shots = await plan(SCENE, "d", FakePlanProvider(shots_payload), run_id="r")
    assert shots[0].motion_preset == "STATIC"
    assert shots[1].motion_preset == "ORBIT"


async def test_named_arc_is_honored():
    # Omit narrative_beat so the planner falls back to the arc's beat names,
    # which proves the requested arc (not the provider's) drove planning.
    payload = [_shot() for _ in range(6)]
    for s in payload:
        s.pop("narrative_beat")
    shots = await plan(SCENE, "d", FakePlanProvider(payload, arc="dramatic"), run_id="r", arc_name="reveal")
    assert [s.narrative_beat for s in shots] == [b.name for b in get_arc("reveal")]


async def test_model_and_drift_come_from_routing_not_provider():
    # Provider tries to dictate model/min_drift_score; planner must ignore them.
    payload = [_shot(render_class="FACE", model="veo_3_1", min_drift_score=0.01) for _ in range(6)]
    shots = await plan(SCENE, "d", FakePlanProvider(payload), run_id="r")
    for s in shots:
        assert s.render_class == RenderClass.FACE
        assert s.model is Model.SOUL_V2                       # route(FACE), not provider's veo
        assert s.min_drift_score == 0.78                     # drift_threshold(FACE), not 0.01
