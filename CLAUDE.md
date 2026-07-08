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

**Reconcile reality (P12 discovery; contract reopened at SWEEP-FIX-2):** the
Higgsfield API accepts NO idempotency key on submit — it only knows job_ids it
issued. So in the REAL adapter, `reconcile(idem_key, shot)` is implemented as a
content-fingerprint search: the fingerprint {catalog model id, full generation
prompt incl. the folded motion phrase} is derived from the persisted Shot AT
CALL TIME (crash-safe — no in-memory state), then matched against recent
generations (show_generations). Prompts are IDENTICAL across a shot's
quality-retry attempts, so anything but exactly one match is ambiguous: zero or
MULTIPLE matches → None, and the caller submits fresh (an accepted, rare
double-charge). Exactly one match → recover its job_id, persist, re-poll. The
mock still supports key-based reconcile directly (same signature; `shot`
unused).

**Real API shapes (confirmed P12.4 — build against these):**
- Response envelope: generate_video / job_display return `{"results":[{…}]}`.
  Unwrap via `_job_entry()` in submit/poll/fetch_result/reconcile. Asset URL at
  `results[0].results.rawUrl` (dict). Media list key is `"uploads"`.
- Status vocabulary: `pending`/`in_progress` → running; `completed` → succeeded.
  Failure strings unobserved — keep `_FAILED` conservative and the
  `unknown → running` defensive branch. Do NOT guess failure strings.
- Media: `media_import_url` (public URL → media_id) works everywhere.
  `_upload_local` (media_upload → PUT presigned → media_confirm) is blocked by the
  Claude sandbox egress proxy (CONNECT 403) — shape-verified but NOT
  end-to-end-tested; verify in a deployed container. Presigned URL signs
  content-type → `_put_bytes` sends matching Content-Type.
  - `media_upload` response: `{"uploads":[{"upload_url","media_id","url",
    "content_type","expires_in_seconds","method":"PUT",...}]}`.
  - `media_import_url` response: `{"media_id","type","content_type","source_url"}`.
- Start-image role per model (in `medias[].role`): `start_image` for
  Seedance/Kling/Veo; **`image_references` for Wan** (Wan has no start_image role).
- Reliability: frequent transient "Something went wrong" errors; the tenacity
  5xx/timeout seam is load-bearing.

**Two transports behind the call_tool seam (the adapter is transport-agnostic):**
- Agent-mediated (in-session demo, proven at P12.4): call_tool = the Claude agent
  invoking mcp__Higgsfield__* via the OAuth connector. Only works in a Claude
  session. This is what `main` demos with.
- REST (P12.5, deployable — BUILT): call_tool = `HiggsfieldRestTransport`,
  authenticated httpx to `platform.higgsfield.ai`, header
  `Authorization: Key KEY_ID:KEY_SECRET`. Runs anywhere. Config:
  `higgsfield_key_id` + `higgsfield_key_secret` + `higgsfield_base_url`.
  Contract derived from the official `higgsfield-js` v2 SDK. Selected in
  pipeline real-mode when both creds are set; ADDITIVE — agent path untouched.
  - REST envelope is FLAT (`{status, request_id, images:[{url}], video:{url}}`),
    NOT the MCP `{"results":[…]}` wrapper. Transport normalizes REST→MCP so the
    adapter stays shape-stable. Status adds `nsfw`/`canceled` → failed.
  - Poll: `GET /requests/{request_id}/status`. Upload: `POST
    /files/generate-upload-url` → presigned PUT.
  - **TWO TODO(P12.5-live) gaps — block a real REST submit until the first
    allowlisted/deployed run:** (1) the per-model submit endpoint is CMS-driven,
    absent from the SDK — transport uses a `/v2/generate` PLACEHOLDER; read real
    endpoints from the live API. (2) `get_cost` has no REST equivalent, so
    `preflight_cost` over REST raises — REST real mode is fully inoperative until
    TODO(P12.5-live) #2 is resolved (preflight raises at the first pipeline step).
    Deploy-time fix (recommended): degrade to the static
    COST_PER_SECOND table for projection + reconcile actual cost post-submit.
  - The sandbox egress proxy blocks platform.higgsfield.ai (CONNECT 403), so REST
    is verified only by stubbed unit tests here; the live smoke is deploy-time.

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
- `phases/motion.py` — the `motion_preset` vocabulary. **TODO(P12) RESOLVED:**
  the real API has NO camera-motion enum (motion is prompt/preset/motion_control
  driven). `motion_preset` stays as internal metadata + a prompt hint: the REAL
  adapter folds it into the generation prompt text (e.g. PUSH_IN → "slow
  push-in"); it is NEVER a generate_video parameter. Keep the field — it is
  reserved for a future mapping to Higgsfield preset_ids. Remove the TODO(P12)
  marker when the real adapter lands.
- `phases/model_limits.py` — real catalog duration constraints per Model
  (Veo {4,6,8}; Wan {5,10,15}; Kling 3–15; Seedance 4–15) + `clamp_duration()`.
  NON-FROZEN — tracks the live catalog via models_explore.

## Duration rule (P12)
The planner clamps each shot's `duration_s` to its routed model's allowed set
AFTER route() assigns the model (the clamp needs the model, so it cannot happen
in the LLM output). Log original vs clamped when they differ. There is NO hard
total-duration bound — the old [60,180] gate is removed (model caps can make it
unsatisfiable; e.g. six Veo shots max at 48s). Plan validation = exactly 6
shots + valid render_classes only.

## Cost units (P12)
Higgsfield bills in CREDITS, and generate_video supports `get_cost: true` as a
no-spend preflight. REAL mode: cost is denominated in credits via get_cost;
the ceiling is a credit budget. MOCK mode: keeps the static placeholder
COST_PER_SECOND table (arbitrary cost-units). `max_cost_usd` is a generic
budget ceiling (name is a known misnomer in real mode; do not rename config
without an approved reopening).

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
Model:       SEEDANCE_2 | KLING_3 | WAN_2_6 | VEO_3_1
# Internal names; the REAL adapter owns the mapping to catalog IDs
# (seedance_2 → seedance_2_0, kling_3 → kling3_0, wan_2_6 → wan2_6,
#  veo_3_1 → veo3_1). SOUL_V2 was renamed at P12.1 — no soul_v2 in the catalog.
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
