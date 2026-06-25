# DirectorAgent — Claude Code Anchor

Claude Code reads this file automatically at session start. It is the
single source of truth for every decision in this project. Do not ask
to revisit decisions marked LOCKED. Do not modify files marked IMMUTABLE.

---

## What this project is
DirectorAgent: one photo + scene description → 6-shot production storyboard
via Higgsfield MCP. Five deterministic phases, parallel fan-out on Phase 3,
CLIP drift detection on Phase 4.

---

## Stack — LOCKED
- Python 3.11+, Pydantic v2, asyncio, aiosqlite, httpx, tenacity, typer, ruff, pytest
- No LangGraph. No Mastra. No Vercel. No framework router.
- Fan-out: `asyncio.gather` + `Semaphore(MAX_CONCURRENT_JOBS=6)`
- Retry: two separate mechanisms — see INVARIANTS below
- Deploy target: Railway / Render / Fly (always-on container, never serverless)

---

## Immutable foundation files — DO NOT MODIFY
These are the typed contracts every other file implements against.
Changing them breaks everything downstream.

| File | Role |
|---|---|
| `schema.py` | All Pydantic models: SceneModel, Shot (carries shot_style + render_class), Attempt, RunState, Storyboard. Enum is `RenderClass` (not ShotType). |
| `protocols.py` | VisionClient, HiggsfieldClient, DriftScorer, StateStore Protocols |
| `routing.py` | ROUTING table, DRIFT_THRESHOLDS, COST_PER_SECOND, retry/concurrency constants |
| `schema.sql` | SQLite DDL: runs, shots, attempts tables |
| `vision_providers.py` | OpenAI / Anthropic / Gemini / Mock VisionProvider transports |

If you think one of these needs changing, stop and explain why before touching it.

---

## File tree — build in this order
```
src/directoragent/
  config.py               ← STEP 1  ✅ (pydantic-settings, env vars; injected, no global)
  state/sqlite_store.py   ← STEP 2  ✅ (StateStore impl, aiosqlite)
  clients/
    higgsfield_mock.py    ← STEP 3a ✅ (mock, no network; supports reconcile)
    higgsfield.py         ← STEP 12 (real MCP adapter; tenacity transient-retry)
  drift/
    mock_scorer.py        ← STEP 4a ✅ (synthetic; fail_first exercises retry)
    clip_scorer.py        ← STEP 13 (lazy open_clip + torch)
  phases/
    vision.py             ← STEP 5  ✅ (VisionExtractor wraps VisionProvider)
    arcs.py               ← STEP 6  ✅ (arc library — NON-FROZEN data)
    motion.py             ← STEP 6  ✅ (motion-preset vocab — NON-FROZEN, provisional)
    mock_plan_provider.py ← STEP 6  ✅ (canned plan for mock-mode planning)
    planner.py            ← STEP 6  ✅ (SceneModel + photo -> list[Shot])
    executor.py           ← STEP 7  ← NEXT (fan-out, submit, poll, drift, quality-retry)
    assembler.py          ← STEP 8  (RunState -> Storyboard)
  pipeline.py             ← STEP 9  (wires phases, owns cost ceiling, injects mocks)
  cli.py                  ← STEP 10 (typer: run / resume / status / list; --arc, --review)
tests/                    ← STEP 11
```
Always ask: what step am I on? Do not skip ahead.
The full, current step-by-step prompts live in `BUILD_RUNBOOK.md`; architecture
and rationale in `docs/TECHNICAL_DOCUMENTATION.md`.

---

## Invariants — enforce these, never violate them

### 1. Two retries, never conflated
**Transient retry** — same job, same attempt row, exponential backoff.
Implemented with `@tenacity.retry` INSIDE the client adapter methods.
Does NOT create a new Attempt row. Handles: 5xx, timeout, network error.

**Quality retry** — new job, new Attempt row, new idem_key, new cost.
Implemented as `while attempt_number <= MAX_ATTEMPTS_PER_SHOT` in executor.py.
Handles: drift score below `shot.min_drift_score`.

If you find yourself creating a new Attempt row inside a tenacity retry —
you have conflated them. Stop and fix it.

### 2. open_attempt() ALWAYS before any network call
```python
await store.open_attempt(attempt)          # status=SUBMITTING — write first
jid = await hf.submit(shot, attempt.idem_key)  # network call second
await store.record_job_id(attempt.attempt_id, jid)  # status=RUNNING
```
The gap between open_attempt and record_job_id is the crash window.
reconcile() covers it on resume. Never reorder these three lines.

### 3. route() and drift_threshold() called ONLY in the planner
The LLM proposes a `render_class` (the closed routing key). routing.py maps
`render_class → model` deterministically. The planner validates the proposed
render_class, then derives `model, model_reason = route(render_class)` and
`min_drift_score = drift_threshold(render_class)`. The LLM NEVER sets model or
min_drift_score. The executor never calls route(); it reads shot.model and
shot.min_drift_score, set by the planner. These fields are immutable once the
Shot is written to the DB.

### 3b. shot_style vs render_class (the split)
`shot_style` is free-form text the LLM chooses (unbounded cinematic descriptor,
no routing meaning). `render_class` is the closed enum routing key. Never conflate
them. Invalid proposed render_class → fall back to the beat's `render_lean` (with
a visible note); never auto-correct a valid-but-odd render_class (trust + plan
review). The LLM proposing render_class is allowed; the routing TABLE choosing
the model is what stays deterministic.

### 4. add_cost() called ONCE per real job submission
Call it immediately after record_job_id(), before polling.
Never call it per-poll, per-attempt-row, or per-retry.

### 5. A PASSED shot is never re-submitted
On executor startup (fresh run or resume), first check:
if latest_attempt.status == PASSED → skip immediately, do not enter retry loop.

### 6. Torch / open_clip never imported at module level
clip_scorer.py must lazy-load on first .score() call.
`import directoragent` must never drag in torch as a side effect.

---

## Planner data — NON-FROZEN, edit freely

These are NOT foundation files. They are plain data meant to grow; editing them
needs no hash regeneration.
- `phases/arcs.py` — the arc library. Each arc = 6 beats (name, intent,
  `render_lean`). `render_lean` is a SOFT default; the planner may override it
  for a shot but must explain the deviation in `model_reason`. Add an arc by
  adding a named 6-beat list.
- `phases/motion.py` — the provisional `motion_preset` vocabulary. **Carries a
  `TODO(P12)` marker.** At STEP 12, reconcile these names against the real
  Higgsfield motion presets (rename to match if needed), update the planner
  allowed-list, and remove the TODO. No foundation/hash change — motion_preset
  is typed as a plain string in schema/DDL precisely because it is provisional.

## Mock-mode planning
`MockVisionProvider` returns a SceneModel and cannot serve the planner. Mock mode
uses `phases/mock_plan_provider.py` (`MockPlanProvider`) for the planner. The
pipeline injects MockVisionProvider for vision and MockPlanProvider for the
planner — both selected at the pipeline injection point, never via an `if mock`
branch inside a phase.

---

## Resume logic (executor.py)
On `da resume <run_id>`, pipeline.py calls store.load_run() and re-enters
executor.run_all() with the existing RunState. Per-shot logic:

```
PASSED          → skip
IN_FLIGHT       → re-poll existing job_id to terminal
SUBMITTING      → reconcile(idem_key) → if job_id found: record + re-poll
                                       → if not found: treat as PENDING, new attempt
RETRYABLE       → continue quality-retry loop from current attempt_number
PENDING / none  → start quality-retry loop from attempt 1
```

---

## Mock mode (`--mock` flag)
Sets `settings.mock_mode = True`. Swaps in:
- MockVisionProvider (vision_providers.py — already written)
- MockHiggsfieldClient (clients/higgsfield_mock.py)
- MockDriftScorer (drift/mock_scorer.py)

With mock mode: full pipeline runs in <10s, zero credentials, zero cost.
CI always runs in mock mode. Do not add any `if mock_mode` branches inside
phases/ or pipeline.py — the swap is done entirely at the injection point
in pipeline.py.

---

## Cost ceiling
`settings.max_cost_usd` (default 10.0, set via --max-cost).
Check: before fan-out, compute `projected = sum(estimate_cost(s.model, s.duration_s) for s in shots)`.
If `state.total_cost + projected > max_cost_usd`: raise CostCeilingError, set
RunStatus.ABORTED in DB, print clear message. Never silently swallow it.

---

## Key types (reference)
```python
# Routing (closed enum, the routing key)
RenderClass: FACE | COMPLEX_MOTION | ABSTRACT_FLUID | WIDE_ENVIRONMENT
Model:       SOUL_V2 | KLING_3 | WAN_2_6 | VEO_3_1
# Shot also carries shot_style: str  (free-form, LLM-chosen, NOT a routing key)

# Attempt lifecycle
PENDING → SUBMITTING → RUNNING → SCORING → PASSED
                                          → FAILED_DRIFT   (quality retry)
                                          → FAILED_ERROR   (quality retry)

# Sets
TERMINAL  = {PASSED, FAILED_DRIFT, FAILED_ERROR}
RETRYABLE = {FAILED_DRIFT, FAILED_ERROR}
IN_FLIGHT = {RUNNING, SCORING}

# RunStatus
PLANNING (plan persisted, not executed — used by --review) | EXECUTING | COMPLETE | ABORTED
```

---

## Environment variables
```
VISION_PROVIDER=anthropic     # openai | anthropic | gemini | mock
VISION_MODEL=                 # blank = provider default
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
HIGGSFIELD_API_KEY=
MAX_COST_USD=10.0
LOG_LEVEL=INFO
```

---

## What NOT to do
- Do not add LangGraph, Mastra, or any graph/workflow framework as a dependency
- Do not use `datetime.utcnow()` — use `datetime.now(timezone.utc)`
- Do not write to the immutable foundation files
- Do not call route() or drift_threshold() outside of planner.py
- Do not let the LLM set model or min_drift_score — derive them from route()/drift_threshold()
- Do not conflate shot_style (free) with render_class (closed routing key)
- Do not build a shot_style→render_class classifier (deferred; trust + plan review)
- Do not put the motion-preset vocabulary in the frozen schema (it is provisional; lives in phases/motion.py)
- Do not batch transient-retry and quality-retry under the same mechanism
- Do not import torch at module level anywhere
- Do not add `if mock_mode` branches inside phases — swap at the pipeline injection point
- Do not use serverless-incompatible patterns (this is an always-on process)
- Do not use a plain JSON file for state — use the SQLite store (concurrency safety)
