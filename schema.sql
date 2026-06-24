-- DirectorAgent state store (SQLite default; Postgres-compatible DDL).
-- Level-1 durability: persist scene, plan, and the per-attempt job ledger.
-- Immutable attempts (one row per submission) give first-try-yield and
-- drift history for free.

PRAGMA journal_mode = WAL;        -- better concurrency for the fan-out
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    status             TEXT NOT NULL,            -- planning|executing|complete|aborted
    scene_json         TEXT NOT NULL,            -- serialized SceneModel
    input_description  TEXT NOT NULL DEFAULT '', -- stored for provenance
    total_cost         REAL NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

-- The immutable plan. One row per shot, written once after Phase 2.
CREATE TABLE IF NOT EXISTS shots (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    shot_id         TEXT NOT NULL,
    shot_name       TEXT NOT NULL,
    shot_type       TEXT NOT NULL,
    narrative_beat  TEXT NOT NULL,
    model           TEXT NOT NULL,
    model_reason    TEXT NOT NULL,
    camera_motion   TEXT NOT NULL,
    motion_preset   TEXT,
    prompt          TEXT NOT NULL,
    reference_json  TEXT NOT NULL,        -- serialized Reference
    duration_s      REAL NOT NULL,
    quality         TEXT NOT NULL,
    min_drift_score REAL NOT NULL,
    PRIMARY KEY (run_id, shot_id)
);

-- The execution log. One row per submission attempt — never updated in a
-- way that loses history; status/job_id/drift fill in over the attempt's life.
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    shot_id        TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,      -- 1-indexed
    idem_key       TEXT NOT NULL UNIQUE,  -- run_id:shot_id:attempt_number
    status         TEXT NOT NULL,         -- pending|submitting|running|scoring|passed|failed_*
    job_id         TEXT,                  -- NULL until submit returns
    drift_score    REAL,
    cost           REAL NOT NULL DEFAULT 0,
    result_url     TEXT,
    error          TEXT,
    submitted_at   TEXT,
    completed_at   TEXT,
    FOREIGN KEY (run_id, shot_id) REFERENCES shots(run_id, shot_id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_shot ON attempts(run_id, shot_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(run_id, status);
