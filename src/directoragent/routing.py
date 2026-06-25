"""Deterministic configuration: routing, drift thresholds, cost model.

This file is the reason "the planner" isn't an LLM decision for model
selection — render_class -> model is a lookup. The LLM's job in Phase 2 is to
assign render_classes and write prompts; the mapping to a model is mechanical
and auditable.
"""

from directoragent.schema import Model, RenderClass


# --- Model routing ----------------------------------------------------------
# render_class -> (model, reason). reason is stored on the Shot as model_reason
# so the storyboard metadata explains every routing choice.
ROUTING: dict[RenderClass, tuple[Model, str]] = {
    RenderClass.FACE: (
        Model.SOUL_V2,
        "Face-centric shot: Soul v2/Cinema for identity-faithful character rendering.",
    ),
    RenderClass.COMPLEX_MOTION: (
        Model.KLING_3,
        "Complex subject/camera motion: Kling 3.0 for motion coherence.",
    ),
    RenderClass.ABSTRACT_FLUID: (
        Model.WAN_2_6,
        "Abstract/fluid imagery: Wan 2.6 for non-rigid, fluid dynamics.",
    ),
    RenderClass.WIDE_ENVIRONMENT: (
        Model.VEO_3_1,
        "Wide environment establishing shot: Veo 3.1 for scene-scale fidelity.",
    ),
}


def route(render_class: RenderClass) -> tuple[Model, str]:
    return ROUTING[render_class]


# --- Drift thresholds (per render_class) ------------------------------------
# Tighter for faces (identity must hold), looser for abstract (more variance
# is acceptable). These become Shot.min_drift_score.
DRIFT_THRESHOLDS: dict[RenderClass, float] = {
    RenderClass.FACE: 0.78,
    RenderClass.COMPLEX_MOTION: 0.72,
    RenderClass.ABSTRACT_FLUID: 0.65,
    RenderClass.WIDE_ENVIRONMENT: 0.70,
}


def drift_threshold(render_class: RenderClass) -> float:
    return DRIFT_THRESHOLDS[render_class]


# --- Cost model -------------------------------------------------------------
# PLACEHOLDER numbers — calibrate against real Higgsfield pricing before
# trusting --max-cost. USD per second of generated video, per model.
COST_PER_SECOND: dict[Model, float] = {
    Model.SOUL_V2: 0.10,
    Model.KLING_3: 0.14,
    Model.WAN_2_6: 0.08,
    Model.VEO_3_1: 0.18,
}


def estimate_cost(model: Model, duration_s: float) -> float:
    return COST_PER_SECOND[model] * duration_s


# --- Retry budget -----------------------------------------------------------
MAX_ATTEMPTS_PER_SHOT = 3   # 1 initial + 2 quality-retries on drift failure
MAX_CONCURRENT_JOBS = 6     # semaphore width for the fan-out
