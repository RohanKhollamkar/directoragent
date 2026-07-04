"""Executor behavior (STEP 11) — quality retry, resume, ordering, cost-once."""

from datetime import datetime, timezone

from directoragent.clients.higgsfield_mock import MockHiggsfieldClient
from directoragent.drift.mock_scorer import MockDriftScorer
from directoragent.phases.executor import run_all
from directoragent.schema import (
    AttemptStatus,
    Model,
    QualityTier,
    Reference,
    ReferenceType,
    RenderClass,
    RunState,
    RunStatus,
    SceneModel,
    Shot,
)
from directoragent.state.sqlite_store import SqliteStateStore

SCENE = SceneModel(
    source_photo_path="assets/p.png",
    subject="figure",
    environment="street",
    lighting="neon",
    mood="noir",
    objects=[],
    color_palette=[],
)


def _shot():
    return Shot(
        shot_id="shot_01",
        shot_name="n",
        shot_style="s",
        render_class=RenderClass.FACE,
        narrative_beat="b",
        model=Model.SEEDANCE_2,
        model_reason="r",
        camera_motion="c",
        motion_preset="STATIC",
        prompt="p",
        reference=Reference(type=ReferenceType.SOURCE_PHOTO, source="assets/p.png"),
        duration_s=10,
        quality=QualityTier.STANDARD,
        min_drift_score=0.78,  # FACE threshold
    )


async def _state(store, shot):
    state = RunState(
        run_id="run",
        status=RunStatus.EXECUTING,
        scene=SCENE,
        input_description="d",
        shots=[shot],
        total_cost=0.0,
        created_at=datetime.now(timezone.utc),
    )
    await store.create_run(state)
    await store.save_shot("run", shot)
    return state


class SpyStore:
    """Delegates to a real store, recording open_attempt/add_cost for assertions."""

    def __init__(self, inner, events):
        self._inner = inner
        self.events = events
        self.add_cost_calls = 0

    async def create_run(self, state):
        return await self._inner.create_run(state)

    async def save_shot(self, run_id, shot):
        return await self._inner.save_shot(run_id, shot)

    async def open_attempt(self, attempt):
        self.events.append(("open_attempt", attempt.idem_key))
        return await self._inner.open_attempt(attempt)

    async def record_job_id(self, attempt_id, job_id):
        return await self._inner.record_job_id(attempt_id, job_id)

    async def update_attempt(self, attempt_id, **fields):
        return await self._inner.update_attempt(attempt_id, **fields)

    async def add_cost(self, run_id, delta):
        self.add_cost_calls += 1
        self.events.append(("add_cost", delta))
        return await self._inner.add_cost(run_id, delta)

    async def load_run(self, run_id):
        return await self._inner.load_run(run_id)

    async def close(self):
        return await self._inner.close()


class SpyHF:
    def __init__(self, inner, events):
        self._inner = inner
        self.events = events
        self.submits = 0
        self.polls = 0

    async def submit(self, shot, idem_key):
        self.submits += 1
        self.events.append(("submit", idem_key))
        return await self._inner.submit(shot, idem_key)

    async def poll(self, job_id):
        self.polls += 1
        return await self._inner.poll(job_id)

    async def fetch_result(self, job_id):
        return await self._inner.fetch_result(job_id)

    async def reconcile(self, idem_key):
        return await self._inner.reconcile(idem_key)

    async def preflight_cost(self, shot):
        return await self._inner.preflight_cost(shot)


async def test_drift_fail_creates_second_attempt_and_passes(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    state = await _state(store, _shot())
    # fail_first=1 -> first score below threshold, second clears it.
    await run_all(state, store, MockHiggsfieldClient(), MockDriftScorer(fail_first=1), max_cost_usd=100.0)

    attempts = state.attempts["shot_01"]
    assert len(attempts) == 2
    assert attempts[0].status == AttemptStatus.FAILED_DRIFT
    assert attempts[0].attempt_number == 1
    assert attempts[1].status == AttemptStatus.PASSED
    assert attempts[1].attempt_number == 2
    await store.close()


async def test_passed_shot_not_resubmitted_on_resume(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    state = await _state(store, _shot())
    await run_all(state, store, MockHiggsfieldClient(), MockDriftScorer(), max_cost_usd=100.0)
    assert state.latest_attempt("shot_01").status == AttemptStatus.PASSED
    before = len(state.attempts["shot_01"])

    # Resume on the already-complete state with a spy: no submit should happen.
    events = []
    spy_hf = SpyHF(MockHiggsfieldClient(), events)
    await run_all(state, store, spy_hf, MockDriftScorer(), max_cost_usd=100.0)
    assert spy_hf.submits == 0
    assert len(state.attempts["shot_01"]) == before
    await store.close()


async def test_open_attempt_before_submit_and_add_cost_once_per_submission(tmp_path):
    events = []
    inner_store = SqliteStateStore(str(tmp_path / "s.db"))
    store = SpyStore(inner_store, events)
    state = await _state(store, _shot())

    spy_hf = SpyHF(MockHiggsfieldClient(), events)
    # fail_first=1 -> exactly two submissions.
    await run_all(state, store, spy_hf, MockDriftScorer(fail_first=1), max_cost_usd=100.0)

    # Ordering: for every submit, the matching open_attempt came first.
    opens = [e for e in events if e[0] == "open_attempt"]
    submits = [e for e in events if e[0] == "submit"]
    assert len(opens) == 2 and len(submits) == 2
    for idem in (f"run:shot_01:{n}" for n in (1, 2)):
        assert events.index(("open_attempt", idem)) < events.index(("submit", idem))

    # add_cost is called once per submission — not per poll.
    assert spy_hf.submits == 2
    assert store.add_cost_calls == 2
    assert spy_hf.polls > 2  # polling happened many times, but cost stayed at 2
    await inner_store.close()
