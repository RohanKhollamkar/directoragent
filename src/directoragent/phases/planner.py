"""Phase 2 — the planner (STEP 6).

Turns a SceneModel + a free-text description into exactly six Shots along a
chosen narrative arc. The LLM proposes per-shot creative fields (style,
render_class, prompt, motion, duration); the planner DERIVES everything that
carries cost or routing meaning — model, model_reason, min_drift_score —
deterministically from routing.py. The LLM never sets those.

This is the ONLY module allowed to call route()/drift_threshold().
"""

import json
import logging

from pydantic import ValidationError

from directoragent.phases.arcs import ARC_LIBRARY, DEFAULT_ARC, Beat, get_arc
from directoragent.phases.model_limits import clamp_duration
from directoragent.phases.motion import normalize_motion
from directoragent.routing import drift_threshold, route
from directoragent.schema import (
    QualityTier,
    Reference,
    ReferenceType,
    RenderClass,
    SceneModel,
    Shot,
)
from directoragent.vision_providers import VisionProvider

log = logging.getLogger(__name__)


class PlanningError(RuntimeError):
    """Raised when planning fails even after one repair attempt."""


class PlanValidationError(ValueError):
    """A parsed plan that violates a structural rule (count, duration, ...)."""


# --- Prompt building --------------------------------------------------------
_PLAN_RULES = """\
RENDER_CLASS RUBRIC (propose one of FACE / COMPLEX_MOTION / ABSTRACT_FLUID / WIDE_ENVIRONMENT):
- FACE = identity/expression-centric.
- WIDE_ENVIRONMENT = scale/establishing.
- COMPLEX_MOTION = significant subject or camera movement.
- ABSTRACT_FLUID = non-representational / fluid / transition.
Default toward the beat's render_lean unless the shot clearly calls for another.

FILMABLE-PROMPT RUBRIC:
- 2-4 sentences, present tense, dense visual nouns/verbs, no narrative connective tissue.
- Representational shots MUST include: subject + concrete observable action; framing/shot scale; camera movement; lighting/atmosphere (from the scene model); a setting anchor in the established environment.
- ABSTRACT_FLUID shots: replace "subject + action" with "visual motif + motion quality" and relax the setting anchor; framing, camera movement, lighting still apply.
- FORBIDDEN: internal states, plot/backstory, unfilmable abstraction, dialogue. A logline is rejected.
- EXAMPLE - Unfilmable: "The detective realizes she's been betrayed." Filmable: "Medium close-up, slow push-in on the detective's face under a single overhead bulb; her eyes shift left, jaw tightening; cold blue light, deep shadows behind."

GROUNDEDNESS CONTRACT:
- Subject identity stays stable across representational shots (same character); reference it CONCISELY - do NOT exhaustively redescribe appearance (the source photo is the identity reference passed to generation; over-describing fights it).
- Palette/lighting anchored to the scene's world; may modulate with the beat.
- Framing, angle, action free to vary.
- WIDE_ENVIRONMENT: lighter subject binding, emphasize establishing the world. ABSTRACT_FLUID: drop subject binding, keep the palette/mood anchor.

DURATION: each shot 4-15s; wide/establishing shots (Veo) are short: 4-8s. Propose realistic per-shot durations — they are snapped to each model's allowed set.

OUTPUT: respond with ONLY this JSON (no prose, no markdown fences), exactly 6 shots:
{
  "shots": [
    {
      "shot_name": "short label",
      "shot_style": "free cinematic descriptor",
      "render_class": "FACE | COMPLEX_MOTION | ABSTRACT_FLUID | WIDE_ENVIRONMENT",
      "narrative_beat": "the beat name",
      "camera_motion": "free prose describing the camera move",
      "motion_preset": "PUSH_IN | PULL_OUT | PAN_LEFT | PAN_RIGHT | TILT_UP | TILT_DOWN | ORBIT | STATIC | HANDHELD | CRANE",
      "prompt": "the filmable prompt",
      "duration_s": 8,
      "quality": "draft | standard | high"
    }
  ]
}
"""


def _scene_block(scene: SceneModel) -> str:
    return (
        f"subject: {scene.subject}\n"
        f"environment: {scene.environment}\n"
        f"lighting: {scene.lighting}\n"
        f"mood: {scene.mood}\n"
        f"objects: {', '.join(scene.objects) or '(none)'}\n"
        f"color_palette: {', '.join(scene.color_palette) or '(none)'}"
    )


def _beats_block(beats: list[Beat]) -> str:
    return "\n".join(
        f"{i + 1}. {b.name} - {b.intent} (render_lean: {b.render_lean.value})"
        for i, b in enumerate(beats)
    )


def _build_arc_prompt(scene: SceneModel, description: str) -> str:
    arcs = "\n".join(
        f"- {name}: "
        + " / ".join(b.name for b in beats)
        for name, beats in ARC_LIBRARY.items()
    )
    return (
        "TASK: SELECT_ARC\n"
        "Choose the single best-fitting 6-beat narrative arc for this scene.\n\n"
        f"SCENE:\n{_scene_block(scene)}\n\n"
        f"DESCRIPTION: {description}\n\n"
        f"AVAILABLE ARCS:\n{arcs}\n\n"
        'Respond with ONLY JSON: {"arc": "<one of the arc names>", '
        '"reason": "<one line>"}'
    )


def _build_planning_prompt(scene: SceneModel, description: str, beats: list[Beat]) -> str:
    return (
        "TASK: WRITE_PLAN\n"
        "You are a director planning a 6-shot sequence from one source photo.\n\n"
        f"SCENE MODEL:\n{_scene_block(scene)}\n\n"
        f"DESCRIPTION: {description}\n\n"
        f"ARC BEATS (soft leans; one shot per beat, in order):\n{_beats_block(beats)}\n\n"
        f"{_PLAN_RULES}"
    )


def _repair_suffix(raw: str, err: Exception) -> str:
    return (
        "\n\n--- YOUR PREVIOUS RESPONSE WAS INVALID ---\n"
        f"Error: {err}\n"
        f"Previous response:\n{raw}\n"
        "Return corrected JSON only, matching the schema exactly."
    )


# --- Parsing helpers --------------------------------------------------------
def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        newline = s.find("\n")
        s = s[newline + 1:] if newline != -1 else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _resolve_render_class(value) -> RenderClass | None:
    if isinstance(value, RenderClass):
        return value
    if value is None:
        return None
    s = str(value).strip()
    try:
        return RenderClass[s.upper()]      # by member name, e.g. "FACE"
    except KeyError:
        pass
    try:
        return RenderClass(s.lower())      # by value, e.g. "face"
    except ValueError:
        return None


def _resolve_quality(value) -> QualityTier:
    if isinstance(value, QualityTier):
        return value
    try:
        return QualityTier(str(value).strip().lower())
    except (ValueError, AttributeError):
        return QualityTier.STANDARD


def _build_shots(raw: str, scene: SceneModel, beats: list[Beat]) -> list[Shot]:
    data = json.loads(_strip_fences(raw))
    shots_data = data["shots"] if isinstance(data, dict) else data
    if not isinstance(shots_data, list):
        raise PlanValidationError("expected a list of shots under 'shots'")

    shots: list[Shot] = []
    for i, sd in enumerate(shots_data):
        beat = beats[i] if i < len(beats) else beats[-1]
        notes: list[str] = []

        # render_class GUARD: resolve the proposed value; fall back to the
        # beat's lean (with a visible note) if it doesn't map to an enum member.
        render_class = _resolve_render_class(sd.get("render_class"))
        if render_class is None:
            render_class = beat.render_lean
            notes.append(
                f"render_class fallback: proposed {sd.get('render_class')!r} "
                f"unresolved, used beat lean {render_class.value}"
            )

        # DERIVE routing — never from the LLM.
        model, model_reason = route(render_class)
        min_drift = drift_threshold(render_class)
        if render_class != beat.render_lean:
            notes.append(
                f"render_class {render_class.value} differs from beat lean "
                f"{beat.render_lean.value}"
            )
        if notes:
            model_reason = f"{model_reason} | " + " | ".join(notes)

        # Clamp the proposed duration to the routed model's allowed set. Needs
        # the model, so it happens here (not in the LLM output). Log deviations
        # so they surface at plan review.
        proposed_duration = float(sd["duration_s"])
        duration_s = clamp_duration(model, proposed_duration)
        if duration_s != proposed_duration:
            log.info(
                "shot_%02d: duration clamped %gs -> %ds (%s)",
                i + 1,
                proposed_duration,
                duration_s,
                model.value,
            )

        shots.append(
            Shot(
                shot_id=f"shot_{i + 1:02d}",
                shot_name=sd["shot_name"],
                shot_style=sd["shot_style"],
                render_class=render_class,
                narrative_beat=sd.get("narrative_beat") or beat.name,
                model=model,
                model_reason=model_reason,
                camera_motion=sd["camera_motion"],
                motion_preset=normalize_motion(sd.get("motion_preset")),
                prompt=sd["prompt"],
                reference=Reference(
                    type=ReferenceType.SOURCE_PHOTO, source=scene.source_photo_path
                ),
                duration_s=duration_s,
                quality=_resolve_quality(sd.get("quality")),
                min_drift_score=min_drift,
            )
        )
    return shots


def _validate(shots: list[Shot]) -> None:
    # No total-duration bound: per-model caps can make [60,180] unsatisfiable
    # (e.g. six Veo shots max at 48s). Durations are clamped per model already.
    if len(shots) != 6:
        raise PlanValidationError(f"expected exactly 6 shots, got {len(shots)}")
    for s in shots:
        if not isinstance(s.render_class, RenderClass):
            raise PlanValidationError(f"{s.shot_id}: invalid render_class")


_PARSE_ERRORS = (
    json.JSONDecodeError,
    ValidationError,
    PlanValidationError,
    KeyError,
    TypeError,
    ValueError,
)


# --- Arc selection ----------------------------------------------------------
async def _select_arc(
    scene: SceneModel, description: str, provider: VisionProvider, arc_name: str | None
) -> tuple[str, str]:
    if arc_name is not None and arc_name in ARC_LIBRARY:
        return arc_name, "explicitly provided"

    # One LLM call to pick the arc; anything invalid/ambiguous -> default.
    try:
        raw = await provider.complete(scene.source_photo_path, _build_arc_prompt(scene, description))
        data = json.loads(_strip_fences(raw))
        choice = str(data.get("arc", "")).strip().lower()
        reason = str(data.get("reason", "")).strip()
        if choice in ARC_LIBRARY:
            return choice, reason or "LLM selection"
    except (*_PARSE_ERRORS, AttributeError):
        pass
    return DEFAULT_ARC, "fell back to default (no valid arc selected)"


# --- Public entrypoint ------------------------------------------------------
async def plan(
    scene: SceneModel,
    description: str,
    provider: VisionProvider,
    run_id: str,
    arc_name: str | None = None,
) -> list[Shot]:
    arc, reason = await _select_arc(scene, description, provider, arc_name)
    log.info("run %s: arc=%s (%s)", run_id, arc, reason)  # P10 plan-review checkpoint
    beats = get_arc(arc)

    image_path = scene.source_photo_path
    prompt = _build_planning_prompt(scene, description, beats)

    raw = await provider.complete(image_path, prompt)
    try:
        shots = _build_shots(raw, scene, beats)
        _validate(shots)
        return shots
    except _PARSE_ERRORS as err:
        raw2 = await provider.complete(image_path, prompt + _repair_suffix(raw, err))
        try:
            shots = _build_shots(raw2, scene, beats)
            _validate(shots)
            return shots
        except _PARSE_ERRORS as err2:
            raise PlanningError(
                f"planning failed after one repair attempt: {err2}\n"
                f"--- raw response ---\n{raw2}"
            ) from err2
