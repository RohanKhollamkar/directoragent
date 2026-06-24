"""Deterministic configuration: routing, drift thresholds, cost model.

This file is the reason "the planner" isn't an LLM decision for model
selection — shot_type -> model is a lookup. The LLM's job in Phase 2 is to
assign shot_types and write prompts; the mapping to a model is mechanical
and auditable.
"""

from directoragent.schema import Model, ShotType


# --- Model routing ----------------------------------------------------------
# shot_type -> (model, reason). reason is stored on the Shot as model_reason
# so the storyboard metadata explains every routing choice.
ROUTING: dict[ShotType, tuple[Model, str]] = {
    ShotType.FACE: (
        Model.SOUL_V2,
        "Face-centric shot: Soul v2/Cinema for identity-faithful character rendering.",
    ),
    ShotType.COMPLEX_MOTION: (
        Model.KLING_3,
        "Complex subject/camera motion: Kling 3.0 for motion coherence.",
    ),
    ShotType.ABSTRACT_FLUID: (
        Model.WAN_2_6,
        "Abstract/fluid imagery: Wan 2.6 for non-rigid, fluid dynamics.",
    ),
    ShotType.WIDE_ENVIRONMENT: (
        Model.VEO_3_1,
        "Wide environment establishing shot: Veo 3.1 for scene-scale fidelity.",
    ),
}


def route(shot_type: ShotType) -> tuple[Model, str]:
    return ROUTING[shot_type]


# --- Drift thresholds (per shot_type) ---------------------------------------
# Tighter for faces (identity must hold), looser for abstract (more variance
# is acceptable). These become Shot.min_drift_score.
DRIFT_THRESHOLDS: dict[ShotType, float] = {
    ShotType.FACE: 0.78,
    ShotType.COMPLEX_MOTION: 0.72,
    ShotType.ABSTRACT_FLUID: 0.65,
    ShotType.WIDE_ENVIRONMENT: 0.70,
}


def drift_threshold(shot_type: ShotType) -> float:
    return DRIFT_THRESHOLDS[shot_type]


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
