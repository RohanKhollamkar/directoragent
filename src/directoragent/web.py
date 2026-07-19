"""HTTP surface (STEP D3a) — a thin FastAPI translation of the CLI.

Exposes the existing plan-review lifecycle over HTTP, mapping 1:1 onto what
cli.py already drives through pipeline.run():

    POST /plan                -> `da run --review`  (plan only, no spend)
    POST /runs/{id}/execute   -> `da resume <id>`   (202; runs in background)
    GET  /runs/{id}           -> `da status <id>`   (poll endpoint)
    GET  /runs                -> `da list`
    GET  /healthz             -> deploy health check

This layer adds NO pipeline logic: provider/adapter selection stays in
pipeline.run() (the single composition root); the plan payload is captured via
pipeline's on_plan sink rather than recomputed here. Mock mode is the default
demo posture — real mode activates only when the REST credentials pipeline.py's
transport gate requires are present.

Execution runs on a daemon thread with its own asyncio.run() loop: a real run
takes minutes, and a thread survives any request-scoped event loop (uvicorn or
TestClient). The pipeline's own persistence + resumability make this safe — if
the process dies mid-run, `da resume` / POST execute picks it back up.

`import directoragent.web` must stay torch-free (mock adapters only; real
adapters are imported lazily inside pipeline.run's non-mock branch).
"""

import asyncio
import base64
import binascii
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from directoragent import pipeline
from directoragent.config import Settings
from directoragent.phases.arcs import ARC_LIBRARY
from directoragent.pipeline import CostCeilingError, _infer_arc
from directoragent.schema import AttemptStatus, RunState
from directoragent.state.sqlite_store import SqliteStateStore

app = FastAPI(
    title="DirectorAgent",
    description="One photo + scene description -> 6-shot production storyboard.",
    version="0.1.0",
)

# Bundled demo photo: /plan works with just a description. Resolved from the
# repo layout (src/directoragent/web.py -> repo root /assets/test.png).
DEFAULT_PHOTO = Path(__file__).resolve().parents[2] / "assets" / "test.png"

# run_id -> executing thread. Guards double-execute; entries are pruned on
# inspection (threads are daemons — the DB, not this dict, is durable truth).
_EXECUTING: dict[str, threading.Thread] = {}


def _settings(provider: str | None = None) -> Settings:
    """Same Settings the CLI builds, with the demo default flipped to mock.

    Real mode activates only when the REST credentials that pipeline.run's
    transport gate (_build_higgsfield_transport) requires are configured —
    the selection itself still happens inside pipeline.run.
    """
    overrides: dict = {}
    if provider is not None:
        overrides["vision_provider"] = provider
    settings = Settings(**overrides)
    if not (settings.higgsfield_key_id and settings.higgsfield_key_secret):
        settings = settings.model_copy(update={"mock_mode": True})
    return settings


async def _load_run(run_id: str, settings: Settings) -> RunState | None:
    store = SqliteStateStore(settings.state_db_path)
    try:
        return await store.load_run(run_id)
    finally:
        await store.close()


# --- API models (thin HTTP shapes; internal models live in schema.py) --------
class PlanRequest(BaseModel):
    description: str
    photo_url: str | None = None     # public URL (REST media_import_url path)
    photo: str | None = None         # local path or base64 image bytes
    arc: str | None = None
    provider: str | None = None      # vision provider override


class PlanShot(BaseModel):
    shot_id: str
    shot_style: str
    render_class: str
    model: str
    duration: float
    projected_cost: float


class PlanResponse(BaseModel):
    run_id: str
    status: str
    arc: str | None
    shots: list[PlanShot]
    projected_total: float


class ExecuteResponse(BaseModel):
    run_id: str
    status: str


class ShotStatus(BaseModel):
    shot_id: str
    shot_name: str
    shot_style: str
    render_class: str
    model: str
    duration: float
    latest_status: str
    drift_score: float | None
    attempts: int
    cost: float
    result_url: str | None


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    shots: list[ShotStatus]
    total_cost: float
    first_try_yield: float


class RunListEntry(BaseModel):
    run_id: str
    status: str
    cost: float


class HealthResponse(BaseModel):
    status: str = Field(default="ok")


# --- photo input --------------------------------------------------------------
def _resolve_photo(req: PlanRequest, settings: Settings) -> str:
    """URL passes straight through (the adapters' media_import_url path);
    a local path is used as-is; base64 is materialized next to the state DB.
    No photo at all falls back to the bundled demo asset."""
    if req.photo_url:
        return req.photo_url
    if req.photo:
        if Path(req.photo).exists():
            return req.photo
        try:
            data = base64.b64decode(req.photo, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(
                status_code=400,
                detail="photo is neither an existing path nor valid base64",
            )
        dest = Path(settings.state_db_path).parent / f"upload_{uuid.uuid4().hex}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(dest)
    if DEFAULT_PHOTO.exists():
        return str(DEFAULT_PHOTO)
    raise HTTPException(
        status_code=400,
        detail="no photo given and the bundled demo photo is missing; "
        "pass photo_url or photo",
    )


# --- endpoints ----------------------------------------------------------------
@app.post("/plan", response_model=PlanResponse)
async def plan(req: PlanRequest) -> PlanResponse:
    """`da run --review` over HTTP: plan, persist as PLANNING, stop before
    any spend. Execute later via POST /runs/{run_id}/execute."""
    if req.arc is not None and req.arc not in ARC_LIBRARY:
        raise HTTPException(
            status_code=400,
            detail=f"unknown arc {req.arc!r}; choose from: {', '.join(ARC_LIBRARY)}",
        )
    settings = _settings(req.provider)
    photo = _resolve_photo(req, settings)
    run_id = uuid.uuid4().hex

    captured: dict = {}

    def collect(state: RunState, projected: float, per_shot_costs: list[float]) -> None:
        captured["state"] = state
        captured["projected"] = projected
        captured["per_shot_costs"] = per_shot_costs

    try:
        await pipeline.run(
            photo, req.description, run_id, settings,
            arc_name=req.arc, plan_only=True, on_plan=collect,
        )
    except CostCeilingError as e:
        raise HTTPException(status_code=400, detail=str(e))

    state: RunState = captured["state"]
    return PlanResponse(
        run_id=run_id,
        status=state.status.value,
        arc=_infer_arc(state.shots),
        shots=[
            PlanShot(
                shot_id=s.shot_id,
                shot_style=s.shot_style,
                render_class=s.render_class.value,
                model=s.model.value,
                duration=s.duration_s,
                projected_cost=cost,
            )
            for s, cost in zip(state.shots, captured["per_shot_costs"])
        ],
        projected_total=captured["projected"],
    )


def _execute_in_thread(run_id: str, settings: Settings) -> None:
    # Own loop, own store connection — independent of any request event loop.
    # Errors land in the persisted attempt rows / run status, which is what
    # GET /runs/{run_id} reports; a crashed run is resumable by design.
    asyncio.run(pipeline.run("", "", run_id, settings))


@app.post("/runs/{run_id}/execute", response_model=ExecuteResponse, status_code=202)
async def execute(run_id: str) -> ExecuteResponse:
    """`da resume` over HTTP: kick off execution in the background and return
    202 immediately — poll GET /runs/{run_id} for progress."""
    settings = _settings()
    state = await _load_run(run_id, settings)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    thread = _EXECUTING.get(run_id)
    if thread is not None and thread.is_alive():
        raise HTTPException(status_code=409, detail=f"run {run_id} is already executing")

    thread = threading.Thread(
        target=_execute_in_thread, args=(run_id, settings), daemon=True
    )
    _EXECUTING[run_id] = thread
    thread.start()
    return ExecuteResponse(run_id=run_id, status="executing")


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
async def run_status(run_id: str) -> RunStatusResponse:
    """`da status` over HTTP — the poll endpoint."""
    state = await _load_run(run_id, _settings())
    if state is None:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    shots = []
    first_try_passes = 0
    for shot in state.shots:
        history = state.attempts.get(shot.shot_id) or []
        last = history[-1] if history else None
        if history and history[0].status == AttemptStatus.PASSED:
            first_try_passes += 1
        shots.append(
            ShotStatus(
                shot_id=shot.shot_id,
                shot_name=shot.shot_name,
                shot_style=shot.shot_style,
                render_class=shot.render_class.value,
                model=shot.model.value,
                duration=shot.duration_s,
                latest_status=last.status.value if last else "pending",
                drift_score=last.drift_score if last else None,
                attempts=len(history),
                cost=sum(a.cost for a in history),
                result_url=last.result_url if last else None,
            )
        )
    return RunStatusResponse(
        run_id=state.run_id,
        status=state.status.value,
        shots=shots,
        total_cost=state.total_cost,
        first_try_yield=first_try_passes / len(state.shots) if state.shots else 0.0,
    )


@app.get("/runs", response_model=list[RunListEntry])
async def list_runs() -> list[RunListEntry]:
    """`da list` over HTTP."""
    settings = _settings()
    store = SqliteStateStore(settings.state_db_path)
    try:
        rows = await store.list_runs()
    finally:
        await store.close()
    return [RunListEntry(run_id=r, status=s, cost=c) for r, s, c in rows]


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()
