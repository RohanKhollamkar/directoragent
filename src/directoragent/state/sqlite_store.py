"""SQLite StateStore (STEP 2).

Level-1 durability: scene, plan, and the per-attempt job ledger. Backed by a
single long-lived aiosqlite connection opened in autocommit mode, so each
ledger write lands on disk immediately — open_attempt() must be durable
*before* the network call it precedes (invariant #2), and reconcile() relies
on that on resume.

Implements the StateStore Protocol (protocols.py) against schema.sql.
"""

import asyncio
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import aiosqlite

from directoragent.schema import (
    Attempt,
    AttemptStatus,
    Reference,
    RunState,
    RunStatus,
    SceneModel,
    Shot,
)

# schema.sql lives one directory up (src/directoragent/schema.sql).
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# Columns update_attempt() is allowed to set. Whitelisted so the dynamic
# UPDATE can never be driven by an arbitrary caller-supplied key.
_UPDATABLE = {
    "status",
    "job_id",
    "drift_score",
    "cost",
    "result_url",
    "error",
    "submitted_at",
    "completed_at",
}


def _adapt(value):
    """Map Python values to their SQLite storage form (enum -> value,
    datetime -> isoformat); everything else passes through unchanged."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteStateStore:
    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _connection(self) -> aiosqlite.Connection:
        """Open the connection on first use, reuse it thereafter."""
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                    conn = await aiosqlite.connect(self._path, isolation_level=None)
                    conn.row_factory = aiosqlite.Row
                    await conn.execute("PRAGMA busy_timeout=5000")
                    await conn.execute("PRAGMA foreign_keys=ON")
                    await conn.executescript(_SCHEMA_PATH.read_text())
                    self._conn = conn
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- Writes -------------------------------------------------------------
    async def create_run(self, state: RunState) -> None:
        conn = await self._connection()
        await conn.execute(
            "INSERT INTO runs (run_id, status, scene_json, input_description, "
            "total_cost, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                state.run_id,
                state.status.value,
                state.scene.model_dump_json(),
                state.input_description,
                state.total_cost,
                state.created_at.isoformat(),
            ),
        )

    async def save_shot(self, run_id: str, shot: Shot) -> None:
        conn = await self._connection()
        await conn.execute(
            "INSERT INTO shots (run_id, shot_id, shot_name, shot_type, "
            "narrative_beat, model, model_reason, camera_motion, motion_preset, "
            "prompt, reference_json, duration_s, quality, min_drift_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                shot.shot_id,
                shot.shot_name,
                shot.shot_type.value,
                shot.narrative_beat,
                shot.model.value,
                shot.model_reason,
                shot.camera_motion,
                shot.motion_preset,
                shot.prompt,
                shot.reference.model_dump_json(),
                shot.duration_s,
                shot.quality.value,
                shot.min_drift_score,
            ),
        )

    async def open_attempt(self, attempt: Attempt) -> None:
        conn = await self._connection()
        await conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, shot_id, attempt_number, "
            "idem_key, status, job_id, drift_score, cost, result_url, error, "
            "submitted_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.attempt_id,
                attempt.run_id,
                attempt.shot_id,
                attempt.attempt_number,
                attempt.idem_key,
                AttemptStatus.SUBMITTING.value,
                attempt.job_id,
                attempt.drift_score,
                attempt.cost,
                attempt.result_url,
                attempt.error,
                _adapt(attempt.submitted_at),
                _adapt(attempt.completed_at),
            ),
        )

    async def record_job_id(self, attempt_id: str, job_id: str) -> None:
        conn = await self._connection()
        await conn.execute(
            "UPDATE attempts SET status = ?, job_id = ?, submitted_at = ? "
            "WHERE attempt_id = ?",
            (AttemptStatus.RUNNING.value, job_id, _now_iso(), attempt_id),
        )

    async def update_attempt(self, attempt_id: str, **fields) -> None:
        if not fields:
            return
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"update_attempt: unknown column(s) {sorted(unknown)}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        values = [_adapt(v) for v in fields.values()]
        values.append(attempt_id)
        conn = await self._connection()
        await conn.execute(
            f"UPDATE attempts SET {assignments} WHERE attempt_id = ?",
            values,
        )

    async def add_cost(self, run_id: str, delta: float) -> float:
        conn = await self._connection()
        # Atomic increment at the SQL level — no read-modify-write race.
        await conn.execute(
            "UPDATE runs SET total_cost = total_cost + ? WHERE run_id = ?",
            (delta, run_id),
        )
        async with conn.execute(
            "SELECT total_cost FROM runs WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return float(row["total_cost"])

    # --- Read / resume ------------------------------------------------------
    async def load_run(self, run_id: str) -> RunState | None:
        conn = await self._connection()

        async with conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ) as cur:
            run_row = await cur.fetchone()
        if run_row is None:
            return None

        async with conn.execute(
            "SELECT * FROM shots WHERE run_id = ? ORDER BY rowid", (run_id,)
        ) as cur:
            shot_rows = await cur.fetchall()

        async with conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY shot_id, attempt_number",
            (run_id,),
        ) as cur:
            attempt_rows = await cur.fetchall()

        shots = [self._row_to_shot(r) for r in shot_rows]

        attempts: dict[str, list[Attempt]] = {}
        for r in attempt_rows:
            attempts.setdefault(r["shot_id"], []).append(self._row_to_attempt(r))

        return RunState(
            run_id=run_row["run_id"],
            status=run_row["status"],
            scene=SceneModel.model_validate_json(run_row["scene_json"]),
            input_description=run_row["input_description"],
            shots=shots,
            attempts=attempts,
            total_cost=run_row["total_cost"],
            created_at=run_row["created_at"],
        )

    @staticmethod
    def _row_to_shot(r: aiosqlite.Row) -> Shot:
        # str enums and isoformat strings are coerced by Pydantic v2.
        return Shot(
            shot_id=r["shot_id"],
            shot_name=r["shot_name"],
            shot_type=r["shot_type"],
            narrative_beat=r["narrative_beat"],
            model=r["model"],
            model_reason=r["model_reason"],
            camera_motion=r["camera_motion"],
            motion_preset=r["motion_preset"],
            prompt=r["prompt"],
            reference=Reference.model_validate_json(r["reference_json"]),
            duration_s=r["duration_s"],
            quality=r["quality"],
            min_drift_score=r["min_drift_score"],
        )

    @staticmethod
    def _row_to_attempt(r: aiosqlite.Row) -> Attempt:
        return Attempt(
            attempt_id=r["attempt_id"],
            run_id=r["run_id"],
            shot_id=r["shot_id"],
            attempt_number=r["attempt_number"],
            idem_key=r["idem_key"],
            status=r["status"],
            job_id=r["job_id"],
            drift_score=r["drift_score"],
            cost=r["cost"],
            result_url=r["result_url"],
            error=r["error"],
            submitted_at=r["submitted_at"],
            completed_at=r["completed_at"],
        )
