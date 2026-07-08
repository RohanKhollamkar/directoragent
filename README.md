# DirectorAgent

One photo + one scene description → a six-shot cinematic storyboard, generated
via Higgsfield. DirectorAgent runs a deterministic five-phase DAG — **Vision →
Planner → Executor + Drift → Assembler** — where an LLM proposes *what* each
shot is (style, prompt, narrative beat) but never *how* it is rendered: model
routing, drift thresholds, and cost gates come from closed tables. The
creativity is in the shot content; the process is auditable end to end (every
attempt, retry, score, and credit is an immutable row in SQLite).

## Quickstart (mock mode — zero credentials)

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

## Real mode — two transports, one adapter

The Higgsfield adapter is transport-agnostic: it emits tool calls through an
injectable async `call_tool` seam, and two transports satisfy it.

**(a) Agent-mediated** — inside a Claude session, the Higgsfield MCP connector
*is* the transport. This is how the first real generation was proven: one
Wan 2.6 / 5s shot, quoted 13 credits by `get_cost` preflight, billed exactly
13 credits, with the live response shapes captured and folded back into the
adapter. Only works inside a Claude session.

**(b) REST** — authenticated httpx against `platform.higgsfield.ai`
(`Authorization: Key KEY_ID:KEY_SECRET`; set `HIGGSFIELD_KEY_ID` +
`HIGGSFIELD_KEY_SECRET`). Runs anywhere — this is the deployable path. The
transport normalizes REST's flat response envelope to the MCP shapes so the
adapter never knows which transport it is on.

> **Current status:** REST is built and stub-tested, with **two deploy-gated
> TODOs** — the per-model submit endpoint is a placeholder (`/v2/generate`;
> the real endpoints are CMS-driven and absent from the SDK), and `get_cost`
> has **no REST equivalent**, so REST-mode plan-review shows **static
> estimates, not live credits**, until resolved. See the deploy-gated table in
> [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md) §11 for
> the full list.

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
  static estimates until its `get_cost` gap is resolved — see above.)
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
| `MOCK_MODE` | `true` = full offline pipeline (same as `--mock`) |
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
