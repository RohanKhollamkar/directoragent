"""MockPlanProvider (STEP 6) — a planner-shaped VisionProvider.

MockVisionProvider returns a SCENE and cannot serve the planner, so this mock
returns the two things the planner asks a provider for, keyed off the task
markers the planner embeds in its prompts:
  - SELECT_ARC  -> a valid arc choice JSON.
  - WRITE_PLAN  -> a canned, valid 6-shot plan JSON (dramatic arc), total 120s.
No network, no credentials. Non-frozen.
"""

import json

_ARC_CHOICE = {"arc": "dramatic", "reason": "canned: dramatic suits a single-subject scene"}

# Six shots aligned to the dramatic arc beats; durations sum to 120 (in [60,180]).
_PLAN = {
    "shots": [
        {
            "shot_name": "Cold open",
            "shot_style": "anamorphic wide, neon-soaked",
            "render_class": "WIDE_ENVIRONMENT",
            "narrative_beat": "establish",
            "camera_motion": "slow push toward the street",
            "motion_preset": "PUSH_IN",
            "prompt": "Wide establishing shot of a rain-slicked neon street at night; "
                      "puddles mirror magenta and cyan signs; a slow push-in down the "
                      "empty sidewalk; wet reflections, deep shadows.",
            "duration_s": 18,
            "quality": "standard",
        },
        {
            "shot_name": "Arrival",
            "shot_style": "intimate portrait",
            "render_class": "FACE",
            "narrative_beat": "introduce subject",
            "camera_motion": "locked-off close framing",
            "motion_preset": "STATIC",
            "prompt": "Medium close-up of the figure in a long coat stepping into frame; "
                      "eyes scanning right; neon rim light along the jaw; steam rising "
                      "behind from a grate.",
            "duration_s": 20,
            "quality": "standard",
        },
        {
            "shot_name": "Pursuit detail",
            "shot_style": "kinetic handheld",
            "render_class": "COMPLEX_MOTION",
            "narrative_beat": "rising detail",
            "camera_motion": "handheld tracking alongside",
            "motion_preset": "HANDHELD",
            "prompt": "Handheld tracking shot following the coat hem and quick boots over "
                      "wet asphalt; reflections smear; signage streaks past; cold blue light.",
            "duration_s": 22,
            "quality": "standard",
        },
        {
            "shot_name": "The look",
            "shot_style": "tense portrait",
            "render_class": "FACE",
            "narrative_beat": "turn",
            "camera_motion": "slow push-in on the face",
            "motion_preset": "PUSH_IN",
            "prompt": "Close-up, slow push-in on the figure's face as it stops; jaw "
                      "tightening, gaze fixing forward; single overhead light, deep "
                      "shadow behind.",
            "duration_s": 20,
            "quality": "high",
        },
        {
            "shot_name": "Crescendo",
            "shot_style": "orbiting spectacle",
            "render_class": "COMPLEX_MOTION",
            "narrative_beat": "peak",
            "camera_motion": "orbit around the subject",
            "motion_preset": "ORBIT",
            "prompt": "Orbiting shot circling the figure at the intersection; umbrellas and "
                      "headlights sweep past; neon smears into arcs; rain catches the light.",
            "duration_s": 22,
            "quality": "high",
        },
        {
            "shot_name": "Quiet out",
            "shot_style": "receding wide",
            "render_class": "WIDE_ENVIRONMENT",
            "narrative_beat": "resolution",
            "camera_motion": "pull back to reveal the empty street",
            "motion_preset": "PULL_OUT",
            "prompt": "Wide pull-out as the figure walks away down the neon corridor; the "
                      "street empties; reflections settle; the signs hum in cyan and magenta.",
            "duration_s": 18,
            "quality": "standard",
        },
    ]
}


class MockPlanProvider:
    async def complete(self, image_path: str, prompt: str) -> str:
        if "SELECT_ARC" in prompt:
            return json.dumps(_ARC_CHOICE)
        return json.dumps(_PLAN)
