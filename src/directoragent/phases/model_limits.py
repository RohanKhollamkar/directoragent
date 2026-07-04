"""Real Higgsfield catalog duration constraints, per Model (STEP 12.1b).

NON-FROZEN: keep in sync with models_explore output. The planner clamps each
shot's requested duration to its routed model's allowed value(s) AFTER routing
assigns the model (the clamp needs the model, so it can't be an LLM decision).
"""

from directoragent.schema import Model

MODEL_DURATIONS: dict[Model, dict] = {
    Model.SEEDANCE_2: {"min": 4, "max": 15, "allowed": None},   # continuous 4-15
    Model.KLING_3:    {"min": 3, "max": 15, "allowed": None},   # continuous 3-15
    Model.WAN_2_6:    {"allowed": [5, 10, 15]},                 # discrete
    Model.VEO_3_1:    {"allowed": [4, 6, 8]},                   # discrete
}


def clamp_duration(model: Model, requested: float) -> int:
    """Snap a requested duration to the model's nearest allowed value."""
    spec = MODEL_DURATIONS[model]
    if spec.get("allowed"):
        return min(spec["allowed"], key=lambda a: abs(a - requested))
    return int(max(spec["min"], min(spec["max"], round(requested))))
