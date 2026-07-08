"""SWEEP-FIX-2 — real-adapter reconcile contract.

reconcile(idem_key, shot) derives its content fingerprint {catalog model id,
full generation prompt incl. motion phrase} from the Shot AT CALL TIME — no
in-memory state, so recovery survives a cross-process crash (every client
here is freshly constructed, never having submitted anything). Prompts are
identical across a shot's quality-retry attempts, so only EXACTLY ONE match
is unambiguous; zero or multiple matches return None (fresh submit fallback).
"""

from directoragent.clients.higgsfield import HiggsfieldClient
from directoragent.schema import (
    Model,
    Reference,
    ReferenceType,
    RenderClass,
    Shot,
)

SHOT = Shot(
    shot_id="shot_01",
    shot_name="n",
    shot_style="s",
    render_class=RenderClass.ABSTRACT_FLUID,
    narrative_beat="b",
    model=Model.WAN_2_6,
    model_reason="r",
    camera_motion="locked off",
    motion_preset="STATIC",
    prompt="ink blooms in dark water",
    reference=Reference(type=ReferenceType.SOURCE_PHOTO, source="https://cdn/ref.png"),
    duration_s=5,
    min_drift_score=0.65,
)

# The fingerprint the adapter derives from SHOT: catalog id + motion-folded prompt.
FP_MODEL = "wan2_6"
FP_PROMPT = "ink blooms in dark water Camera: locked-off static camera."


def _client_returning(generations: list[dict]) -> HiggsfieldClient:
    """Fresh client (no submit history) whose show_generations returns the
    given items in the live {"results": [...]} envelope."""

    async def call_tool(tool, **params):
        assert tool == "show_generations"
        return {"results": generations}

    return HiggsfieldClient(api_key="unused", call_tool=call_tool)


def _gen(job_id: str, model: str = FP_MODEL, prompt: str = FP_PROMPT) -> dict:
    return {"id": job_id, "params": {"model": model, "prompt": prompt}}


async def test_exactly_one_match_recovers_job_id_across_processes():
    # Fresh client — the fingerprint must come from the shot, not submit history.
    hf = _client_returning(
        [
            _gen("other", model="veo3_1", prompt="something else"),
            _gen("JOB-42"),
        ]
    )
    assert await hf.reconcile("run:shot_01:1", SHOT) == "JOB-42"


async def test_zero_matches_returns_none():
    hf = _client_returning([_gen("other", prompt="a different prompt")])
    assert await hf.reconcile("run:shot_01:1", SHOT) is None


async def test_multiple_matches_are_ambiguous_and_return_none(caplog):
    # Two generations with the same fingerprint — e.g. attempt 1 (drift-failed)
    # and the orphaned attempt 2 share an identical prompt. Ambiguous -> None.
    hf = _client_returning([_gen("JOB-1"), _gen("JOB-2")])
    with caplog.at_level("WARNING"):
        assert await hf.reconcile("run:shot_01:2", SHOT) is None
    assert any("ambiguous" in r.message for r in caplog.records)


async def test_prompt_fingerprint_includes_motion_phrase():
    # A generation matching the RAW prompt (motion phrase missing) must NOT
    # match — submit() always sends the motion-folded prompt.
    hf = _client_returning([_gen("JOB-RAW", prompt="ink blooms in dark water")])
    assert await hf.reconcile("run:shot_01:1", SHOT) is None
