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
| `phases/vision.py` | VisionClient (wraps a VisionProvider; prompt + parse + validate + repair) | Planned (P5) |
| `state/sqlite_store.py` | StateStore (aiosqlite, Level-1 ledger) | Built (P2) |
| `clients/higgsfield_mock.py` | HiggsfieldClient (no network; supports reconcile) | Built (P3a) |
| `clients/higgsfield.py` | HiggsfieldClient (real MCP adapter, tenacity transient-retry) | Planned (P12) |
| `drift/mock_scorer.py` | DriftScorer (synthetic, deterministic fail-then-pass) | Built (P4a) |
| `drift/clip_scorer.py` | DriftScorer (lazy open_clip + torch) | Planned (P13) |

### Layer C — Orchestration

| File | Role | Status |
|---|---|---|
| `phases/planner.py` | SceneModel + photo ─► 6 × Shot; arc skeleton + deterministic routing | Planned (P6) |
| `phases/executor.py` | Fan-out, submit/poll, drift scoring, quality-retry, resume | Planned (P7) |
| `phases/assembler.py` | RunState ─► Storyboard; metrics; shot grid; storyboard.json | Planned (P8) |
| `pipeline.py` | Wires the five phases; owns the cost ceiling; selects mock vs real impls | Planned (P9) |
| `cli.py` | Typer app: run / resume / status / list; constructs Settings; prints run_id | Planned (P10) |
| `config.py` | pydantic-settings Settings; constructed once at the CLI boundary | Built (P1) |

### Layer D — Verification

| File | Role | Status |
|---|---|---|
| `.github/workflows/ci.yml` | Deterministic gates on every push: ruff, pytest, mock pipeline, idempotent resume | Built |
| `tests/test_invariants.py` | Static invariants as executable checks (no module-level torch, routing-call locality, no utcnow, no settings singleton, foundation-hash guard) | Built |
| `tests/test_routing.py` | Routing/threshold/cost correctness | Planned (P11) |
| `tests/test_executor.py` | Behavioral invariants: second attempt on drift-fail, no resubmit on PASSED, open_attempt-before-network, add_cost-once | Planned (P11) |
| `tests/test_pipeline_mock.py` | Full mock pipeline yields a valid Storyboard | Planned (P11) |

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
resume, a shot left in SUBMITTING is recovered via `reconcile(idem_key)`, which
asks Higgsfield whether a job for that idempotency key already exists — avoiding
a double-submit (and double-charge) at the worst possible moment.

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

## 7. Routing & the shot_style / render_class split

### The problem this solves

A single field was doing two unrelated jobs: describing the shot cinematically
*and* serving as the routing key that selects a Higgsfield model and drift
threshold. That conflation caps creative vocabulary at the number of routing
targets, which is wrong — cinematic variety is unbounded, routing targets are
not.

### The decision (LOCKED — pending implementation before P6)

Split the two jobs into two fields, "guarded-(a)":

- **`shot_style`** — free-form text the LLM chooses with wide latitude
  (close-up, over-the-shoulder dolly, crane-out, abstract transition, …).
  Unbounded; adding values costs nothing.
- **`render_class`** — a closed enum (currently FACE, COMPLEX_MOTION,
  ABSTRACT_FLUID, WIDE_ENVIRONMENT) that is the routing key. The LLM proposes a
  render_class per shot; the planner **validates** it against the closed set and
  falls back to a default mapping if the LLM emits an unknown value. The model
  is then derived deterministically via `route(render_class)` — the LLM never
  picks the model, preserving the core invariant.

Recommended implementation is to rename the existing `shot_type` → `render_class`
for clarity and add `shot_style`. The lower-churn alternative (keep `shot_type`
as the routing key, only add `shot_style`) is acceptable if minimum blast radius
is preferred; in that case `shot_type` is documented as "the routing class."

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

---

## 9. Build sequence & current status

| Step | Artifact | Status |
|---|---|---|
| P0 | Package restructure, pyproject, gitignore | ✅ Done |
| — | Verification harness (CI, invariant gates, dep-split, hashes) | ✅ Done |
| P1 | config.py | ✅ Done |
| P2 | state/sqlite_store.py | ✅ Done |
| P3a | clients/higgsfield_mock.py | ✅ Done |
| P4a | drift/mock_scorer.py | ✅ Done |
| — | **shot_style / render_class split** | ⏳ Decided, implement before P6 |
| P5 | phases/vision.py | ▶ Next |
| P6 | phases/planner.py (arc library + split + routing) | Planned |
| P7 | phases/executor.py | Planned |
| P8 | phases/assembler.py | Planned |
| P9 | pipeline.py | Planned |
| P10 | cli.py (incl. plan-review checkpoint) | Planned |
| P11 | test suite | Planned |
| P12 | clients/higgsfield.py (real MCP) | Planned |
| P13 | drift/clip_scorer.py (real CLIP) | Planned |
| P14 | README, finalize, full integrity sweep | Planned |

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
| Plan-review checkpoint before spend | User sees the exact Higgsfield prompts and approves before any paid generation; aligns with the cost-ceiling philosophy. |
| Two separate retry mechanisms | Prevents double-counted cost and swallowed quality failures; keeps metrics meaningful. |

---

## 11. Deferred & future work (with rationale)

| Item | Why deferred |
|---|---|
| Real Higgsfield adapter (P12) | Mock-first unblocks the entire pipeline with zero credentials/cost; the real adapter is a drop-in once tool names are confirmed by inspecting the MCP. |
| Real CLIP scorer (P13) | Same mock-first reasoning; torch is heavy and only needed for real drift scoring. |
| User-defined custom arcs | The template mechanism is built v1 (default + named); accepting arbitrary user beat-lists is additive and not needed to prove the concept. Stubbed behind the same arc interface. |
| Shot-to-shot chaining (PREVIOUS_SHOT references at generation time) | True chaining serialises parts of the fan-out and introduces an ordering dependency. v1 resolves all generation references to the source photo and records reference.type as metadata only, keeping the fan-out fully parallel. |
| render_class expansion beyond four | Cardinality tracks the model roster; four models exist today. Adding a class is a localized, on-demand routine (see §12), not a v1 concern. |
| Postgres backend | The StateStore protocol makes it a config-time swap; SQLite covers development and the demo. |
| Cost-model calibration | COST_PER_SECOND values are placeholders until validated against real Higgsfield pricing; required before trusting `--max-cost` in real mode. |

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
