# DirectorAgent — Technical Architecture & Engineering Documentation

> Audience: engineers joining or reviewing the DirectorAgent project. This
> document explains what the system is, how its parts connect, what changes
> ripple where, the decisions that have been locked and why, and what has been
> deliberately deferred. It is the standing reference; the BUILD_RUNBOOK drives
> day-to-day implementation and CLAUDE.md is the operational anchor for the
> coding agent.

---

## 1. Purpose & scope

DirectorAgent converts a single photograph plus a short scene description into a
six-shot, production-grade storyboard — a sequenced set of cinematic video clips
with consistent characters, controlled camera grammar, and validated visual
fidelity — using Higgsfield's MCP as the generation backend.

The system is intentionally a **deterministic DAG**, not an agent that branches
at runtime. The creative latitude is scoped to descriptive fields chosen by an
LLM; every control-flow and resource decision (which model runs, when to retry,
what a shot costs, when to stop) is mechanical and auditable. This separation is
the central design principle and most invariants exist to protect it.

---

## 2. System shape — five phases

```
photo + description
        │
  [1] VISION      photo ─► typed SceneModel              (provider-agnostic)
        │
  [2] PLANNER     SceneModel + photo ─► 6 × Shot         (LLM creative + deterministic routing)
        │
  [3] EXECUTOR    fan-out 6 jobs ─► Higgsfield MCP        (asyncio + Semaphore(6))
        │
  [4] DRIFT       each result ─► CLIP cosine vs source    (per-shot threshold, quality-retry)
        │
  [5] ASSEMBLER   RunState ─► Storyboard + shot grid      (metrics, cost, first-try yield)
        │
   storyboard.json + stdout grid
```

Phases 3 and 4 are interleaved inside the executor (submit → poll → score →
maybe retry), but conceptually remain distinct stages.

---

## 3. Artifact map

Artifacts are organised in layers. Lower layers are depended upon by higher
layers and change far less often.

### Layer A — Contracts (foundation; frozen)

| File | Role |
|---|---|
| `src/directoragent/schema.py` | All Pydantic models and enums: SceneModel, Shot, Attempt, RunState, ShotResult, Storyboard. The typed spine every phase hands across. |
| `src/directoragent/protocols.py` | The four seams as `Protocol`s: VisionClient, HiggsfieldClient, DriftScorer, StateStore. |
| `src/directoragent/routing.py` | Deterministic config: ROUTING, DRIFT_THRESHOLDS, COST_PER_SECOND, MAX_ATTEMPTS_PER_SHOT, MAX_CONCURRENT_JOBS, and the route()/drift_threshold()/estimate_cost() helpers. |
| `src/directoragent/schema.sql` | SQLite DDL: runs, shots, attempts tables. Immutable attempt rows. |

These files are treated as immutable contracts. Their bytes are frozen by a hash
guard (`tests/foundation_hashes.json`). Intentional changes require regenerating
those hashes in the same commit, which makes any modification visible in review.

### Layer B — Implementations behind the contracts

| File | Satisfies | Status |
|---|---|---|
| `vision_providers.py` | VisionProvider transports (OpenAI/Anthropic/Gemini/Mock) + `make_provider()` | Foundation, frozen |
| `phases/vision.py` | VisionClient (wraps a VisionProvider; prompt + parse + validate + repair) | Built (P5) |
| `state/sqlite_store.py` | StateStore (aiosqlite, Level-1 ledger) | Built (P2) |
| `clients/higgsfield_mock.py` | HiggsfieldClient (no network; supports reconcile) | Built (P3a) |
| `clients/higgsfield.py` | HiggsfieldClient (real MCP: catalog mapping, media handshake, motion-into-prompt, fingerprint reconcile, get_cost preflight) | Built (P12.3) |
| `drift/mock_scorer.py` | DriftScorer (synthetic, deterministic fail-then-pass) | Built (P4a) |
| `drift/clip_scorer.py` | DriftScorer (lazy open_clip+torch; PyAV mid-point frame from video; pure-Python cosine; fail→0.0, never raises) | Built (P13) |

### Layer C — Orchestration

| File | Role | Status |
|---|---|---|
| `phases/arcs.py` | Arc library (4 arcs × 6 beats with soft render_leans); non-frozen data | Built (P6) |
| `phases/motion.py` | Provisional motion-preset vocabulary; non-frozen; TODO(P12) | Built (P6) |
| `phases/mock_plan_provider.py` | Canned 6-shot plan for mock-mode planning | Built (P6) |
| `phases/planner.py` | SceneModel + photo ─► 6 × Shot; arc selection + guard + deterministic routing | Built (P6) |
| `phases/executor.py` | Fan-out, submit/poll, drift scoring, quality-retry, resume | Built (P7) |
| `phases/assembler.py` | RunState ─► Storyboard; metrics; shot grid; storyboard.json | Built (P8) |
| `pipeline.py` | Wires the five phases; owns the cost ceiling; selects mock vs real impls; resume-based plan-review | Built (P9) |
| `phases/model_limits.py` | Real per-model duration constraints + clamp_duration(); non-frozen, tracks the catalog | Built (P12.1b) |
| `cli.py` | Typer app: run / resume / status / list; --arc, --review; prints run_id | Built (P10) |
| `config.py` | pydantic-settings Settings; constructed once at the CLI boundary | Built (P1) |

### Layer D — Verification

| File | Role | Status |
|---|---|---|
| `.github/workflows/ci.yml` | Deterministic gates on every push: ruff, pytest, mock pipeline, idempotent resume | Built |
| `tests/test_invariants.py` | Static invariants as executable checks (no module-level torch, routing-call locality, no utcnow, no settings singleton, foundation-hash guard) | Built |
| `tests/test_routing.py` | Routing/threshold/cost correctness | Built (P11) |
| `tests/test_executor.py` | Behavioral invariants: second attempt on drift-fail, no resubmit on PASSED, open_attempt-before-network, add_cost-once | Built (P11) |
| `tests/test_pipeline_mock.py` | Full mock pipeline yields a valid Storyboard; plan-review/resume path | Built (P11) |

---

## 4. Dependency & coupling model — what ripples where

The architecture is designed so that the **frequently-changing things have small
blast radius** and the **wide-blast-radius things change rarely**. The coupling
rules below are the practical consequence and should guide any change.

| Change | Ripples to | Blast radius | Notes |
|---|---|---|---|
| `schema.py` model/field | every serializer, every phase, every test, foundation hashes | **Highest** | Why it is frozen. Reopen deliberately, before dependents are built. |
| A `protocols.py` signature | all implementations of that protocol + their callers | High | The seam is the API; changing it breaks both sides. |
| `routing.py` table entry (model/threshold/cost) | the planner (sets fields) + routing tests + hashes | **Low** | The executor/store/assembler are unaffected — they read populated fields. This is the core payoff of deterministic routing. |
| Add a `render_class` value | routing.py (3 entries) + planner allowed-list + tests + hashes | **Low, localized** | No hard-file changes. A routine, on-demand extension. |
| Add a `shot_style` value | nothing | **Zero** | Free-form text; the LLM emits it. Unbounded creative vocabulary at no cost. |
| Add a VisionProvider | `vision_providers.py` factory only | **Zero elsewhere** | Bring-your-own-model is one method (`complete`). |
| Swap SQLite ─► Postgres | a new StateStore impl + the injection point in pipeline.py | **Zero in phases** | Phases never see the backend; they see the StateStore protocol. |
| Swap mock ─► real Higgsfield/CLIP | the injection point in pipeline.py | **Zero in phases** | Selected by `settings.mock_mode`; no `if mock` branches leak into phases. |

The single most important structural fact: **the executor never branches on
`render_class`.** It reads `shot.model` and `shot.min_drift_score`, both set by
the planner from the routing table. This is why new models/classes never touch
the hardest file in the system.

---

## 5. Data model

The flow is a progressive refinement from unstructured input to a validated
artifact, with an immutable execution log accumulating underneath the plan.

- **SceneModel** (Phase 1 output): source_photo_path, subject, environment,
  lighting, mood, objects, color_palette. Typed extraction from the photo.
- **Shot** (Phase 2 output; the plan, immutable once written): shot_id,
  shot_name, narrative_beat, `shot_style` (free creative descriptor),
  `render_class` (closed routing key), model, model_reason, camera_motion,
  motion_preset, prompt, reference (type/source/weight), duration_s, quality,
  min_drift_score. *(See §7 for the shot_style/render_class split, decided and
  pending implementation.)*
- **Attempt** (Phases 3–4 execution log; immutable rows): attempt_id, run_id,
  shot_id, attempt_number, idem_key, status, job_id, drift_score, cost,
  result_url, error, timestamps. One row per submission; never mutated in a way
  that loses history.
- **RunState**: the run plus its shots plus attempts grouped by shot_id. Powers
  resume. Carries input_description for provenance.
- **Storyboard** (Phase 5 output): per-shot ShotResults, total_cost,
  first_try_yield, mean_drift.

Immutable attempt rows are a deliberate choice: they yield first-try-yield and
per-shot drift history for free, which a mutable status field would discard.

---

## 6. State, durability & retries

### Level-1 durability (job ledger)

Only one thing in the system costs real money and cannot be cheaply redone: a
submitted Higgsfield job. Vision and planning are cheap and reproducible.
Therefore durability is scoped to the scene, the plan, and the per-attempt job
ledger — not full step-level checkpointing. On restart the executor re-attaches
to in-flight jobs rather than re-paying for them.

### The crash window

```
open_attempt()      → status SUBMITTING   (written BEFORE any network call)
hf.submit()         → returns job_id      (the network call)
record_job_id()     → status RUNNING      (written AFTER)
```

The gap between `open_attempt` and `record_job_id` is the crash window. On
resume, a shot left in SUBMITTING is recovered via `reconcile(idem_key)`.

**Reconcile reality (P12 discovery — LOCKED):** the live Higgsfield API accepts
**no idempotency key** on submission; it only knows job_ids it issued. So the
real adapter implements `reconcile` as a **content-fingerprint search**: query
recent generations (`show_generations`) and match on {model, prompt, params} —
shot prompts are unique per attempt, so a recent match is the orphaned job.
Recover its job_id, persist, re-poll. If the match is absent or ambiguous, fall
back to a fresh submit (an accepted, rare double-charge). The Protocol signature
is unchanged; the mock still supports direct key-based reconcile. Net effect:
the crash-recovery *intent* (don't re-pay) is preserved via the mechanism the
API actually provides.

### Two distinct retry mechanisms (never conflated)

1. **Transient retry** — a 5xx/timeout on submit or poll. Same job, same attempt
   row, exponential backoff. Implemented with `tenacity` **inside the real
   adapter** (`clients/higgsfield.py`). Never creates a new attempt row. The
   executor calls `hf.submit`/`hf.poll` directly and does not wrap them.
2. **Quality retry** — drift below `shot.min_drift_score`. A genuinely new job:
   new idem_key, new cost, **new attempt row**. Implemented as the bounded
   `while n < MAX_ATTEMPTS_PER_SHOT` loop in the executor.

Conflating them either double-counts cost or silently swallows quality failures,
and corrupts the first-try-yield metric.

### Concurrency

The fan-out is `asyncio.gather` over `run_shot` coroutines bounded by
`Semaphore(MAX_CONCURRENT_JOBS=6)`. The SQLite store uses a single long-lived
aiosqlite connection so the six coroutines' writes serialise cleanly on one
writer (WAL mode, busy_timeout) rather than racing a shared file.

---

## 7. Planner design — routing, the style/class split, arcs, guard logic, and the prompt contract

### The problem this solves

A single field was doing two unrelated jobs: describing the shot cinematically
*and* serving as the routing key that selects a Higgsfield model and drift
threshold. That conflation caps creative vocabulary at the number of routing
targets, which is wrong — cinematic variety is unbounded, routing targets are
not.

### The decision (LOCKED — schema/routing/storage implemented; guard logic at P6)

Split the two jobs into two fields, "guarded-(a)":

- **`shot_style`** — free-form text the LLM chooses with wide latitude
  (close-up, over-the-shoulder dolly, crane-out, abstract transition, …).
  Unbounded; adding values costs nothing.
- **`render_class`** — a closed enum (FACE, COMPLEX_MOTION, ABSTRACT_FLUID,
  WIDE_ENVIRONMENT) that is the routing key. The LLM proposes a render_class per
  shot; the planner **validates** it (see guard logic) and the model is derived
  deterministically via `route(render_class)` — the LLM never picks the model.

Implemented as a rename of `shot_type` → `render_class` plus a new `shot_style`
field across schema.py, routing.py, schema.sql, and the SQLite store.

### Guard logic (guarded-(a)) — LOCKED, implemented at P6

For every shot the planner sets `render_class` **first**, then derives
`model, model_reason = route(render_class)` and
`min_drift_score = drift_threshold(render_class)`. The LLM proposes a
render_class but never names a model or sets a threshold; the routing table is
the only thing that turns a class into a model. This holds across all paths
below, so the "LLM never sets model" invariant is intact.

**Invalid / malformed render_class → fall back to the beat's `render_lean`:**
1. Normalize the proposed value (lowercase, trim, match the four enum members).
   This absorbs harmless case/whitespace noise.
2. If it still does not resolve → use the beat's `render_lean` (always a valid,
   arc-coherent default). The fallback is **recorded as a visible note on the
   shot** so the plan-review checkpoint surfaces it. No `shot_style` keyword
   parsing is used to recover a fallback — that would reintroduce the brittle
   classifier discussed below.

**Valid-but-inconsistent render_class (e.g. style "extreme wide aerial" tagged
FACE) → trust the validated value; do NOT auto-correct.** A `shot_style →
render_class` classifier is deliberately avoided in v1 because it introduces
*false* corrections (overriding render_classes the LLM got right for subtle
reasons) and degrades routing accuracy under the guise of safety. Instead,
consistency is handled by three cheaper layers:
- **Prevention (prompt):** the planner prompt gives the LLM a one-line rubric
  per render_class and requires the class to match the shot's scale/subject, so
  coherent output is the default.
- **Detection (human):** the plan-review checkpoint shows every shot's
  shot_style + render_class + model before any paid generation.
- **Containment (machinery):** a genuinely misrouted shot fails drift, exhausts
  the bounded retry budget, and is flagged failed by the assembler — bounded,
  visible waste capped by the cost ceiling.

The automated consistency-check is **deferred, gated on observed mismatch rate**
(see §11): if real runs show frequent mismatches, that failure data justifies
building it; until then it is speculative complexity.

### render_class count — the criticality call

`render_class` cardinality tracks the number of **meaningfully distinct
generation models**, not the number of shot ideas. Four models exist today, so
four classes is correct. Adding a fifth is a low-urgency, additive operation
(routing.py + planner allowed-list + tests + hashes; no hard-file changes) and
is performed only when Higgsfield's model roster genuinely grows. See §12.

### render_class count — the criticality call

`render_class` cardinality tracks the number of **meaningfully distinct
generation models**, not the number of shot ideas. Four models exist today, so
four classes is correct. Adding a fifth is a low-urgency, additive operation
(routing.py + planner allowed-list + tests + hashes; no hard-file changes) and
is performed only when Higgsfield's model roster genuinely grows. See §12.

### Arc skeleton (planner structure) — LOCKED, pending implementation at P6

The six-shot arc is a **named, swappable template**, not prose hard-wired into
the planner prompt. An arc template is an ordered list of six **beats**; each
beat carries three things:

- `name` — the structural role (e.g. "establish the space")
- `intent` — one line on its dramatic function, fed to the planner as guidance
- `render_lean` — a **suggested, non-binding** RenderClass for that beat

**Override rule (the soft-hint contract):** the planner uses a beat's
`render_lean` as the default render_class *unless the scene gives a concrete
reason to deviate*. When it deviates, the deviation **must be explained in that
shot's `model_reason`**. This keeps every off-lean routing choice auditable and
preserves the deterministic-routing story — lean is the default, deviation is
allowed, deviation is always justified.

**Default-arc selection flow (when the user names no arc):**
1. User named an arc → use it.
2. Else → the planner reads scene + description, selects the best-fitting arc
   from the library, and records a one-line reason.
3. Genuinely ambiguous → fall back to `dramatic`.

The selected arc and its reason are **announced to the user** ("Selected the
*observational* arc — the scene reads as a group exchange") and surface in the
plan-review checkpoint before any paid generation.

**v1 arc library (four arcs):**

| Arc | Beats | Render leans (soft) |
|---|---|---|
| `dramatic` *(default/fallback)* | establish · introduce subject · rising detail · turn · peak · resolution | wide · face · complex_motion · face · complex_motion · wide |
| `observational` | wide establish · group framing · individual reaction · exchange detail · pull back · environmental drift | wide · wide · face · face · complex_motion · wide |
| `reveal` | conceal · hint · approach · widening context · the reveal · aftermath | face · abstract_fluid · complex_motion · wide · face · wide |
| `mood-piece` | texture open · subject in space · light/detail · abstract interlude · subject return · lingering close | abstract_fluid · wide · face · abstract_fluid · face · face |

**Placement:** the arc library is **plain, non-frozen data** (a module such as
`phases/arcs.py`), deliberately separate from the frozen routing table. Arcs are
meant to grow; adding one is a pure data addition with zero ripple. The
`render_lean` values reference RenderClass but carry no routing authority — only
`route(render_class)` does. The user-defined-arc mode (accepting an arbitrary
caller-supplied beat list through the same interface) is **stubbed for later**
(see §11).

### Filmable-prompt contract — LOCKED, implemented at P6

Each shot's `prompt` is effectively a Higgsfield generation prompt. The contract
forces it to describe what a camera literally captures — present-tense, no
interiority — rather than a narrative logline. An LLM left unconstrained drifts
toward plot ("she confronts her past"); the rubric pulls it back to the camera.

**Length:** 2–4 sentences, dense with visual nouns and verbs, no narrative
connective tissue.

**Required elements (representational shots):**
1. Subject + concrete observable action ("the subject turns toward the window,"
   not "the subject feels uncertain").
2. Framing / shot scale (close-up, medium, wide). This is where `shot_style`
   connects to the prompt text.
3. Camera movement (push-in, dolly, pan, static hold, crane-out). Must agree
   with the shot's `camera_motion` / `motion_preset` fields.
4. Lighting / atmosphere, drawn from the scene model's lighting and mood.
5. Setting anchor within the established environment, so the six shots read as
   one place rather than six unrelated images.

**ABSTRACT_FLUID exemption:** these shots have no subject-action by nature.
Element 1 is replaced by "visual motif + motion quality," and the setting anchor
(element 5) is relaxed. Framing, camera movement, and lighting/atmosphere still
apply.

**Negative instruction (stated explicitly in the prompt):** no internal states,
no plot or backstory, no unfilmable abstractions, no dialogue. A prompt that
reads as a logline is rejected.

**Few-shot anchor:** the planner prompt includes a concrete contrast pair, e.g.
— Unfilmable: *"The detective realizes she's been betrayed."*
— Filmable: *"Medium close-up, slow push-in on the detective's face under a
single overhead bulb; her eyes shift left, jaw tightening; cold blue light, deep
shadows behind."*
Examples move LLM behavior more reliably than rules alone.

**Empirical tuning:** the prompt is a strong starting point, not a final answer.
The planner is the one component expected to need a round or two of tuning on
real photos after P6 (filmability, groundedness, prompt length). That tuning is
a design loop (decided in the brainstorming context, not improvised in code),
and length in particular is a knob to revisit once Higgsfield's real model
response is observed at P12.

### Groundedness contract — LOCKED, implemented at P6

Groundedness is **graded, not binary** — over-binding produces stiff, repetitive
near-photocopies that fight the generation; under-binding breaks the
consistent-character premise. The contract is explicit about what stays stable
and what is free to vary.

**Three tiers of binding:**
1. **Subject identity — stable** across all representational shots. Every
   FACE / COMPLEX_MOTION / WIDE_ENVIRONMENT prompt references the established
   subject so it reads as the same character. Non-negotiable.
2. **Palette & lighting — anchored, modulating with the beat.** The scene
   model's lighting/palette/mood set the world; shots stay inside it but may
   shift contrast/intensity for dramatic beats. Anchored, not locked.
3. **Framing, angle, action, in-frame content — free** to vary per shot. This
   is where cinematic variety lives and must not be bound.

**Beat-dependent application** (parallels the filmable-rubric exemption):
- FACE / COMPLEX_MOTION → full subject binding + palette anchor.
- WIDE_ENVIRONMENT → lighter subject binding (subject may be contextual/small),
  palette anchor emphasized to establish the world.
- ABSTRACT_FLUID → subject binding dropped; the palette/mood anchor becomes the
  primary groundedness, so the interlude reads as the same world.

**Two principles that keep this non-arbitrary:**
- *The contract is the textual twin of the per-class drift thresholds.* FACE is
  bound tight in text and scored at 0.78; ABSTRACT_FLUID is relaxed in text and
  scored at 0.65; WIDE_ENVIRONMENT sits between at 0.70. Prose guidance and
  numeric enforcement express the same grading and must stay consistent — retune
  a threshold, move the wording with it.
- *The reference image, not the text, is the primary identity carrier.* Each
  Higgsfield job receives the source photo as a reference and CLIP drift scoring
  enforces fidelity against it. So the textual subject anchor is **concise** —
  enough to keep the shot coherent and direct the action — and does **not**
  exhaustively redescribe appearance, which can conflict with the image
  reference. Text guides; image + drift enforces. (This is why the planner is
  given the photo, not only the flattened `subject` string.)

### Camera motion & motion presets — LOCKED (motion_preset provisional, reconciled at P12)

Two fields describe the same physical camera move at different levels, mirroring
the shot_style/render_class split one level down:

- **`camera_motion`** — free-text prose ("slow push-in toward her face"); feeds
  the filmable prompt. Permanently free; never maps to an API enum.
- **`motion_preset`** — a closed value the real Higgsfield adapter maps to a
  parameter. The planner writes `camera_motion` expressively, then selects the
  `motion_preset` that best matches it.

**Provisional vocabulary (v1):** PUSH_IN, PULL_OUT, PAN_LEFT, PAN_RIGHT, TILT_UP,
TILT_DOWN, ORBIT, STATIC, HANDHELD, CRANE. Invalid/absent → fall back to STATIC.

**Why the provisional set lives in mutable code:** the schema already types
`motion_preset` as a string and schema.sql stores it as TEXT, so **no foundation
change is needed**. The closed vocabulary lives in a non-frozen planner module
(`phases/motion.py`) precisely *because* it is provisional. `render_class` is
frozen (core, stable, tied to the model roster); `motion_preset` is not
(provisional, tied to an unknown external vocabulary). The distinction is
deliberate.

**P12 reconciliation — RESOLVED (discovery complete):** inspection of the live
MCP found **no camera-motion enum anywhere** on `generate_video` — motion is
expressed via prompt text, Higgsfield presets (`preset_id`), or the separate
`motion_control` tool (reference-video motion transfer). Outcome, as decided:
`motion_preset` stays as an **internal abstraction** — human-readable storyboard
metadata plus a prompt hint. The real adapter **folds it into the generation
prompt text** (e.g. PUSH_IN → "slow push-in"); it is never a `generate_video`
parameter. The field is retained (not simplified away) as reserved for a future
mapping to Higgsfield `preset_id`s. The `TODO(P12)` marker is removed when the
real adapter lands. Executor/store/assembler were never touched — they always
passed it through opaquely, exactly as designed.

### P12 discovery — what the live API changed (all LOCKED)

Inspection of the connected Higgsfield MCP (~68 tools) surfaced four mismatches
between design assumptions and reality, each resolved by decision:

1. **`soul_v2` does not exist in the catalog.** The identity/face model is
   `seedance_2_0`. Resolved: `Model.SOUL_V2` renamed to `Model.SEEDANCE_2`
   (schema + routing + hash regen — P12.1, done). Internal enum names stay in
   DirectorAgent style; the **real adapter owns the mapping to catalog IDs**
   (seedance_2 → `seedance_2_0`, kling_3 → `kling3_0`, wan_2_6 → `wan2_6`,
   veo_3_1 → `veo3_1`) so a catalog rename never reopens the frozen enum.
2. **No idempotency key** → fingerprint-based reconcile (see §6).
3. **Real duration limits are hard and per-model** (Veo {4,6,8}s; Wan {5,10,15};
   Kling 3–15; Seedance 4–15) — far tighter than the planner's old 8–30s/60–180s
   assumption (six Veo shots max at 48s, making the old total bound
   unsatisfiable). Resolved (P12.1b, done): non-frozen `phases/model_limits.py`
   carries `MODEL_DURATIONS` + `clamp_duration()`; the planner clamps each
   shot's duration **after** `route()` assigns the model (the clamp needs the
   model); the total-duration hard bound is **removed** — validation is now
   "exactly 6 shots + valid render_classes"; the planner prompt teaches
   realistic per-model ranges so clamping is the exception.
4. **Billing is in credits, and `generate_video` supports `get_cost: true`** —
   a no-spend cost preflight. Resolved: a `preflight_cost(shot)` method is added
   to the HiggsfieldClient Protocol (P12.2 — foundation reopening, hash regen).
   It is the single cost source for the pipeline projection and the executor's
   budget-guard/add_cost. Real adapter: credits via `get_cost`; mock: the static
   COST_PER_SECOND table (values unchanged, tests unaffected). Real-mode cost is
   denominated in **credits**; `max_cost_usd` is a generic budget ceiling (a
   known naming misnomer, deliberately not renamed). One remaining unknown —
   the **status-string vocabulary** — is unconfirmable without spending; the
   adapter ships defensive normalization (unknown strings logged, not crashed)
   and the first cheap real shot (P12.4) confirms the vocabulary. **Live
   calibration data point:** Veo 3.1 @ 8s costs 22 credits (confirmed via
   `get_cost` in P12.3 smoke). Wan 2.6 @ 5s is expected to be the cheapest
   combination — used for P12.4.

**Transport seam:** the repo's Python cannot call a claude.ai MCP connector
directly. The real adapter reaches Higgsfield via an injectable `call_tool` seam
(production: a real MCP client session built with `api_key`; test/dev: the
connector called directly and the response shape captured). The smoke in P12.3
validated param-building and response-extraction against the real `get_cost`
response. Status-string parsing carries `TODO(P12.4)` markers, resolved on the
first paid run.

**`preflight_cost` without media is correct behavior:** cost is
model/duration/quality-driven; the reference media_id does not affect the credit
figure. Requiring a media upload at plan-review time would couple the cost gate
to a network handshake unnecessarily. `submit` sends the resolved media; preflight
does not.

### P12.4 live-run discovery (first real shot — Wan 2.6 @ 5s, 13 credits)

The first real generation confirmed three things the mock could not, resolving
all `TODO(P12.4)` markers:

**Response envelope.** `generate_video` and `job_display` wrap every job in
`{"results":[{…}]}`. The adapter unwraps via `_job_entry()` in submit / poll /
fetch_result / reconcile. The asset URL lives at `results[0].results.rawUrl` (a
dict, not a list-of-`{url}`). The media list key is `"uploads"` (not
medias/files/items). This envelope bug was invisible in mock mode (the mock
returned the assumed shape) and would have made every real poll read
`status=None` → stuck "running"; it is exactly the class of defect the live run
existed to surface.

**Status vocabulary (observed, verbatim).** `pending` (at submit) → `in_progress`
(in flight) → `completed` (success). So `_SUCCEEDED={"completed"}`,
`_RUNNING={"pending","in_progress"}`. Failure strings were NOT observed
(success-only run), so `_FAILED` stays conservative and the defensive
`unknown → running` branch remains as the safety net for an as-yet-unseen
failure/terminal string. Do not tighten `_FAILED` against guesses.

**Media-path constraint (environment-specific, affects the transport decision).**
The presigned-upload path (`media_upload → PUT bytes → media_confirm`,
`_upload_local`) was **blocked in the Claude sandbox**: the egress proxy
policy-denied the direct PUT to `upload.higgsfield.ai` (CONNECT 403,
non-retryable). The run completed via the other real branch —
`media_import_url` on a public URL → media_id — which works in-sandbox.
Consequence: `_upload_local` is coded and shape-verified but **not
end-to-end-tested**; it is expected to work in a normal deployed container (no
such egress restriction) and must be verified there. The presigned URL signs
`content-type`, so `_put_bytes` sends the matching `Content-Type` header.
Confirmed shapes: `media_upload` → `{"uploads":[{"upload_url","media_id","url",
"content_type","expires_in_seconds","method":"PUT",...}]}`; `media_import_url` →
`{"media_id","type","content_type","source_url"}`. Per-model start-image role:
`start_image` for Seedance/Kling/Veo, `image_references` for Wan.

**Live result record (P12.4) — the reference artifact for the deferred
`TODO(P13-live)` run.** Job `58c50606-ee3a-46cd-8904-8e4e99bd0298` (wan2_6, 5s,
`completed`); prompt "Abstract ink dispersing in water… Camera: locked-off static
camera."; spend 13 credits (100 → 87), matching the `get_cost` preflight exactly.
Asset (at `results[0].results.rawUrl`):

```
https://d8j0ntlcm91z4.cloudfront.net/user_3FdggLn5IvFD0ORQbHZhSgip7Mf/hf_20260706_160230_58c50606-ee3a-46cd-8904-8e4e99bd0298.mp4
```

Recorded here because it is the input the real-CLIP verification needs and is not
otherwise reconstructible — it survived the deletion of `P12.4_RESUME.md` only in
git history (`git show 415bdd6`). Source image: `assets/test.png` (committed).

**Reliability note.** Higgsfield's live server returned transient "Something went
wrong" errors frequently (media_upload failed 3× before succeeding, and the first
get_cost retried once). The tenacity 5xx/timeout seam is load-bearing, not
decorative.

**P12 build sequence:** P12.1 rename (✅) → P12.1b duration realism (✅) →
P12.2 preflight_cost seam (✅) → P12.3 real adapter (✅) → P12.4 one deliberate
cheap real shot (Wan 2.6 @ 5s, **manually triggered by the operator** — the
first paid call is a conscious act, and it doubles as the status-vocabulary
discovery run, resolving the TODO(P12.4) markers in poll()).

### Mock-mode planning

`MockVisionProvider` returns a canned SceneModel and cannot serve the planner,
which needs a six-shot plan. Mock mode therefore uses a dedicated
`MockPlanProvider` (a VisionProvider that returns a canned plan), selected at the
pipeline injection point — vision gets `MockVisionProvider`, the planner gets
`MockPlanProvider`. Each mock stays single-purpose and no `if mock` branch leaks
into the phases. `MockPlanProvider` is non-frozen (not part of
vision_providers.py).

---

## 8. Verification harness

Three properties need verifying, and each needs a different tool — no single
reviewer covers all three.

| Property | Question | Verified by |
|---|---|---|
| Working state | Does it run? | Execution — `da run --mock` + pytest in CI |
| Functional authenticity | Does it do the right thing? | Behavioral tests (executor + pipeline) |
| Integrity | Does it match the locked design? | Static invariant gates + adversarial AI review |

**Ordering rule:** deterministic gates are the source of truth; AI review sits on
top and can only raise concerns, never grant a pass. A red gate is never
overridden by a green review. The full policy, cadence (gates every commit;
adversarial review only on risky steps P2/P7/P9; full sweeps at milestones), and
the reusable review prompt live in `VERIFICATION.md`.

CI runs light: torch and open-clip live in an optional `clip` extra, so mock
mode and tests install in seconds with no 2GB download. The lazy-load design
(torch imported only inside the CLIP scorer, never at module level) is what makes
this possible and is itself an enforced invariant.

### The CI mock pipeline (`.github/workflows/ci.yml`)

On every push and PR, CI runs, in order: `ruff check`, `pytest` (which includes
`test_invariants.py`), then an **end-to-end mock pipeline run** (`da run … --mock`
against a generated 1×1 test image) asserting `storyboard.json` is written, then
an **idempotent resume** (`da resume … --mock`) asserting no new work. No
credentials, no torch. This is the backbone that proves *working state* — that
the whole photo→storyboard chain actually executes, not just that units pass.

**Guarded skip until the CLI exists.** The two pipeline steps are gated on
`src/directoragent/cli.py` being present (with a `command -v da` secondary check).
Before STEP 10 they skip cleanly so CI stays green on what exists; the lint and
test steps always run unconditionally. **STEP 10 (cli.py) is the milestone that
flips these steps from skip to run** — the first push after `cli.py` lands is the
first time the full pipeline executes in CI, so wiring issues in the end-to-end
path surface there. Gating on the source file (not only `command -v da`) is
deliberate: `pip install -e .` generates the `da` console-script wrapper from the
entry point *before* `cli.py` is written, so the command check alone would report
a false positive.

---

## 9. Build sequence & current status

| Step | Artifact | Status |
|---|---|---|
| P0–P4 | Restructure, harness, config, store, mocks | ✅ Done |
| — | shot_style / render_class split | ✅ Done |
| P5–P8 | vision, planner (+arcs/motion/mock_plan), executor, assembler | ✅ Done |
| P9 | pipeline.py (+ per-attempt cost persistence) | ✅ Done |
| P10 | cli.py (--arc, --review); CI mock-pipeline gate flipped to run | ✅ Done |
| P11 | test suite (19 → 21 tests; asyncio_mode fix) | ✅ Done |
| P12.1 | Model rename SOUL_V2 → SEEDANCE_2 (foundation + hashes) | ✅ Done |
| P12.1b | model_limits.py + planner duration clamp; total-duration bound removed | ✅ Done |
| P12.2 | preflight_cost on the Protocol + mock + pipeline/executor wiring | ✅ Done |
| P12.3 | clients/higgsfield.py — real MCP adapter (call_tool seam, catalog mapping, media handshake, motion-into-prompt, defensive status norm, fingerprint reconcile, get_cost preflight) | ✅ Done |
| P12.4 | First cheap real shot — Wan 2.6 @ 5s, 13 credits; confirmed status vocabulary + response envelope + media-path constraint | ✅ Done |
| P12.5 | REST transport (higgsfield_rest_transport.py, KEY_ID:KEY_SECRET, SDK-derived contract, envelope normalization) — built + stub-tested; live smoke deferred (egress-blocked) | ✅ Done |
| P12.5-live | First out-of-sandbox REST run: confirm per-model submit endpoint (placeholder now) + resolve get_cost-over-REST gap | ⬜ At deploy/allowlist |
| P13 | drift/clip_scorer.py (real CLIP — lazy torch, PyAV mid-point frame, pure-Python cosine, fail→0.0) | ✅ Done |
| P13-live | Verify real CLIP scoring on an actual Higgsfield .mp4 (needs a real generated video) | ⬜ At deploy/real-run |
| P14 | README + .env.example + TODO sweep + final gates | ✅ Done |
| P14.1 | Run-status lifecycle fix (PLANNING→EXECUTING→COMPLETE persisted; ABORTED unwired by design) | ✅ Done |
| — | Milestone full-integrity sweep (separate session, whole-tree) | ✅ Done — 8 concerns found |
| SWEEP-FIX-1 | C1,C3,C4,C5,C6 (plan-review contract, reconcile cost, budget race, status lifecycle, orphan rows) | ✅ Done |
| SWEEP-FIX-2 | C2,C7 (Protocol reconciliation + crash-safe reconcile) — approved foundation reopening | ✅ Done |
| — | Focused re-verification (separate session) | ✅ **All 8 resolved, gates green** (58 tests) |

A working mock demo exists at P10; CI-green regression coverage at P11; real
generation from P12.

---

## 10. Locked design decisions & rationale

| Decision | Rationale |
|---|---|
| Bare async Python (no LangGraph/Mastra) | The flow is a deterministic DAG with one fan-out; a graph engine would be unused weight. The systems narrative reads better than a framework wrapper and plays in both generalist and agent-shop reviews. |
| Always-on container (not serverless/Vercel) | Jobs run for minutes with retries; serverless time limits and the inability to bundle torch force a queue redesign + hosted embeddings. A long-lived process holds job state simply. |
| SQLite default, Postgres swappable behind StateStore | Clone-and-run with zero infra, yet production-swappable via config. SQLite transactions also remove the JSON-file corruption risk under concurrent coroutine writes. |
| Immutable attempt rows | First-try-yield and drift history come for free; a mutable status field discards them. |
| Level-1 durability + idempotent submit | Scopes durability to the only expensive, irreversible side effect; reconcile prevents double-charge in the crash window. |
| Vision as a standalone provider-agnostic service | Lower coupling than wrapping MCP; lets users bring their own model via a one-method transport. |
| Scene model **and** photo to the planner | Groundedness is the product premise; the small token cost is justified by visually faithful shots. |
| shot_style / render_class split | Unbounded creative vocabulary while routing stays deterministic and the LLM never picks the model. |
| Named swappable arc templates | Supports non-action scenes; makes default/named/user-defined arcs one code path; surfaces the arc to the user. |
| camera_motion free-text + motion_preset provisional closed enum | Same move, two representations: expressive prose for the prompt, a mappable value for the API. Mirrors shot_style/render_class. The provisional set lives in mutable planner code, not the frozen schema, because it reconciles against the real API at P12 — reconciled: no motion enum exists; the field is metadata + a prompt hint the adapter folds into prompt text. |
| Plan-review checkpoint before spend | User sees the exact Higgsfield prompts and approves before any paid generation; aligns with the cost-ceiling philosophy. |
| Two separate retry mechanisms | Prevents double-counted cost and swallowed quality failures; keeps metrics meaningful. |

---

## 11. Deferred & future work (with rationale)

| Item | Status / why deferred |
|---|---|
| Real Higgsfield adapter (P12) | ✅ BUILT (P12.3), proven live at P12.4 (agent-mediated). |
| REST transport (P12.5) | ✅ BUILT + stub-tested; live verification deploy-gated (see live table below). |
| Real CLIP scorer (P13) | ✅ BUILT; real-model-on-real-video verification deploy-gated (see below). |
| User-defined custom arcs | The template mechanism is built v1 (default + named); accepting arbitrary user beat-lists is additive and not needed to prove the concept. Stubbed behind the same arc interface. |
| Shot-to-shot chaining (PREVIOUS_SHOT references at generation time) | True chaining serialises parts of the fan-out and introduces an ordering dependency. v1 resolves all generation references to the source photo and records reference.type as metadata only, keeping the fan-out fully parallel. |
| Automated shot_style↔render_class consistency check | Trusting the validated render_class is simpler and avoids false corrections from a brittle classifier. Prevention (prompt rubric), detection (plan review), and containment (retry + cost ceiling) cover the gap. Build only if real runs show a high mismatch rate. |
| motion_preset ↔ Higgsfield reconciliation | RESOLVED at P12: no motion enum on the API; motion_preset is internal metadata + prompt hint (§7). Retained for a future preset_id mapping. |
| Multi-frame drift sampling | v1 scores a single mid-point video frame (cheap, correct baseline). Multi-frame sampling is a data-driven refinement if real drift scores don't track perceived fidelity. |
| render_class expansion beyond four | Cardinality tracks the model roster; four models exist today. Adding a class is a localized, on-demand routine (§12). |
| Postgres backend | The StateStore protocol makes it a config-time swap; SQLite covers development and the demo. |
| Cost-model calibration | COST_PER_SECOND placeholders; real credits come from get_cost (MCP) — but see the REST get_cost gap below. |

### Integrity sweep record (whole-tree adversarial review, post-P14.1)

All deterministic gates were green and every checklist invariant passed — and the
sweep still surfaced **eight concerns**, three of them live-reproduced defects no
unit test covered. This is the documented reason the AI-review layer sits on top
of the gates rather than being replaced by them.

| # | Concern | Resolution |
|---|---|---|
| C1 | `--review` on an *existing* run-id silently executed and spent (plan_only early-return lived only in the fresh-run branch). Reproduced live. | Fixed (SWEEP-FIX-1): `elif plan_only` branch prints the plan and returns None on existing runs — no status write, no executor. |
| C2 | Real-adapter `reconcile` used an **in-memory** fingerprint map, so it could never survive the cross-process crash it exists for. CLAUDE.md's "prompts are unique per attempt" premise was also false in code (identical across quality-retries). | Fixed (SWEEP-FIX-2, Protocol reopening): `reconcile(idem_key, shot)` derives the fingerprint from the persisted Shot at call time; exactly-one match → recover, zero-or-multiple → None → fresh submit (rare double-charge accepted). Doc premise corrected. |
| C3 | Reconcile-recovered submissions never called `add_cost` — Higgsfield charged, the ledger did not. | Fixed (SWEEP-FIX-1): recovery branch charges exactly once and persists `attempt.cost`. |
| C4 | Budget guard raced across the fan-out: six coroutines read a stale shared total, so the ceiling could be overshot by ~5 shots. | Fixed (SWEEP-FIX-1): shared `asyncio.Lock`; preflight + ceiling check + cost reservation are atomic; rollback if submit fails pre-`add_cost`. In-memory total = DB total + outstanding reservations. |
| C5 | Resuming a COMPLETE run transiently rewrote status to EXECUTING. | Fixed (SWEEP-FIX-1): EXECUTING write gated on `status != COMPLETE`. |
| C6 | Orphaned SUBMITTING rows (reconcile → None) stayed non-terminal forever while consuming a retry slot. | Fixed (SWEEP-FIX-1): closed as FAILED_ERROR with an explanatory error before the next attempt opens. |
| C7 | `update_run_status`/`list_runs` were implementation extras, not on the frozen StateStore Protocol — quietly falsifying the "Postgres swap = implement the Protocol" claim. | Fixed (SWEEP-FIX-2): both added to the Protocol; hash regen in the same commit. |
| C8 | REST real mode is not merely "missing a cost gate" — `preflight_cost` raises at the first pipeline step, so it is **fully inoperative**. | Accepted as deploy-gated (`TODO(P12.5-live)` #2); wording sharpened across README/CLAUDE.md/docs. |

**Focused re-verification (separate session):** all eight confirmed resolved with
file:line evidence; C1 and C4 re-reproduced live; foundation hashes MATCH after the
approved `protocols.py` reopening; 58 tests green; `import directoragent` torch-free.

### Deploy-gated live verification — status

Three of the original four items are now CLOSED. The system is deployed and live at
**https://directoragent.onrender.com/docs** (mock-mode default). One optional item
remains: a first paid generation over the REST transport.

| Marker | Status |
|---|---|
| `TODO(P12.5-live)` #1 (submit endpoint) | ✅ **RESOLVED** against Higgsfield's authenticated Cloud API docs. Real route is `POST /{model_id}` (e.g. `higgsfield-ai/dop/standard`), not the earlier `/v2/generate` placeholder. Wired in D2. |
| `TODO(P12.5-live)` #2 (REST get_cost) | ✅ **RESOLVED** — the Cloud API genuinely has no cost endpoint (only submit/status/cancel exist). REST projects cost from the static COST_PER_SECOND table and reconciles actual credits post-submit; `nsfw`/`failed` refund, so reconciliation reads the real terminal status. Wired in D2. |
| `TODO(P13-live)` (real CLIP) | ✅ **CLOSED** — ran the real model: seed image vs its generated `.mp4` = **0.89** (clears ABSTRACT_FLUID 0.65 with margin, not a suspicious ~1.0); unrelated white-PNG control = **0.42** (proves discrimination). Note: weight download may route through the Xet CDN (`cas-server.xethub.hf.co`); set `HF_HUB_DISABLE_XET=1` or allowlist that host in restricted networks. |
| First paid REST submit (`_upload_local` + one real DoP shot) | ⬜ **OPTIONAL** — the only remaining item. Would add a real DoP clip via the REST path alongside the already-proven MCP generation, and exercise the presigned-PUT upload end to end. Deferred as a deliberate spend decision, not a code gap. Requires funding cloud.higgsfield.ai API credits (a separate credit pool from the MCP plus-plan). |

**Two-catalog reality (confirmed at deploy):** the MCP connector (higgsfield.ai)
exposes Seedance/Kling/Veo/Wan; the REST Cloud API (cloud.higgsfield.ai) exposes
Higgsfield's own DoP/Soul models under a different taxonomy, billed from a separate
credit pool. One `call_tool` seam abstracts two genuinely different Higgsfield
products. One render_class (COMPLEX_MOTION → DoP Standard, whose signature is a real
`motions` camera-motion parameter) is mapped as a working demonstration; the other
three raise a clear "no REST model mapping" error rather than hitting a wrong URL.
Full render_class→DoP mapping is a documented config step.

**REST reconcile limitation:** the Cloud API has no request-history endpoint, so
`reconcile` cannot recover an orphaned job over REST. A crash in the SUBMITTING
window followed by a resume fails loudly over REST rather than silently
double-charging — the safer failure, documented rather than hidden.

### The deploy (D-series)

The CLI was wrapped in a thin FastAPI layer (`web.py`) exposing the plan → execute
→ status lifecycle over HTTP: `POST /plan` (plan-only, returns the six-shot plan +
costs), `POST /runs/{id}/execute` (non-blocking — returns 202, runs on a background
thread since real generation takes minutes), `GET /runs/{id}` (polls durable state),
`GET /runs`, `GET /healthz`. Deployed on Render's native Python buildpack via
`render.yaml`, mock-mode default. Input handling was hardened (D3c): the Swagger
example is executable on first click, junk optional values are treated as absent,
and errors return structured 4xx/500 with detail rather than an opaque 500. Live
demo: https://directoragent.onrender.com/docs.

---

## 12. Extension points (how to extend without breaking invariants)

- **Add a vision model:** implement `VisionProvider.complete(image_path, prompt)`
  and register it in `make_provider()`. No other file changes.
- **Add a render_class / routing target:** add the enum value (schema), add
  ROUTING + DRIFT_THRESHOLDS + COST_PER_SECOND entries (routing.py), extend the
  planner's allowed-list and guidance, add a routing test, regenerate foundation
  hashes. The executor/store/assembler are untouched.
- **Add a state backend:** implement the StateStore protocol (e.g. asyncpg) and
  select it at the pipeline injection point. Phases are unaffected.
- **Add an arc template:** add a named six-beat list (name/intent/render_lean
  per beat) to the arc library module. The planner consumes it as data; no
  prompt rewrite, no routing change, no foundation touch.
- **Add a motion preset:** add the value to `phases/motion.py` and the planner
  allowed-list. At P12, ensure it maps to a real Higgsfield preset. No
  foundation touch.
- **Swap mock for real generation/scoring:** controlled entirely by
  `settings.mock_mode` at the pipeline injection point. No `if mock` branches
  are permitted inside phases.

---

## 13. Glossary

- **Render class** — the closed routing key mapping a shot to a Higgsfield model
  and drift threshold. Deterministic; never chosen by the LLM directly as a
  model.
- **Shot style** — the free-form cinematic descriptor chosen by the LLM.
  Unbounded; carries no routing meaning.
- **Drift** — CLIP cosine similarity between a generated frame and the source
  photo; below the per-shot threshold triggers a quality retry.
- **Crash window** — the interval between recording intent to submit
  (SUBMITTING) and recording the returned job_id (RUNNING); covered by reconcile.
- **Quality retry vs transient retry** — a new attempt row on drift failure vs a
  same-row backoff on a network error; see §6.
- **Level-1 durability** — persisting scene + plan + job ledger only, not full
  step checkpointing.
- **Foundation files** — the four frozen contract files (schema, protocols,
  routing, schema.sql) guarded by hash.
