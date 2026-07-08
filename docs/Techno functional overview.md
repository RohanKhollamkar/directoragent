# DirectorAgent — Techno-Functional Overview

> **Audience:** product managers, engineering managers, and stakeholders who need
> to understand what DirectorAgent does, how it works, what it costs, and where
> it's going — without reading the source code. For implementation detail see
> `docs/TECHNICAL_DOCUMENTATION.md`; for the build plan see `BUILD_RUNBOOK.md`.
>
> **Living document.** Updated as the product is built. Status is current as of
> the real-integration work (STEP 12) being underway: the mock pipeline is
> complete and the Higgsfield API has been inspected; the real adapter is being
> wired against the confirmed API (see §8).

---

## 1. What DirectorAgent is (the one-paragraph version)

DirectorAgent turns **a single photograph plus a short description** into a
**six-shot cinematic storyboard** — a sequence of short video clips that tell a
visual story while keeping the same character and setting consistent across every
shot, with controlled camera work and automatic quality checks. It is aimed at
anyone who needs to go from a single reference image to a directed, multi-shot
video sequence quickly and predictably: filmmakers, advertisers, content teams,
or product demos. The generation itself is done by Higgsfield's video models; what
DirectorAgent adds is the **direction** — choosing the right model for each shot,
structuring the shots into a story, checking each result for fidelity, and keeping
costs visible and bounded.

---

## 2. What it does, step by step

DirectorAgent works in five stages. Each stage has a clear input and output, and
they run in a fixed order — there is no unpredictable "AI deciding what to do
next." The creativity is in the content of each shot; the process around it is
deliberate and repeatable.

```mermaid
flowchart TD
    A[Photo + scene description] --> B["1 · Vision<br/>read the photo into structured details"]
    B --> C["2 · Planner<br/>design a 6-shot story arc"]
    C --> D{"Review the plan first?<br/>(--review)"}
    D -- "approve, then resume" --> E["3 + 4 · Executor<br/>generate each shot, check its quality, retry if needed"]
    D -- "run in one go" --> E
    E --> F["5 · Assembler<br/>package the storyboard + report metrics"]
    F --> G["Storyboard: 6 clips + cost, quality scores, model used per shot"]
```

1. **Vision** — reads the photograph and turns it into structured details: who or
   what the subject is, the environment, the lighting, the mood, key objects, the
   colour palette. This is the system's "understanding" of the source image.
2. **Planner** — designs a six-shot story. It picks a narrative structure (an
   "arc"), and for each shot decides what it shows, how the camera moves, and
   writes a concrete instruction for the video model. It also chooses which video
   model is right for each shot — but that choice follows fixed rules (see §4), it
   is not improvised.
3. **Executor** — sends all six shots to Higgsfield to be generated, in parallel,
   then checks each finished clip for fidelity to the original photo. If a clip
   isn't faithful enough, it automatically retries (up to a limit).
4. **Quality check (drift detection)** — folded into the executor: every generated
   clip is compared against the source photo and scored. A clip that scores too
   low is regenerated.
5. **Assembler** — collects the approved clips into a storyboard, writes it to a
   file, and reports the numbers that matter: cost, how many shots passed on the
   first try, and the average fidelity score.

---

## 3. Key features (and why each matters)

**Directed, not random — deterministic model routing.** Higgsfield offers several
video models, each strong at different things. DirectorAgent always sends face
shots to the face-specialist model, wide establishing shots to the
environment-specialist model, and so on, by a fixed rule. This makes results
**predictable and auditable** — every model choice can be explained — rather than
a black box.

**Cinematic structure — story arcs.** Shots aren't generated in isolation; they're
arranged into a six-beat narrative (establish → introduce → build → turn → peak →
resolve, and other shapes). The user can pick the arc, or the system picks the
best-fitting one and explains its choice. This is what makes the output *read* as
a directed sequence rather than six unrelated clips.

**Consistent characters — groundedness.** The same person and setting carry across
all six shots, anchored to the original photo, while framing, angle, and action
vary freely from shot to shot. This is the core promise: one reference image, a
coherent sequence.

**Built-in quality control — drift detection with auto-retry.** Every clip is
scored for fidelity to the source. Below-threshold clips are automatically
regenerated, with stricter standards for faces (identity must hold) than for
abstract or atmospheric shots (more variation is acceptable).

**Cost is visible and bounded.** The system estimates the full cost *before*
generating anything and can stop if it would exceed a set ceiling. An optional
review step lets the user see the exact plan and projected cost and approve it
before any money is spent (see §5 and §6).

**Safe to interrupt — resumable.** Video generation takes minutes and costs real
money. If the process is interrupted, DirectorAgent can resume without
re-generating (and re-paying for) work already done.

**Try it for free — mock mode.** The entire pipeline can run end-to-end with no
credentials and no cost, producing placeholder results. This lets anyone evaluate
the workflow, and lets the team test continuously, before spending a cent.

**Bring your own vision model.** The image-understanding step (Vision) works with
OpenAI, Anthropic, or Google models interchangeably, and adding another is a small
change — users aren't locked into one provider.

---

## 4. How model routing works

Each shot is tagged with a **render class** — a category that determines which
Higgsfield model generates it and how strict the fidelity check is. There are four
render classes today, one per available model:

```mermaid
flowchart LR
    F["FACE<br/>(identity / expression)"] --> SV["Seedance 2.0"]
    CM["COMPLEX_MOTION<br/>(movement)"] --> K["Kling 3.0"]
    AF["ABSTRACT_FLUID<br/>(texture / transition)"] --> W["Wan 2.6"]
    WE["WIDE_ENVIRONMENT<br/>(scale / establishing)"] --> V["Veo 3.1"]
```

The AI suggests a render class for each shot, but the **model is always assigned by
the fixed rule above, never chosen freely by the AI**. This separation is
deliberate: it keeps routing predictable and explainable while still letting the AI
be creative about the *description* of each shot. (Adding a fifth model later is a
small, contained change — see §8.)

---

## 5. Cost model — how cost is calculated

**The cost model in real mode uses Higgsfield credits via a no-spend preflight.** Before generating anything, the system calls `get_cost: true` on `generate_video` to get the exact credit cost for each planned shot — no estimate, no placeholder. First live calibration data: Veo 3.1 (the environment/establishing model) at 8 seconds costs **22 credits**. The illustrative USD prices below are mock-mode placeholders only; real-mode costs come from the preflight call.

**The basic formula (mock mode / illustrative).** Each model has a price per second of generated video. A shot's cost is:

> shot cost = (model's price per second) × (shot duration in seconds)

A run's projected cost is the sum across all six shots. Illustrative per-second
prices (these are **placeholders to be calibrated against real Higgsfield
pricing** — see §8):

| Render class | Model | Price/sec (illustrative) |
|---|---|---|
| FACE | Seedance 2.0 | $0.10 (mock placeholder) |
| COMPLEX_MOTION | Kling 3.0 | $0.14 (mock placeholder) |
| ABSTRACT_FLUID | Wan 2.6 | $0.08 (mock placeholder) |
| WIDE_ENVIRONMENT | Veo 3.1 | $0.18 (mock placeholder; real = 22 credits / 8s) |

**Worked example.** A typical six-shot plan:

| Shot | Model | Duration | Cost |
|---|---|---|---|
| 1 (wide establish) | Veo 3.1 | 20s | $3.60 |
| 2 (face) | Soul v2 | 15s | $1.50 |
| 3 (motion) | Kling 3.0 | 25s | $3.50 |
| 4 (face) | Soul v2 | 15s | $1.50 |
| 5 (motion) | Kling 3.0 | 25s | $3.50 |
| 6 (wide resolve) | Veo 3.1 | 20s | $3.60 |
| **Projected total** | | **120s** | **$17.20** |

**The cost ceiling.** There is a configurable maximum (default $10). In the example
above, the projected $17.20 exceeds the default ceiling, so the run would **stop
before generating anything** — the operator either raises the ceiling deliberately
or revises the plan. This prevents surprise bills, which is the failure mode that
most often bites people running generative tools.

**Retries cost money.** If a shot fails the quality check and is regenerated, that
retry is a new paid job. So a shot that takes two attempts costs roughly twice as
much. This is why the **first-try yield** metric matters (see §6) — it's a direct
measure of cost efficiency.

**Where cost is shown.** With the review step enabled, the full plan and projected
cost are displayed *before* any spend, for explicit approval. After a run, the
storyboard reports the actual cost per shot and the run total.

---

## 6. Quality control — drift detection

"**Drift**" is how far a generated clip has strayed from the source photo. After a
clip is generated, it's scored for similarity to the original; the score is
compared against a threshold for that render class.

```mermaid
flowchart TD
    S["Generate shot via Higgsfield"] --> P["Wait for it to finish"]
    P --> R["Generated clip"]
    R --> D["Score fidelity vs source photo"]
    D --> Q{"Score meets the<br/>render class threshold?"}
    Q -- "yes" --> PASS["Shot PASSED ✓"]
    Q -- "no" --> C{"Attempts used < 3?"}
    C -- "yes, retry" --> S
    C -- "no" --> FAIL["Shot flagged as failed"]
```

**Thresholds vary by render class** — stricter where fidelity matters most:

| Render class | Threshold | Why |
|---|---|---|
| FACE | 0.78 (strict) | Identity must hold — a face that drifts is wrong |
| COMPLEX_MOTION | 0.72 | Movement allows more variation |
| WIDE_ENVIRONMENT | 0.70 | Establishing scale tolerates change |
| ABSTRACT_FLUID | 0.65 (loose) | Atmospheric shots are meant to vary |

**First-try yield** is the share of shots that passed on their first attempt. A
yield of 83% means five of six shots were right the first time and one needed a
retry. It's both a **quality signal** (are prompts and routing working?) and a
**cost signal** (retries cost money), so it's the single most useful number for
judging a run's health.

---

## 7. The plan-review flow (cost gate before spend)

DirectorAgent separates *planning* (cheap, instant) from *generating* (slow,
costs money). This lets the user inspect and approve the plan before spending.

```mermaid
stateDiagram-v2
    [*] --> PLANNING: run with --review
    PLANNING --> EXECUTING: user approves (resume)
    [*] --> EXECUTING: run in one go (no review)
    EXECUTING --> COMPLETE: all shots done
    EXECUTING --> ABORTED: cost ceiling exceeded
```

With `--review`, the system plans the six shots, saves the plan, shows it (the
chosen arc, each shot's description, model, and projected cost), and **stops before
spending**. The user reviews, then approves by resuming. Because the plan is saved,
the review can happen any time — the user can walk away and come back, and the same
plan is waiting. This design is also exactly what a future web interface would use:
plan, show, approve, generate.

---

## 8. Roadmap — what's built and what's coming

**Built and working:** the full pipeline end-to-end — mock mode (free, no
credentials), the real Higgsfield adapter with **two transports** (agent-mediated,
proven live with a real 13-credit generation; REST for deployment, built and
stub-tested), real cost preview via live `get_cost` in agent mode, and **real
fidelity scoring** (CLIP against the source photo, mid-point video frame). The
README, quickstart, and final verification gates are complete.

**Remaining before "done":** a small run-status bookkeeping fix and the final
whole-tree integrity review. **Remaining at deployment:** four live-verification
items that cannot run in the development sandbox (the REST submit endpoint and
REST cost preview must be confirmed against the live service; real CLIP scoring
against an actual generated video; and the direct-upload path) — all built,
documented, and safely inert until then.

---

## 9. Deferred features and the reasoning

Nothing here is missing by accident — each was a deliberate prioritization call.

| Deferred item | Why deferred | When it's picked up |
|---|---|---|
| Real Higgsfield generation | Mock-first let the whole product be built and tested for free; the real connection is now being wired against the inspected live API | Underway (STEP 12) |
| Real fidelity (CLIP) scoring | Same mock-first logic; the scoring engine is heavy and only needed for real output | Near-term (STEP 13) |
| User-defined custom story arcs | The four built-in arcs cover the common cases; accepting fully custom arcs is an easy add once there's demand | On demand |
| Shot-to-shot chaining (each shot building on the previous one's output) | True chaining would make shots wait on each other and slow the parallel generation; v1 keeps all six independent and grounded to the source photo | When the quality benefit is proven to outweigh the speed cost |
| More than four models / render classes | The number of categories tracks the number of available models; four models exist today | When Higgsfield's model lineup grows |
| Heavier database (Postgres) for scale | A simple built-in database covers development and demos with zero setup; the system is built to swap to Postgres with a config change | At production scale |
| Automatic correction of mismatched shot categories | Trusting the AI's category choice (with a human review step as backstop) is simpler and avoids a fragile auto-corrector that could "fix" things that were right | Only if real usage shows frequent mismatches |
| Cost calibration | Placeholder per-second prices are used in mock mode; the live service returns exact real costs via a no-spend preflight, so real-mode projections are accurate credits, not estimates | Real costs available now via preflight (STEP 12); mock table stays illustrative |
| Web interface | The backend is intentionally built to support one (plan/approve/generate maps cleanly to a UI), but the CLI comes first | Future |

---

## 10. Key trade-offs and decisions (plain-language)

| Decision | The trade-off |
|---|---|
| **No heavyweight AI framework** (built on plain, well-understood components) | Faster to build, fully transparent, and easy to explain — at the cost of hand-building a few things a framework would provide. Chosen because the workflow is a fixed sequence, not a system that needs to branch unpredictably. |
| **Mock-first development** | The whole product runs free for evaluation and testing before any paid integration — at the cost of the real model connections coming later in the schedule. |
| **Deterministic model routing** | Predictable, explainable model choices — by design, the AI does not get to "freely pick" a model. This is a feature (auditability), not a limitation. |
| **Simple database by default** | Zero setup, clone-and-run — swappable to an industrial database for scale. |
| **Graded character consistency** | Same character across shots without making every shot a stiff copy of the photo; the balance between "locked" and "free" is tuned per shot type. May need fine-tuning on real images. |
| **Review before spend** | Strong protection against surprise costs — at the cost of one extra approval step (which a UI would make a single click). |

---

## 11. Risks and open questions

- **Real Higgsfield behaviour is not yet known.** The exact controls and pricing of
  the live models are confirmed only at integration; the system is structured so
  this is a contained, low-risk step.
- **Cost figures are illustrative** until calibrated against real pricing.
- **The planner's output quality** (how good and "filmable" the shot descriptions
  are) is expected to need a round of tuning on real photographs — normal for any
  AI-driven creative step.
- **Fidelity thresholds** may need adjustment once measured against the real
  scoring engine rather than placeholders.

---

## 12. Glossary

- **Storyboard** — the final output: six short video clips in sequence, plus
  metrics (cost, quality scores, model per shot).
- **Shot** — one clip in the storyboard.
- **Arc** — the narrative structure of the six shots (e.g. establish → build →
  peak → resolve). The user can choose one or let the system pick.
- **Beat** — one step in an arc; the dramatic role a shot plays.
- **Render class** — the category that decides which model generates a shot and how
  strict its quality check is (FACE, COMPLEX_MOTION, ABSTRACT_FLUID,
  WIDE_ENVIRONMENT).
- **Shot style** — the free-form creative description of a shot (e.g. "slow
  push-in close-up"); separate from the render class, which handles routing.
- **Drift / drift score** — how far a generated clip has strayed from the source
  photo; higher is more faithful.
- **Drift threshold** — the minimum fidelity a shot must meet to pass; stricter for
  faces than for abstract shots.
- **Attempt** — one generation try for a shot. A shot that fails the quality check
  gets another attempt, up to a limit (3).
- **First-try yield** — the share of shots that passed on their first attempt; a
  combined quality-and-cost-efficiency metric.
- **Cost ceiling** — a configurable maximum spend; a run that would exceed it stops
  before generating.
- **Plan review** — an optional step that shows the full plan and projected cost
  for approval before any spend.
- **Mock mode** — running the whole pipeline with placeholder results, free and
  without credentials, for evaluation and testing.
- **Higgsfield / MCP** — the external service (and its integration protocol) that
  actually generates the video clips.
- **Resumable** — the ability to continue an interrupted run without re-generating
  (and re-paying for) completed work.
