"""Mock Higgsfield client (STEP 3a).

No network, no credentials, no cost. Implements the HiggsfieldClient
Protocol so the whole pipeline — submit, poll-to-terminal, fetch, and
crucially reconcile — runs offline and in CI.

reconcile() is the crash-recovery path: after a crash in the SUBMITTING
window (open_attempt written, job_id not yet recorded), resume looks the job
up by idem_key to avoid double-submitting. The mock records every submit in
an internal dict so reconcile can find it — it MUST work.
"""

import asyncio

from directoragent.routing import estimate_cost
from directoragent.schema import Shot

# Polls before a job flips to succeeded (per job_id).
_RUNNING_POLLS = 2
# Small synthetic latency so awaits actually yield.
_LATENCY_S = 0.01


class MockHiggsfieldClient:
    def __init__(self) -> None:
        self._jobs: dict[str, str] = {}        # idem_key -> job_id
        self._poll_counts: dict[str, int] = {}  # job_id -> times polled

    async def submit(self, shot: Shot, idem_key: str) -> str:
        await asyncio.sleep(_LATENCY_S)
        # Idempotent on idem_key: a repeat submit returns the same job_id.
        job_id = self._jobs.get(idem_key)
        if job_id is None:
            job_id = f"mock-job-{idem_key}"
            self._jobs[idem_key] = job_id
        return job_id

    async def poll(self, job_id: str) -> str:
        await asyncio.sleep(_LATENCY_S)
        count = self._poll_counts.get(job_id, 0)
        self._poll_counts[job_id] = count + 1
        # "running" for the first _RUNNING_POLLS calls, then "succeeded".
        return "running" if count < _RUNNING_POLLS else "succeeded"

    async def fetch_result(self, job_id: str) -> str:
        await asyncio.sleep(_LATENCY_S)
        return f"mock://results/{job_id}.mp4"

    async def reconcile(self, idem_key: str) -> str | None:
        await asyncio.sleep(_LATENCY_S)
        return self._jobs.get(idem_key)

    async def preflight_cost(self, shot: Shot) -> float:
        # Mock cost units: the static COST_PER_SECOND table. No job submitted.
        return estimate_cost(shot.model, shot.duration_s)
