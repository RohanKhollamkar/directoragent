# DirectorAgent

One photo + one scene description → a six-shot cinematic storyboard, generated
via Higgsfield. DirectorAgent runs a deterministic five-phase DAG — **Vision →
Planner → Executor + Drift → Assembler** — where an LLM proposes *what* each
shot is (style, prompt, narrative beat) but never *how* it is rendered: model
routing, drift thresholds, and cost gates come from closed tables. The
creativity is in the shot content; the process is auditable end to end (every
attempt, retry, score, and credit is an immutable row in SQLite).

> ### ▶ Live demo — [directoragent.onrender.com/docs](https://directoragent.onrender.com/docs)
>
> Try it now: send a description to `POST /plan` and watch the pipeline
> produce a six-shot plan. The demo runs in **mock mode** — free, no
> credentials, nothing to set up. It lives on Render's free tier, which
> sleeps when idle: the **first request after a quiet period takes ~30–50s**
> while the instance wakes, then everything responds normally.

## Running it

**Hosted demo** — [https://directoragent.onrender.com/docs](https://directoragent.onrender.com/docs):
Swagger UI over the full plan-review lifecycle. `POST /plan` (plan + projected
cost, no execution) → `POST /runs/{run_id}/execute` (202, runs in the
background) → `GET /runs/{run_id}` (poll per-shot status, drift, cost).

**Local — web API:**

```bash
pip install -e ".[web]"
uvicorn directoragent.web:app        # then open http://127.0.0.1:8000/docs
```

**Local — CLI (mock quickstart, zero credentials):**

```bash
pip install -e ".[dev]"
da run assets/test.png "a lone figure on a neon street" --mock
da status <run_id>   # per-shot status, drift scores, cost
da list              # all runs
```

That is the **full pipeline** — vision, planning, parallel fan-out, drift
scoring, quality-retry, assembly — with **no API keys, no cost, in under 10
seconds**. CI runs exactly this. Mock and real mode share every line of phase
code; only the injected clients differ.

## Plan review before spend

`--review` plans, persists the run as `PLANNING`, prints every shot's prompt /
model / cost, and stops. `da resume <run_id>` executes it later:

```
$ da run assets/test.png "a lone figure on a neon street" --mock --review
run_id: demo-review
=== PLAN  run_id=demo-review ===
arc: dramatic

shot_01  beat=establish  render_class=wide_environment  model=veo_3_1  dur=8s  cost=$1.44
  style : anamorphic wide, neon-soaked
  motion: slow push toward the street [PUSH_IN]
  prompt: Wide establishing shot of a rain-slicked neon street at night; ...
...
projected total cost: $8.08
plan persisted. run `da resume demo-review` to execute.

$ da resume demo-review --mock
...
first-try yield: 100% | mean drift: 0.820 | total cost: $8.08 | failures: none
```

## Arcs

Four named six-beat arcs shape the storyboard: **`dramatic`**,
**`observational`**, **`reveal`**, **`mood-piece`**. Pick one with
`--arc <name>`; when unset, the planner chooses one and announces its choice.
Each beat carries a *soft* `render_lean` the planner may override — with the
deviation explained in the shot's `model_reason`.

## Model routing (deterministic)

The LLM proposes a `render_class` per shot; a closed table picks the model and
drift threshold. The LLM **never** chooses the model — that is what keeps every
routing decision auditable and reproducible.

| `render_class` | Model | Drift threshold |
|---|---|---|
| `face` | Seedance 2.0 | 0.78 |
| `complex_motion` | Kling 3.0 | 0.72 |
| `abstract_fluid` | Wan 2.6 | 0.65 |
| `wide_environment` | Veo 3.1 | 0.70 |

(Thresholds are tighter where fidelity matters most — identity for faces —
and looser where variance is acceptable, e.g. abstract fluids.)

## Real mode — one adapter, two transports, two Higgsfield products

The Higgsfield adapter is transport-agnostic: it emits tool calls through an
injectable async `call_tool` seam (~10 lines in
[`clients/higgsfield.py`](src/directoragent/clients/higgsfield.py) — the
`_mcp` method), and two transports satisfy it. They reach **two different
Higgsfield products with different model catalogs and separate credit pools**:

**(a) Agent-mediated MCP** — higgsfield.ai's connector inside a Claude
session; exposes the **Seedance / Kling / Veo / Wan** catalog (plus-plan
credits). This is how the first real generation was proven: one Wan 2.6 / 5s
shot, quoted 13 credits by `get_cost` preflight, billed exactly 13 credits,
with the live response shapes captured and folded back into the adapter. Only
works inside a Claude session.

**(b) REST** — authenticated httpx against `platform.higgsfield.ai`, the
cloud.higgsfield.ai product (`Authorization: Key KEY_ID:KEY_SECRET`; set
`HIGGSFIELD_KEY_ID` + `HIGGSFIELD_KEY_SECRET`); exposes the **DoP / Soul**
catalog (separate API-credit pool). Runs anywhere — this is the deployable
path. The transport normalizes REST's flat response envelope to the MCP shapes
so the adapter never knows which transport it is on. Submit is
`POST /{model_id}` (e.g. `higgsfield-ai/dop/standard`); one render_class
(COMPLEX_MOTION → DoP Standard) is mapped as the demonstration and the rest
are a documented config step. The REST API has **no cost endpoint**, so
REST-mode plan-review shows **static estimates, not live credits** (actual
credits reconcile post-submit; `nsfw`/`failed` refund).

> **Current status: deployed and live** — the hosted demo above serves the
> full plan → execute → poll lifecycle in mock mode. Real generation is
> **proven over MCP** (the P12.4 Wan 2.6 shot; its output scores **0.89**
> CLIP drift against the seed photo, clearing the 0.65 threshold). REST is
> **wired against the confirmed live Cloud API contract** and stub-tested
> against the recorded shapes. One optional item remains: a first paid REST
> generation to close the loop end to end. Details in
> [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md) §11.

## Drift scoring

Every generated shot is scored against the *source photo*: CLIP (ViT-B-32)
cosine similarity on a single mid-point frame of the generated video. Each
shot must clear its render_class threshold (table above); a failing score
triggers a quality-retry — a fresh job, fresh attempt row, fresh cost — up to
**3 attempts per shot**. Scorer failures (dead URL, undecodable video, …)
return 0.0 and land in the same retry path; the scorer never crashes the
fan-out. Real scoring needs the heavy extra:

```bash
pip install -e ".[clip]"   # torch, open-clip-torch, av, pillow
```

Mock mode never imports torch.

## Cost safety

- `--max-cost` sets a ceiling checked **before** the fan-out: if the projected
  plan cost exceeds it, the run aborts before a single job is submitted.
- In agent-mediated real mode the projection uses Higgsfield's `get_cost`
  no-spend preflight — real credits, not guesses. (REST mode degrades to
  static estimates — the Cloud API has no cost endpoint; see above.)
- Attempts are immutable rows: retries, first-try yield, and per-attempt cost
  are all visible after the fact, not averaged away.

## Architecture notes

- **State:** SQLite behind a `StateStore` protocol (Postgres is a config-time
  swap, not a rewrite). Never a JSON file — concurrency safety.
- **Resumable runs:** a crash-safe job ledger (`open_attempt` → submit →
  `record_job_id`); on resume, in-flight jobs re-poll and orphaned submissions
  are reconciled by content fingerprint — the Higgsfield API has no
  idempotency key, so reconcile matches `{model, prompt}` against recent
  generations.
- **Two retry mechanisms, never conflated:** transient (5xx/timeout —
  tenacity inside the client, same attempt row) vs quality (drift failure —
  new attempt, new job, new cost, in the executor).
- **Torch stays out of the import graph** unless real scoring actually runs:
  `import directoragent` never loads it (enforced by an invariant test).

## Environment variables

| Variable | Meaning |
|---|---|
| `VISION_PROVIDER` | Vision/planning LLM: `openai` \| `anthropic` \| `gemini` \| `mock` |
| `VISION_MODEL` | Provider model override; blank = provider default |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | Key for whichever vision provider you use (read by the provider SDKs) |
| `HIGGSFIELD_KEY_ID` | REST transport key id (`Authorization: Key KEY_ID:KEY_SECRET`) |
| `HIGGSFIELD_KEY_SECRET` | REST transport key secret |
| `HIGGSFIELD_BASE_URL` | REST base URL (default `https://platform.higgsfield.ai`) |
| `HIGGSFIELD_API_KEY` | Legacy/dead — auth is the OAuth connector in agent mode, `KEY_ID:KEY_SECRET` in REST mode |
| `MOCK_MODE` | `true` = full offline pipeline (same as `--mock`). The deploy default (`render.yaml`); the web layer also falls back to mock whenever the REST creds are unset |
| `MAX_COST_USD` | Budget ceiling checked before fan-out (credits in real mode; the name is a known misnomer) |
| `STATE_DB_PATH` | SQLite state DB path (default `.directoragent/state.db`) |
| `LOG_LEVEL` | Python log level (default `INFO`) |

Copy `.env.example` to `.env` and fill in what you need.

## Project docs

- [`CLAUDE.md`](CLAUDE.md) — the agent anchor: locked decisions, invariants,
  build order. The single source of truth for contributors (human or agent).
- [`docs/TECHNICAL_DOCUMENTATION.md`](docs/TECHNICAL_DOCUMENTATION.md) —
  architecture, phase-by-phase design, decision log, and the deploy-gated
  verification table (§11).
- [`docs/Techno functional overview.md`](docs/Techno%20functional%20overview.md)
  — the techno-functional overview: what the system does, phase by phase, for
  a mixed technical/product audience.
- [`docs/Build_runbook.md`](docs/Build_runbook.md) — the step-by-step build
  prompts the project was constructed from.
- [`verification.md`](verification.md) — the verification policy: gates,
  review cadence, and the reusable adversarial-review prompt.
