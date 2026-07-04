"""DirectorAgent — typed spine shared across all five phases.

Phase boundaries are Pydantic models so every handoff is validated.
Plan objects (SceneModel, Shot) are immutable once produced.
Execution log (Attempt) accumulates underneath each Shot.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# --- Routing categories -----------------------------------------------------
# render_class is the routing key. It deterministically selects a model
# (see routing.py) and a drift threshold. Keep this set tight so routing
# stays a lookup, not a decision.
class RenderClass(str, Enum):
    FACE = "face"                       # -> Soul v2 / Cinema
    COMPLEX_MOTION = "complex_motion"   # -> Kling 3.0
    ABSTRACT_FLUID = "abstract_fluid"   # -> Wan 2.6
    WIDE_ENVIRONMENT = "wide_environment"  # -> Veo 3.1


class Model(str, Enum):
    SEEDANCE_2 = "seedance_2"
    KLING_3 = "kling_3"
    WAN_2_6 = "wan_2_6"
    VEO_3_1 = "veo_3_1"


class QualityTier(str, Enum):
    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"


class ReferenceType(str, Enum):
    SOURCE_PHOTO = "source_photo"     # the single input photo
    PREVIOUS_SHOT = "previous_shot"   # chain off an earlier shot's output
    NONE = "none"


# --- Phase 1 output ---------------------------------------------------------
class SceneModel(BaseModel):
    """Typed scene model extracted by the Vision API (Phase 1)."""
    source_photo_path: str
    subject: str
    environment: str
    lighting: str
    mood: str
    objects: list[str] = Field(default_factory=list)
    color_palette: list[str] = Field(default_factory=list)


# --- Phase 2 output: the plan -----------------------------------------------
class Reference(BaseModel):
    type: ReferenceType
    source: str | None = None   # path, url, or upstream shot_id
    weight: float = 1.0


class Shot(BaseModel):
    """One shot in the 6-shot arc. Immutable once planned."""
    shot_id: str
    shot_name: str
    shot_style: str          # free LLM-chosen cinematic descriptor, no routing meaning
    render_class: RenderClass  # closed routing key -> model + drift threshold
    narrative_beat: str
    model: Model
    model_reason: str
    camera_motion: str
    motion_preset: str | None = None
    prompt: str
    reference: Reference
    duration_s: float
    quality: QualityTier = QualityTier.STANDARD
    min_drift_score: float   # set from DRIFT_THRESHOLDS[render_class]


# --- Phases 3 & 4: execution log --------------------------------------------
class AttemptStatus(str, Enum):
    PENDING = "pending"          # row created, not yet submitted
    SUBMITTING = "submitting"    # idem_key recorded, awaiting job_id (crash window)
    RUNNING = "running"          # job_id recorded, generation in flight
    SCORING = "scoring"          # result fetched, computing drift
    PASSED = "passed"            # drift >= min_drift_score (terminal, success)
    FAILED_DRIFT = "failed_drift"  # drift < min_drift_score (terminal, retryable)
    FAILED_ERROR = "failed_error"  # job/network error (terminal, retryable)


TERMINAL = {AttemptStatus.PASSED, AttemptStatus.FAILED_DRIFT, AttemptStatus.FAILED_ERROR}
RETRYABLE = {AttemptStatus.FAILED_DRIFT, AttemptStatus.FAILED_ERROR}
IN_FLIGHT = {AttemptStatus.RUNNING, AttemptStatus.SCORING}


class Attempt(BaseModel):
    attempt_id: str
    run_id: str
    shot_id: str
    attempt_number: int          # 1-indexed
    idem_key: str                # f"{run_id}:{shot_id}:{attempt_number}"
    status: AttemptStatus = AttemptStatus.PENDING
    job_id: str | None = None
    drift_score: float | None = None
    cost: float = 0.0
    result_url: str | None = None
    error: str | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None


# --- Run-level aggregate (powers resume) ------------------------------------
class RunStatus(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETE = "complete"
    ABORTED = "aborted"   # e.g. cost ceiling hit


class RunState(BaseModel):
    run_id: str
    status: RunStatus
    scene: SceneModel
    input_description: str          # stored for provenance; not re-used after planning
    shots: list[Shot]
    attempts: dict[str, list[Attempt]] = Field(default_factory=dict)  # shot_id -> attempts
    total_cost: float = 0.0
    created_at: datetime

    def latest_attempt(self, shot_id: str) -> Attempt | None:
        history = self.attempts.get(shot_id) or []
        return history[-1] if history else None


# --- Phase 5 output ---------------------------------------------------------
class ShotResult(BaseModel):
    shot: Shot
    result_url: str
    drift_score: float
    model: Model
    attempts_used: int
    first_try: bool
    cost: float


class Storyboard(BaseModel):
    run_id: str
    results: list[ShotResult]
    total_cost: float
    first_try_yield: float       # fraction of shots that passed on attempt 1
    mean_drift: float
