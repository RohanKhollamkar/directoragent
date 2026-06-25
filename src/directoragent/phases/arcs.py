"""Arc library (STEP 6) — plain data.

An "arc" is a fixed 6-beat narrative skeleton. Each beat carries a name, a
one-line intent, and a render_lean: a SOFT default RenderClass the planner
may follow or override per shot. render_lean is NOT binding — the LLM
proposes a render_class per shot and the planner guards it; the lean is only
the fallback and the "expected" shape for that beat.
"""

from dataclasses import dataclass

from directoragent.schema import RenderClass


@dataclass(frozen=True)
class Beat:
    name: str
    intent: str               # one line
    render_lean: RenderClass   # soft default, not binding


ARC_LIBRARY: dict[str, list[Beat]] = {
    "dramatic": [
        Beat("establish", "set the place", RenderClass.WIDE_ENVIRONMENT),
        Beat("introduce subject", "bring the subject in", RenderClass.FACE),
        Beat("rising detail", "build tension through detail", RenderClass.COMPLEX_MOTION),
        Beat("turn", "the pivot", RenderClass.FACE),
        Beat("peak", "height of the sequence", RenderClass.COMPLEX_MOTION),
        Beat("resolution", "settle and close", RenderClass.WIDE_ENVIRONMENT),
    ],
    "observational": [
        Beat("wide establish", "open on the whole space", RenderClass.WIDE_ENVIRONMENT),
        Beat("group framing", "frame the group within the space", RenderClass.WIDE_ENVIRONMENT),
        Beat("individual reaction", "catch one person's reaction", RenderClass.FACE),
        Beat("exchange detail", "hold on a detail of the exchange", RenderClass.FACE),
        Beat("pull back", "pull back to widen the perspective", RenderClass.COMPLEX_MOTION),
        Beat("environmental drift", "drift across the environment to close", RenderClass.WIDE_ENVIRONMENT),
    ],
    "reveal": [
        Beat("conceal", "hold the subject partly hidden", RenderClass.FACE),
        Beat("hint", "hint at what is coming", RenderClass.ABSTRACT_FLUID),
        Beat("approach", "move toward the subject", RenderClass.COMPLEX_MOTION),
        Beat("widening context", "widen to show the surrounding context", RenderClass.WIDE_ENVIRONMENT),
        Beat("the reveal", "reveal the subject fully", RenderClass.FACE),
        Beat("aftermath", "settle on the aftermath", RenderClass.WIDE_ENVIRONMENT),
    ],
    "mood-piece": [
        Beat("texture open", "open on texture and atmosphere", RenderClass.ABSTRACT_FLUID),
        Beat("subject in space", "place the subject within the space", RenderClass.WIDE_ENVIRONMENT),
        Beat("light/detail", "study light and detail on the subject", RenderClass.FACE),
        Beat("abstract interlude", "an abstract interlude", RenderClass.ABSTRACT_FLUID),
        Beat("subject return", "return to the subject", RenderClass.FACE),
        Beat("lingering close", "linger and close on the subject", RenderClass.FACE),
    ],
}

# Every arc is exactly six beats — the planner depends on this.
assert all(len(beats) == 6 for beats in ARC_LIBRARY.values())

DEFAULT_ARC = "dramatic"


def get_arc(name: str) -> list[Beat]:
    """Return the 6 beats for an arc, or raise KeyError if unknown."""
    return ARC_LIBRARY[name]
