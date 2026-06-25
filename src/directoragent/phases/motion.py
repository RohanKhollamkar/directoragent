"""Provisional camera-motion preset vocabulary (STEP 6).

The planner asks the LLM for a motion_preset per shot and normalizes it to
one of these. camera_motion stays free prose; motion_preset is the closed
token a downstream renderer can map to a real control.
"""

# TODO(P12): reconcile these provisional presets against the real Higgsfield
# motion vocabulary; rename to match if needed.
MOTION_PRESETS = frozenset(
    {
        "PUSH_IN",
        "PULL_OUT",
        "PAN_LEFT",
        "PAN_RIGHT",
        "TILT_UP",
        "TILT_DOWN",
        "ORBIT",
        "STATIC",
        "HANDHELD",
        "CRANE",
    }
)


def normalize_motion(value) -> str:
    """Upper/trim the value (tolerating '-'/' ' separators) and return a valid
    preset, else 'STATIC'."""
    if value is None:
        return "STATIC"
    candidate = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    return candidate if candidate in MOTION_PRESETS else "STATIC"
