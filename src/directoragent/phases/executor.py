"""Phase 3 + 4 — the executor (STEP 7).

Fans out one coroutine per shot under a concurrency semaphore, submits each to
Higgsfield, polls to a terminal state, scores drift against the source photo,
and quality-retries on drift failure up to the budget.

Two retries, never conflated:
  - Transient retry (5xx/timeout/network) lives INSIDE the client adapter
    (tenacity); it never creates a new Attempt row.
  - Quality retry (drift below threshold) is the while-loop here; each pass is a
    NEW Attempt row, new idem_key, new cost.

Invariants honored:
  - open_attempt() is written BEFORE any network call (crash window).
  - add_cost() is called ONCE per real submission, right after record_job_id.
  - A PASSED shot is never re-submitted.
  - The executor reads shot.model / shot.min_drift_score (set by the planner)
    and NEVER calls the routing helpers (route / drift_threshold) itself.
  - Drift reference is ALWAYS the source photo.
  - motion_preset and render_class are passed through opaquely.

Budget accounting: one shared lock serializes every ceiling check across the
fan-out. A shot reserves its projected cost against the in-memory total under
the lock BEFORE submitting, so concurrent shots can't all pass the check on
the same stale number. state.total_cost therefore equals the DB total plus
outstanding reservations; add_cost() makes each reservation durable, and a
submission that fails before add_cost rolls its reservation back. The DB
total stays authoritative for reloads.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from directoragent.protocols import DriftScorer, HiggsfieldClient, StateStore
from directoragent.routing import (
    MAX_ATTEMPTS_PER_SHOT,
    MAX_CONCURRENT_JOBS,
)
from directoragent.schema import (
    IN_FLIGHT,
    Attempt,
    AttemptStatus,
    RunState,
    Shot,
)

POLL_INTERVAL_S = 0.5

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_all(
    state: RunState,
    store: StateStore,
    hf: HiggsfieldClient,
    scorer: DriftScorer,
    max_cost_usd: float,
) -> None:
    sem = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    # One lock shared by every shot: all budget checks/reservations serialize
    # on it, so the ceiling can't be overshot by concurrent stale reads.
    budget_lock = asyncio.Lock()
    await asyncio.gather(
        *(
            run_shot(shot, state, store, hf, scorer, sem, max_cost_usd, budget_lock)
            for shot in state.shots
        ),
        return_exceptions=False,
    )


async def run_shot(
    shot: Shot,
    state: RunState,
    store: StateStore,
    hf: HiggsfieldClient,
    scorer: DriftScorer,
    sem: asyncio.Semaphore,
    max_cost_usd: float,
    budget_lock: asyncio.Lock,
) -> None:
    async with sem:
        # --- Resume: re-attach to whatever the last attempt was doing -------
        last = state.latest_attempt(shot.shot_id)
        if last is not None:
            if last.status == AttemptStatus.PASSED:
                return  # never re-submit a passed shot
            if last.status in IN_FLIGHT:
                await _poll_to_terminal(last, store, hf, scorer, state, shot)
                if last.status == AttemptStatus.PASSED:
                    return
            elif last.status == AttemptStatus.SUBMITTING:
                # Crash window: a job may already exist for this idem_key.
                jid = await hf.reconcile(last.idem_key)
                if jid:
                    await store.record_job_id(last.attempt_id, jid)
                    last.job_id = jid
                    last.status = AttemptStatus.RUNNING
                    # The crash predates the original add_cost (which follows
                    # record_job_id), so this is the first and only charge for
                    # that submission — invariant #4 holds at exactly once.
                    async with budget_lock:
                        cost = await hf.preflight_cost(shot)
                        await store.add_cost(state.run_id, cost)
                        state.total_cost += cost
                    last.cost = cost
                    await store.update_attempt(last.attempt_id, cost=cost)
                    await _poll_to_terminal(last, store, hf, scorer, state, shot)
                    if last.status == AttemptStatus.PASSED:
                        return
                else:
                    # No job to recover: close the orphan so the ledger never
                    # holds a forever-SUBMITTING row. Nothing was charged. The
                    # loop below opens a fresh attempt.
                    now = _now()
                    error = "orphaned in crash window; reconcile found no match"
                    await store.update_attempt(
                        last.attempt_id,
                        status=AttemptStatus.FAILED_ERROR,
                        error=error,
                        completed_at=now,
                    )
                    last.status = AttemptStatus.FAILED_ERROR
                    last.error = error
                    last.completed_at = now

        # --- Quality-retry loop --------------------------------------------
        n = len(state.attempts.get(shot.shot_id, []))
        while n < MAX_ATTEMPTS_PER_SHOT:
            # ONE preflight per iteration; reused for the guard, add_cost, and
            # attempt.cost. preflight_cost MUST NOT submit a job. Check and
            # reservation happen atomically under the shared lock so parallel
            # shots can't all pass the ceiling on the same stale total.
            async with budget_lock:
                cost = await hf.preflight_cost(shot)
                if state.total_cost + cost > max_cost_usd:
                    log.warning(
                        "shot %s skipped: budget ceiling (%.2f + %.2f > %.2f)",
                        shot.shot_id, state.total_cost, cost, max_cost_usd,
                    )
                    break  # budget guard: leave the shot unfinished
                state.total_cost += cost  # reserve before submitting

            n += 1
            attempt = Attempt(
                attempt_id=uuid.uuid4().hex,
                run_id=state.run_id,
                shot_id=shot.shot_id,
                attempt_number=n,
                idem_key=f"{state.run_id}:{shot.shot_id}:{n}",
                status=AttemptStatus.SUBMITTING,
            )
            await store.open_attempt(attempt)             # BEFORE any network call
            state.attempts.setdefault(shot.shot_id, []).append(attempt)

            try:
                jid = await hf.submit(shot, attempt.idem_key)  # transient retry is inside the adapter
            except Exception as exc:
                # Submit failed past the adapter's retries: no job went out and
                # add_cost never ran, so release the reservation and close the
                # row before propagating.
                async with budget_lock:
                    state.total_cost -= cost
                now = _now()
                error = f"submit failed: {exc}"
                await store.update_attempt(
                    attempt.attempt_id,
                    status=AttemptStatus.FAILED_ERROR,
                    error=error,
                    completed_at=now,
                )
                attempt.status = AttemptStatus.FAILED_ERROR
                attempt.error = error
                attempt.completed_at = now
                raise
            await store.record_job_id(attempt.attempt_id, jid)
            attempt.job_id = jid
            attempt.status = AttemptStatus.RUNNING

            # ONCE per submission. The reservation above already counted this
            # cost in state.total_cost; this write makes it durable in the DB
            # (the in-memory total must NOT be overwritten from the DB return —
            # that would drop other shots' outstanding reservations).
            await store.add_cost(state.run_id, cost)
            attempt.cost = cost
            # Persist the per-attempt cost too, so a reloaded RunState (and the
            # assembled storyboard) attributes cost per shot faithfully. This is
            # NOT a second add_cost — the run total was already incremented once.
            await store.update_attempt(attempt.attempt_id, cost=cost)

            await _poll_to_terminal(attempt, store, hf, scorer, state, shot)
            if attempt.status == AttemptStatus.PASSED:
                return


async def _poll_to_terminal(
    attempt: Attempt,
    store: StateStore,
    hf: HiggsfieldClient,
    scorer: DriftScorer,
    state: RunState,
    shot: Shot,
) -> None:
    while True:
        status = await hf.poll(attempt.job_id)

        if status == "succeeded":
            # Mark SCORING (IN_FLIGHT) so a crash mid-score re-polls on resume.
            attempt.status = AttemptStatus.SCORING
            await store.update_attempt(attempt.attempt_id, status=AttemptStatus.SCORING)

            url = await hf.fetch_result(attempt.job_id)
            # Drift reference is ALWAYS the source photo.
            drift = await scorer.score(state.scene.source_photo_path, url)
            final = (
                AttemptStatus.PASSED
                if drift >= shot.min_drift_score
                else AttemptStatus.FAILED_DRIFT
            )
            now = _now()
            await store.update_attempt(
                attempt.attempt_id,
                drift_score=drift,
                result_url=url,
                status=final,
                completed_at=now,
            )
            attempt.drift_score = drift
            attempt.result_url = url
            attempt.status = final
            attempt.completed_at = now
            return

        if status == "failed":
            now = _now()
            await store.update_attempt(
                attempt.attempt_id,
                status=AttemptStatus.FAILED_ERROR,
                error="higgsfield job reported failed",
                completed_at=now,
            )
            attempt.status = AttemptStatus.FAILED_ERROR
            attempt.error = "higgsfield job reported failed"
            attempt.completed_at = now
            return

        await asyncio.sleep(POLL_INTERVAL_S)
