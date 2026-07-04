"""The four seams of DirectorAgent, as Protocols.

Each has a real implementation and a mock. The mocks are what make the
repo clone-and-run with zero credentials and zero cost, and what let CI
exercise the whole pipeline. Program against these, never against a
concrete client.
"""

from typing import Protocol

from directoragent.schema import Attempt, RunState, SceneModel, Shot


# --- Phase 1 -----------------------------------------------------------------
class VisionClient(Protocol):
    async def extract_scene(self, photo_path: str) -> SceneModel:
        """Photo -> typed scene model."""
        ...


# --- Phase 3: the only thing that costs real money ---------------------------
class HiggsfieldClient(Protocol):
    """Submit/poll/fetch against Higgsfield MCP.

    NOTE: the real adapter wraps whatever MCP tools Higgsfield exposes;
    these signatures are the shape DirectorAgent wants, and the adapter's
    job is to map onto the actual tool names. The mock implements this
    directly. `idem_key` lets submit be safely retried after a crash.
    """
    async def submit(self, shot: Shot, idem_key: str) -> str:
        """Submit a generation job; return job_id. Must be idempotent on idem_key."""
        ...

    async def poll(self, job_id: str) -> str:
        """Return job status string: 'running' | 'succeeded' | 'failed'."""
        ...

    async def fetch_result(self, job_id: str) -> str:
        """Return the result asset URL/path for a succeeded job."""
        ...

    async def reconcile(self, idem_key: str) -> str | None:
        """Look up an existing job by idem_key (used on restart to avoid
        double-submitting after a crash in the SUBMITTING window).
        Return job_id if one exists, else None."""
        ...

    async def preflight_cost(self, shot: Shot) -> float:
        """Projected cost of ONE generation of this shot, in the client's native
        cost units (real adapter: Higgsfield credits via get_cost; mock: static
        table units). MUST NOT submit a job."""
        ...


# --- Phase 4 -----------------------------------------------------------------
class DriftScorer(Protocol):
    """CLIP cosine similarity between source reference and generated frame.

    Real impl lazy-loads open_clip + torch (so importing the package never
    drags in torch). Mock returns synthetic scores so --mock skips torch
    entirely.
    """
    async def score(self, reference_path: str, candidate_url: str) -> float:
        ...


# --- Durability: Level-1 job ledger -----------------------------------------
class StateStore(Protocol):
    """Persists exactly what's expensive to lose: scene, plan, and the
    per-attempt job ledger. SQLite by default, Postgres swappable behind
    this interface. Everything else in the pipeline is stateless and
    cheap to recompute.
    """
    async def create_run(self, state: RunState) -> None: ...
    async def save_shot(self, run_id: str, shot: Shot) -> None: ...

    # Attempt lifecycle — write BEFORE the network call, update AFTER.
    async def open_attempt(self, attempt: Attempt) -> None: ...        # status=SUBMITTING
    async def record_job_id(self, attempt_id: str, job_id: str) -> None: ...  # -> RUNNING
    async def update_attempt(self, attempt_id: str, **fields) -> None: ...

    async def load_run(self, run_id: str) -> RunState | None: ...      # powers re-attach
    async def add_cost(self, run_id: str, delta: float) -> float: ...  # returns new total
