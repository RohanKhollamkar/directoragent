# DirectorAgent — Consolidated Build Runbook (P0–P14)

Single source of truth for the build sequence. Reconciled with every locked
design decision (the shot_style/render_class split, the planner design, the
verification harness). Companion docs: `CLAUDE.md` (operational anchor),
`docs/TECHNICAL_DOCUMENTATION.md` (architecture & rationale), `VERIFICATION.md`
(gates, cadence, review prompt).

**How to use:** paste one step's prompt into Claude Code, let it run, review the
PR (only intended files; no foundation file touched unless the step says so),
confirm CI green, merge, then next. Every prompt ends with "list changes, then
stop" so each step is reviewable before merge.

**Status legend:** ✅ done · ▶ next · ⬜ planned

---

## Status snapshot

| Step | Artifact | Status |
|---|---|---|
| P0 | Package restructure + pyproject + gitignore | ✅ |
| — | Verification harness (CI, invariant gates, dep-split, foundation hashes) | ✅ |
| P1 | config.py | ✅ |
| P2 | state/sqlite_store.py | ✅ |
| P3 | store round-trip smoke check | ✅ |
| P4 | clients/higgsfield_mock.py + drift/mock_scorer.py | ✅ |
| — | shot_style / render_class split (schema/routing/storage) | ✅ |
| P5 | phases/vision.py | ✅ |
| P6 | phases/planner.py + arcs.py + motion.py + mock_plan_provider.py | ✅ |
| P7 | phases/executor.py | ✅ |
| P8 | phases/assembler.py | ✅ |
| P9 | pipeline.py (+ per-attempt cost persistence; resume-based plan-review) | ✅ |
| P10 | cli.py (--arc, --review; CI mock-pipeline gate flipped to run) | ✅ |
| P11 | test suite (asyncio_mode fix; invariant + behavioral coverage) | ✅ |
| P12.1 | Model rename SOUL_V2 → SEEDANCE_2 (foundation + hashes) | ✅ |
| P12.1b | model_limits.py + planner duration clamp; total-duration bound removed | ✅ |
| P12.2 / 12.2b | preflight_cost seam + plan-review cost display from the same source | ✅ |
| P12.3 | clients/higgsfield.py — real MCP adapter (call_tool seam) | ✅ |
| P12.4 | First real shot (agent-mediated, Wan 2.6 @ 5s, 13 credits) — status vocab + envelope + media shapes confirmed | ✅ |
| P12.5 | REST transport (SDK-derived contract, stub-tested; live smoke deploy-gated) | ✅ |
| P13 | drift/clip_scorer.py (real CLIP, lazy torch, PyAV mid-point frame) | ✅ |
| P14 | README + .env.example + TODO sweep + final gates | ✅ |
| P14.1 | Run-status lifecycle fix (PLANNING→EXECUTING→COMPLETE written to DB) | ▶ |
| — | Milestone full-integrity sweep (VERIFICATION.md, whole-tree) | ⬜ Final gate |
| — | Deploy-gated live items (P12.5-live ×2, P13-live, _upload_local) | ⬜ At deploy |

> **Execution note:** the original P12/P13/P14 prompts below are the pre-discovery
> versions, retained as historical record. P12 was actually executed as
> discovery-driven sub-steps (P12.1 → P12.5) after live API inspection changed
> several assumptions (SOUL_V2 absent from the catalog; per-model duration caps;
> no idempotency key; no motion enum; two-transport reality). The authoritative
> current-state reference is docs/TECHNICAL_DOCUMENTATION.md (§9 status, §11
> deploy-gated table).

---

## Completed steps (record)

**P0 — Restructure.** Package laid out under `src/directoragent/`; foundation
files moved in with imports rewritten to `from directoragent.X import ...`;
`pyproject.toml` and `.gitignore` created.

**Verification harness.** `.github/workflows/ci.yml` (ruff + pytest + mock
pipeline + idempotent resume, guarded so pipeline steps skip until `cli.py`
exists), `tests/test_invariants.py` (static invariant gates), dependency split
(torch/open-clip moved to a `clip` extra; `dev` extra added),
`tests/foundation_hashes.json` frozen. CI is green on the current tree.

**P1 — config.py.** pydantic-settings `Settings`; constructed once at the CLI
boundary and injected (no module-level global).

**P2 — sqlite_store.py.** StateStore over aiosqlite; one long-lived connection;
open_attempt→SUBMITTING before network; record_job_id→RUNNING; atomic add_cost;
load_run reconstructs RunState with attempts ordered by attempt_number.

**P3 — store smoke check.** Round-trip script proved crash-window persistence.
(Deleted at P11.)

**P4 — mocks.** MockHiggsfieldClient (supports reconcile) and MockDriftScorer
(`fail_first` to exercise the quality-retry path).

**Split — shot_style / render_class.** `ShotType`→`RenderClass`; `Shot` gained
free-form `shot_style` and routing-key `render_class`; schema.sql, sqlite_store,
and foundation hashes updated. The LLM proposes render_class; `route()` derives
the model. (See TECHNICAL_DOCUMENTATION §7.)

**P5 — phases/vision.py.** VisionExtractor wraps a VisionProvider; builds the
extraction prompt from an internal `_SceneExtraction` schema, parses/validates
into SceneModel, with one repair attempt on failure. `source_photo_path` is
attached to the returned SceneModel.

---

## P6 — Planner (the design-loaded step)

Creates four non-frozen modules under `phases/`. This replaces the old
placeholder; the standalone `P6_PLANNER_PROMPT.md` is retired in favor of this.

```
STEP 6 — the planner. The most design-loaded file in the build; implement exactly to spec. Read CLAUDE.md first. Do NOT modify any foundation file (schema.py, protocols.py, routing.py, schema.sql, vision_providers.py). Create four new non-frozen modules under phases/.

=== FILE 1: phases/arcs.py — arc library (plain data) ===
ARC_LIBRARY: dict[str, list[beat]]; each arc has EXACTLY 6 beats; each beat carries name (str), intent (str, one line), render_lean (RenderClass from directoragent.schema). render_lean is a SOFT default, not binding.
- "dramatic" (default/fallback):
  1 establish · set the place · WIDE_ENVIRONMENT
  2 introduce subject · bring the subject in · FACE
  3 rising detail · build tension through detail · COMPLEX_MOTION
  4 turn · the pivot · FACE
  5 peak · height of the sequence · COMPLEX_MOTION
  6 resolution · settle and close · WIDE_ENVIRONMENT
- "observational":
  1 wide establish · WIDE_ENVIRONMENT
  2 group framing · WIDE_ENVIRONMENT
  3 individual reaction · FACE
  4 exchange detail · FACE
  5 pull back · COMPLEX_MOTION
  6 environmental drift · WIDE_ENVIRONMENT
- "reveal":
  1 conceal · FACE
  2 hint · ABSTRACT_FLUID
  3 approach · COMPLEX_MOTION
  4 widening context · WIDE_ENVIRONMENT
  5 the reveal · FACE
  6 aftermath · WIDE_ENVIRONMENT
- "mood-piece":
  1 texture open · ABSTRACT_FLUID
  2 subject in space · WIDE_ENVIRONMENT
  3 light/detail · FACE
  4 abstract interlude · ABSTRACT_FLUID
  5 subject return · FACE
  6 lingering close · FACE
Provide get_arc(name) -> list[beat] (KeyError if unknown) and DEFAULT_ARC = "dramatic".

=== FILE 2: phases/motion.py — provisional motion-preset vocabulary ===
MOTION_PRESETS = {PUSH_IN, PULL_OUT, PAN_LEFT, PAN_RIGHT, TILT_UP, TILT_DOWN, ORBIT, STATIC, HANDHELD, CRANE} (frozenset of strings or a str Enum). Add on the definition:
  # TODO(P12): reconcile these provisional presets against the real Higgsfield motion vocabulary; rename to match if needed.
Provide normalize_motion(value) -> str: upper/trim, return a valid preset else "STATIC".

=== FILE 3: phases/mock_plan_provider.py — MockPlanProvider ===
A VisionProvider (`async def complete(self, image_path, prompt) -> str`) returning a canned, valid 6-shot plan as JSON matching the planner output schema below. Required because MockVisionProvider returns a SCENE and cannot serve the planner. Non-frozen.

=== FILE 4: phases/planner.py — the planner ===
async def plan(scene: SceneModel, description: str, provider: VisionProvider, run_id: str, arc_name: str | None = None) -> list[Shot]

A) Arc selection (one LLM call, folded into planning):
   - arc_name given and in ARC_LIBRARY -> use it.
   - else the LLM picks the best-fitting arc from the four (from scene + description) with a one-line reason; invalid/ambiguous -> DEFAULT_ARC.
   - Log the selected arc + reason (for the P10 plan-review checkpoint).

B) Build the planning prompt as a VISION call: pass image_path = scene.source_photo_path. Provide scene model fields, the chosen arc's 6 beats (name + intent + render_lean), and the rules below. Demand JSON only.
   Per-shot fields the LLM returns: shot_name, shot_style (free descriptor), render_class (PROPOSED; one of FACE/COMPLEX_MOTION/ABSTRACT_FLUID/WIDE_ENVIRONMENT), narrative_beat (beat name), camera_motion (free prose), motion_preset (one of MOTION_PRESETS), prompt (filmable), duration_s, quality (draft|standard|high).

   FILMABLE-PROMPT RUBRIC (include verbatim with the example):
   - 2–4 sentences, present tense, dense visual nouns/verbs, no narrative connective tissue.
   - Representational shots MUST include: subject + concrete observable action; framing/shot scale; camera movement; lighting/atmosphere (from the scene model); a setting anchor in the established environment.
   - ABSTRACT_FLUID shots: replace "subject + action" with "visual motif + motion quality" and relax the setting anchor; framing, camera movement, lighting still apply.
   - FORBIDDEN: internal states, plot/backstory, unfilmable abstraction, dialogue. A logline is rejected.
   - EXAMPLE — Unfilmable: "The detective realizes she's been betrayed." Filmable: "Medium close-up, slow push-in on the detective's face under a single overhead bulb; her eyes shift left, jaw tightening; cold blue light, deep shadows behind."

   GROUNDEDNESS CONTRACT (include):
   - Subject identity stays stable across representational shots (same character); reference it CONCISELY — do NOT exhaustively redescribe appearance (the source photo is the identity reference passed to generation; over-describing fights it).
   - Palette/lighting anchored to the scene's world; may modulate with the beat.
   - Framing, angle, action free to vary.
   - WIDE_ENVIRONMENT: lighter subject binding, emphasize establishing the world. ABSTRACT_FLUID: drop subject binding, keep the palette/mood anchor.

   RENDER_CLASS RUBRIC (for coherent proposals): FACE = identity/expression-centric; WIDE_ENVIRONMENT = scale/establishing; COMPLEX_MOTION = significant subject or camera movement; ABSTRACT_FLUID = non-representational/fluid/transition. Default toward the beat's render_lean unless the shot clearly calls for another.

   DURATION: each shot 8–30s; total across 6 shots in [60, 180].

C) Build each Shot from the parsed JSON:
   - render_class GUARD: normalize the proposed value against the four enum members; if unresolved, fall back to the beat's render_lean and attach a visible note recording the fallback. No shot_style keyword parsing.
   - motion_preset: normalize_motion(...) (fallback STATIC).
   - quality: validate against QualityTier, fallback STANDARD.
   - DERIVE (never from the LLM): model, model_reason = route(render_class); min_drift_score = drift_threshold(render_class). If render_class differs from the beat's render_lean, append the reason to model_reason.
   - shot_id = f"shot_{i+1:02d}".
   - reference: shot 1 -> Reference(type=SOURCE_PHOTO, source=scene.source_photo_path); shots 2–6 -> same for v1 (no chaining; deferred).

D) Validate the plan: exactly 6 shots; every render_class valid; total duration in [60, 180]. On failure, retry ONCE with the error appended; on a second failure, raise with the raw response.

INVARIANTS: route() and drift_threshold() called ONLY here. The LLM never sets model or min_drift_score. No module-level torch. Planner sets no timestamps.

VERIFY: ruff check . and python -m pytest -q. Then a planner self-test with MockPlanProvider: plan() on a minimal SceneModel returns exactly 6 Shots, each with a valid render_class, a routed model, a valid motion_preset, and total duration in [60,180]. Paste raw output. List all files created/changed, then stop.
```

---

## P7 — phases/executor.py

```
STEP 7 — the executor (Phases 3+4), the most complex orchestration file. Read CLAUDE.md first. Create src/directoragent/phases/executor.py. Honor every invariant.

async run_all(state, store, hf, scorer, max_cost_usd): asyncio.Semaphore(MAX_CONCURRENT_JOBS); asyncio.gather a run_shot coroutine per shot (return_exceptions=False).

async run_shot(shot, state, store, hf, scorer, sem, max_cost_usd): acquire sem, then —
Resume via state.latest_attempt(shot.shot_id):
  * PASSED -> return.
  * status in IN_FLIGHT -> await _poll_to_terminal(last); if PASSED return.
  * status == SUBMITTING -> jid = await hf.reconcile(last.idem_key); if jid: record_job_id then _poll_to_terminal(last); if PASSED return.
Quality-retry loop: n = count of prior attempts; while n < MAX_ATTEMPTS_PER_SHOT:
  * budget guard: if state.total_cost + estimate_cost(shot.model, shot.duration_s) > max_cost_usd: break (leave shot unfinished).
  * n += 1; new Attempt (idem_key = f"{run_id}:{shot_id}:{n}", status SUBMITTING).
  * await store.open_attempt(attempt)                 # BEFORE any network call.
  * jid = await hf.submit(shot, attempt.idem_key)     # transient retry lives in the real adapter (tenacity), NOT here.
  * await store.record_job_id(attempt.attempt_id, jid).
  * new_total = await store.add_cost(run_id, estimate_cost(...)); update state.total_cost.   # ONCE per submission.
  * await _poll_to_terminal(attempt); if PASSED return.
  Append each attempt to state.attempts[shot_id].

async _poll_to_terminal(attempt, store, hf, scorer, state, shot):
  status = await hf.poll(attempt.job_id)
  if "succeeded": set SCORING; url = await hf.fetch_result(job_id); drift = await scorer.score(state.scene.source_photo_path, url); status = PASSED if drift >= shot.min_drift_score else FAILED_DRIFT; await store.update_attempt(..., drift_score=drift, result_url=url, status=status, completed_at=now); update the in-memory attempt; return.
  if "failed": set FAILED_ERROR, persist, return.
  else: await asyncio.sleep(poll_interval); loop.
Use datetime.now(timezone.utc). Drift reference is ALWAYS the source photo. The executor reads shot.model / shot.min_drift_score (set by the planner) and never calls route()/drift_threshold(). motion_preset and render_class are passed through opaquely.

Do not modify foundation files. Run ruff check . and python -m pytest -q; paste output. List changes, then stop.
```

---

## P8 — phases/assembler.py

```
STEP 8 — the assembler (Phase 5). Read CLAUDE.md first. Create src/directoragent/phases/assembler.py: function assemble(state: RunState) -> Storyboard.
- For each shot find its PASSED attempt. If a shot has NO passed attempt (budget exhausted), treat it as a failure — do NOT crash. For passed shots build ShotResult: result_url, drift_score, model, attempts_used = attempt_number, first_try = (attempt_number == 1), cost = sum of cost across ALL attempts for that shot_id.
- Storyboard: results = passed shots; total_cost from state; first_try_yield = (shots passed on attempt 1) / total shots; mean_drift = mean of passed drift scores (0.0 if none). Do NOT modify the Storyboard schema; report failed shot_ids via stdout/log. If all fail, still return a valid Storyboard with empty results.
- Write storyboard JSON to .directoragent/runs/{run_id}/storyboard.json (mkdir parents). Print a shot grid: shot_name | shot_style | render_class | model | drift | attempts | cost | result_url, then a summary line (first-try yield, mean drift, total cost, failures).
Do not modify schema.py. Run ruff check . and python -m pytest -q; paste output. List changes, then stop.
```

---

## P9 — pipeline.py

Reconciled: injects `MockPlanProvider` for the planner in mock mode, threads
`arc_name`, and supports the plan-review checkpoint via the resume path.

```
STEP 9 — pipeline wiring. Read CLAUDE.md first. Create src/directoragent/pipeline.py.

async def run(photo_path, description, run_id, settings, arc_name=None, plan_only=False) -> Storyboard | None:
  store = SqliteStateStore(settings.state_db_path)
  # Provider selection — mock mode uses TWO single-purpose mocks:
  if settings.mock_mode:
      vision_provider = MockVisionProvider()
      plan_provider   = MockPlanProvider()
      hf = MockHiggsfieldClient(); scorer = MockDriftScorer()
  else:
      shared = make_provider(settings.vision_provider, settings.vision_model)
      vision_provider = plan_provider = shared
      hf = HiggsfieldClient(settings.higgsfield_api_key)   # import lazily
      scorer = ClipDriftScorer()                            # import lazily (no torch in mock)
  vision = VisionExtractor(vision_provider)

  state = await store.load_run(run_id)
  if state is None:
      scene = await vision.extract_scene(photo_path)
      shots = await planner.plan(scene, description, plan_provider, run_id, arc_name=arc_name)
      projected = sum(estimate_cost(s.model, s.duration_s) for s in shots)
      if projected > settings.max_cost_usd: raise CostCeilingError(projected, settings.max_cost_usd)
      status = RunStatus.PLANNING if plan_only else RunStatus.EXECUTING
      state = RunState(run_id=run_id, status=status, scene=scene, input_description=description, shots=shots, created_at=datetime.now(timezone.utc))
      await store.create_run(state)
      for shot in shots: await store.save_shot(run_id, shot)
      if plan_only:
          print_plan(state, projected)   # arc + per-shot prompt/render_class/model/cost + projected total + run_id
          return None                    # stop before any spend; user runs `da resume <run_id>` to execute
  # (resume from PLANNING or EXECUTING both fall through to execution)
  await executor.run_all(state, store, hf, scorer, settings.max_cost_usd)
  return assembler.assemble(await store.load_run(run_id))

Define CostCeilingError and a print_plan helper. Real adapters imported lazily so mock mode never imports torch.
Do not modify foundation files. Run ruff check . and python -m pytest -q; paste output. List changes, then stop.
```

---

## P10 — cli.py

Reconciled: adds `--arc` (named arc) and `--review` (plan-only) and surfaces the
plan before spend.

```
STEP 10 — the CLI. Read CLAUDE.md first. Create src/directoragent/cli.py: a typer app.
  run <photo> <description> [--mock] [--max-cost N] [--provider openai|anthropic|gemini] [--arc dramatic|observational|reveal|mood-piece] [--review] [--run-id RUN_ID]
  resume <run_id> [--mock] [--max-cost N]
  status <run_id>
  list
- run: construct Settings applying flags as overrides; validate --arc against ARC_LIBRARY (error clearly if unknown); generate run_id (uuid4) if not given and PRINT it immediately; asyncio.run(pipeline.run(photo, description, run_id, settings, arc_name=arc, plan_only=review)). If --review, after the plan prints, tell the user: "Plan ready — review above, then run `da resume <run_id>` to generate."
- resume: load the run_id and re-enter pipeline.run with the same run_id (the persisted plan is loaded; execution proceeds). Same code path.
- status: load_run and print the per-shot attempt table (include shot_style, render_class, model, latest status, drift).
- list: print all runs (id, status, cost) from the runs table.
Register the `da` entry point. Do not modify foundation files. Run ruff check . and python -m pytest -q; paste output. List changes, then stop.
```

---

## P11 — test suite

Reconciled: adds planner tests (arc selection, render_class guard, motion
normalization) and the plan-review/resume path.

```
STEP 11 — tests. Read CLAUDE.md first. Create the suite under tests/ with pytest + pytest-asyncio (asyncio_mode=auto already configured):
- test_routing.py: route() maps each RenderClass to the correct Model; drift_threshold() returns the specced values; estimate_cost() math.
- test_planner.py: with MockPlanProvider — plan() returns exactly 6 valid Shots; an invalid proposed render_class falls back to the beat's render_lean; motion_preset normalizes (bad value -> STATIC); a named --arc is honored; model and min_drift_score come from routing, never from the provider; total duration in [60,180].
- test_executor.py: with MockHiggsfieldClient + MockDriftScorer(fail_first=1) — a drift-fail creates a SECOND attempt row and passes on attempt 2; a PASSED shot on resume is never re-submitted; open_attempt is called before submit (assert ordering); add_cost is called once per submission, not per poll.
- test_pipeline_mock.py: full pipeline.run(mock_mode=True) yields a Storyboard with 6 results, first_try_yield in [0,1], written storyboard.json. Plus a plan-review case: run with plan_only=True yields status PLANNING and no generation; a subsequent resume completes it.
Run python -m pytest -q; fix failures. Delete scripts/_smoke_store.py. Stop when green.
```

---

## P12 — clients/higgsfield.py (real MCP) + motion reconciliation

Reconciled: explicit `motion_preset` reconciliation step (the obligation parked
since P6).

```
STEP 12 — real Higgsfield adapter. Read CLAUDE.md first. Create clients/higgsfield.py: class HiggsfieldClient(api_key) implementing the HiggsfieldClient protocol against the real Higgsfield MCP tools.
1. Inspect the Higgsfield MCP tools available in this environment; list their names/signatures AND the real motion-preset vocabulary.
2. MOTION RECONCILIATION (TODO(P12)): map the provisional names in phases/motion.py onto the real presets. If names differ, update phases/motion.py and the planner allowed-list to match reality, and REMOVE the TODO(P12) marker. (motion.py is non-frozen — no hash regen.)
3. submit(shot, idem_key) -> the generation tool: select the model from shot.model; translate shot fields (prompt, duration_s, reference, motion_preset, camera_motion) into its parameters; pass idem_key as an idempotency key/param.
4. poll(job_id) -> normalize status strings to "running"|"succeeded"|"failed". fetch_result(job_id) -> result URL. reconcile(idem_key) -> existing job_id or None.
5. Wrap submit and poll with tenacity: @retry(wait=wait_exponential(...), stop=stop_after_attempt(4), reraise=True) — retry ONLY 5xx/timeout; 4xx raises immediately. This is the transient-retry layer; it lives here, never in the executor.
If tool names aren't discoverable, implement the full structure with clearly-marked TODOs plus an httpx fallback. Do not modify protocols.py. List changes and the discovered tool names + motion mapping, then stop.
```

---

## P13 — drift/clip_scorer.py (real CLIP)

```
STEP 13 — real CLIP scorer. Read CLAUDE.md first. Create drift/clip_scorer.py: class ClipDriftScorer implementing DriftScorer. Lazy-import torch and open_clip INSIDE a _ensure_model() helper called from score() — NEVER at module top level. Load model+preprocess once, cache on the instance. score(reference_path, candidate_url): if remote, download to temp via httpx; embed the reference (always the source photo) and the candidate with the CLIP image encoder; return cosine similarity (~[0,1]); return 0.0 gracefully on a bad candidate. Verify in a fresh process that `import directoragent` does NOT import torch. Install path: `pip install -e ".[clip]"`. Do not modify protocols.py. List changes, then stop.
```

---

## P14 — finalize

```
STEP 14 — finalize. Read CLAUDE.md first.
- Write README.md: what DirectorAgent is; the 5-phase architecture; mock quickstart (`pip install -e .` then `da run assets/test.png "..." --mock`); the arcs and the --arc / --review flow; env vars; the model-routing table; SQLite default with Postgres swappable behind StateStore.
- Ensure .env.example is complete; confirm all TODO(P12) markers are resolved.
- Run `ruff check --fix` and `python -m pytest -q`; confirm `da run ... --mock` is green and `da run ... --mock --review` then `da resume` completes.
- Summarize the final file tree and any remaining TODOs (cost calibration vs real pricing; shot-to-shot chaining; user-defined arcs; automated style↔class consistency check).
Stop. Then run the milestone full integrity sweep (VERIFICATION.md, whole-tree mode).
```

---

## Design-decision checkpoints that route back to the brainstorming context
- After P6: empirical planner tuning on real photos (filmability, groundedness, prompt length).
- At P12: confirm the Higgsfield tool/parameter mapping and the motion-preset reconciliation before trusting real spend.
- Before first real run: calibrate COST_PER_SECOND against real Higgsfield pricing so `--max-cost` is meaningful.
