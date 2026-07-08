"""SWEEP-FIX-1 — regression tests for the milestone integrity-sweep concerns.

  C1: plan_only on an EXISTING run prints the plan and executes nothing.
  C3: SUBMITTING→reconcile-success recovery charges the run exactly once and
      persists the per-attempt cost.
  C4: the budget guard is race-free — with a ceiling admitting only 2 of 6
      shots and a slow preflight, the concurrent fan-out never overshoots.
  C5: resuming a COMPLETE run never flips its status back to EXECUTING.
  C6: an orphaned SUBMITTING attempt (reconcile finds nothing) is closed
      FAILED_ERROR instead of dangling forever.
"""

import asyncio
from datetime import datetime, timezone

from directoragent import pipeline
from directoragent.clients.higgsfield_mock import MockHiggsfieldClient
from directoragent.config import Settings
from directoragent.drift.mock_scorer import MockDriftScorer
from directoragent.phases.executor import run_all
from directoragent.routing import estimate_cost
from directoragent.schema import (
    Attempt,
    AttemptStatus,
    RunState,
    RunStatus,
    Storyboard,
)
from directoragent.state.sqlite_store import SqliteStateStore
from tests.test_executor import SCENE, SpyStore, _shot, _state


def _settings(**over) -> Settings:
    base = dict(mock_mode=True, state_db_path=".directoragent/state.db", max_cost_usd=100.0)
    base.update(over)
    return Settings(**base)


async def _reload(settings: Settings, run_id: str) -> RunState:
    store = SqliteStateStore(settings.state_db_path)
    try:
        state = await store.load_run(run_id)
        assert state is not None
        return state
    finally:
        await store.close()


# --- C1: plan_only on an existing run executes nothing -----------------------
async def test_plan_only_on_existing_run_executes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = _settings()

    # A completed run with six passed attempts on the ledger.
    sb = await pipeline.run("assets/p.png", "noir", "runA", settings)
    assert isinstance(sb, Storyboard)
    before = await _reload(settings, "runA")
    counts_before = {k: len(v) for k, v in before.attempts.items()}

    # plan_only on the EXISTING run: returns None, no new attempts, no new
    # cost, no status write.
    out = await pipeline.run("assets/p.png", "noir", "runA", settings, plan_only=True)
    assert out is None
    after = await _reload(settings, "runA")
    assert {k: len(v) for k, v in after.attempts.items()} == counts_before
    assert after.total_cost == before.total_cost
    assert after.status == before.status == RunStatus.COMPLETE


async def test_plan_only_twice_stays_planning_with_no_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = _settings()

    assert await pipeline.run("assets/p.png", "noir", "runB", settings, plan_only=True) is None
    # The review-twice case that used to execute and spend (C1).
    assert await pipeline.run("assets/p.png", "noir", "runB", settings, plan_only=True) is None

    state = await _reload(settings, "runB")
    assert state.status == RunStatus.PLANNING
    assert state.attempts == {}
    assert state.total_cost == 0.0


# --- C3: reconcile recovery charges exactly once + persists attempt.cost -----
async def test_reconcile_recovery_charges_exactly_once_and_persists_cost(tmp_path):
    events = []
    inner_store = SqliteStateStore(str(tmp_path / "s.db"))
    store = SpyStore(inner_store, events)
    shot = _shot()
    state = await _state(store, shot)

    # Simulate a crash in the SUBMITTING window: the job exists "server-side"
    # (submitted under this idem_key) but the process died before
    # record_job_id/add_cost ran.
    hf = MockHiggsfieldClient()
    await hf.submit(shot, "run:shot_01:1")
    attempt = Attempt(
        attempt_id="orphan-1",
        run_id="run",
        shot_id="shot_01",
        attempt_number=1,
        idem_key="run:shot_01:1",
        status=AttemptStatus.SUBMITTING,
    )
    await store.open_attempt(attempt)
    state.attempts["shot_01"] = [attempt]

    await run_all(state, store, hf, MockDriftScorer(), max_cost_usd=100.0)

    expected = estimate_cost(shot.model, shot.duration_s)
    assert store.add_cost_calls == 1  # the recovery charge, and only it
    reloaded = await inner_store.load_run("run")
    recovered = reloaded.attempts["shot_01"][0]
    assert len(reloaded.attempts["shot_01"]) == 1  # no fresh submission
    assert recovered.status == AttemptStatus.PASSED
    assert recovered.cost == expected              # per-attempt cost persisted
    assert reloaded.total_cost == expected         # run total charged once
    assert state.total_cost == expected
    await inner_store.close()


# --- C4: budget guard is race-free under the concurrent fan-out --------------
class SlowPreflightHF:
    """Delegates to the mock but makes preflight slow and fixed-cost. Without
    the shared budget lock, all six shots would pass the ceiling check on the
    same stale total during the sleep and every one would submit."""

    def __init__(self, cost: float, delay: float = 0.05):
        self._inner = MockHiggsfieldClient()
        self._cost = cost
        self._delay = delay
        self.submits = 0

    async def submit(self, shot, idem_key):
        self.submits += 1
        return await self._inner.submit(shot, idem_key)

    async def poll(self, job_id):
        return await self._inner.poll(job_id)

    async def fetch_result(self, job_id):
        return await self._inner.fetch_result(job_id)

    async def reconcile(self, idem_key):
        return await self._inner.reconcile(idem_key)

    async def preflight_cost(self, shot):
        await asyncio.sleep(self._delay)
        return self._cost


async def test_budget_guard_never_overshoots_under_concurrency(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    shots = [_shot().model_copy(update={"shot_id": f"shot_{i:02d}"}) for i in range(1, 7)]
    state = RunState(
        run_id="run",
        status=RunStatus.EXECUTING,
        scene=SCENE,
        input_description="d",
        shots=shots,
        total_cost=0.0,
        created_at=datetime.now(timezone.utc),
    )
    await store.create_run(state)
    for s in shots:
        await store.save_shot("run", s)

    # Ceiling admits exactly 2 of the 6 unit-cost shots.
    hf = SlowPreflightHF(cost=1.0)
    await run_all(state, store, hf, MockDriftScorer(), max_cost_usd=2.0)

    assert hf.submits == 2
    assert state.total_cost <= 2.0
    reloaded = await store.load_run("run")
    assert reloaded.total_cost <= 2.0
    submitted = [sid for sid, atts in reloaded.attempts.items() if atts]
    assert len(submitted) == 2
    await store.close()


# --- C5: resuming a COMPLETE run never rewrites EXECUTING --------------------
async def test_resume_of_complete_run_stays_complete_throughout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = _settings()
    await pipeline.run("assets/p.png", "noir", "runC", settings)
    assert (await _reload(settings, "runC")).status == RunStatus.COMPLETE

    written: list[RunStatus] = []
    orig = SqliteStateStore.update_run_status

    async def spy(self, run_id, status):
        written.append(status)
        return await orig(self, run_id, status)

    monkeypatch.setattr(SqliteStateStore, "update_run_status", spy)

    sb = await pipeline.run("", "", "runC", settings)  # idempotent resume
    assert isinstance(sb, Storyboard)
    assert RunStatus.EXECUTING not in written  # never flipped back, even transiently
    assert (await _reload(settings, "runC")).status == RunStatus.COMPLETE


# --- C6: orphaned SUBMITTING attempt is closed FAILED_ERROR -------------------
async def test_orphaned_submitting_attempt_closed_failed_error(tmp_path):
    store = SqliteStateStore(str(tmp_path / "s.db"))
    shot = _shot()
    state = await _state(store, shot)

    # SUBMITTING attempt whose idem_key the (fresh) client has never seen:
    # reconcile returns None, so the row must be closed, not left dangling.
    attempt = Attempt(
        attempt_id="orphan-1",
        run_id="run",
        shot_id="shot_01",
        attempt_number=1,
        idem_key="run:shot_01:1",
        status=AttemptStatus.SUBMITTING,
    )
    await store.open_attempt(attempt)
    state.attempts["shot_01"] = [attempt]

    await run_all(state, store, MockHiggsfieldClient(), MockDriftScorer(), max_cost_usd=100.0)

    reloaded = await store.load_run("run")
    attempts = reloaded.attempts["shot_01"]
    assert attempts[0].status == AttemptStatus.FAILED_ERROR
    assert "orphaned in crash window" in (attempts[0].error or "")
    assert attempts[0].completed_at is not None
    assert attempts[0].cost == 0.0                  # nothing was charged for it
    # A fresh attempt was opened and passed.
    assert attempts[1].attempt_number == 2
    assert attempts[1].status == AttemptStatus.PASSED
    await store.close()
