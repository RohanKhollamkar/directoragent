# DirectorAgent — Verification Policy

Three things need verifying, and each needs a different tool. No single agent
covers all three.

| What | Question | Verified by |
|---|---|---|
| Working state | Does it run? | **Execution** — `da run --mock` + `pytest` in CI |
| Functional authenticity | Does it do the right thing? | **Tests** — the invariant + pipeline tests |
| Integrity | Does it still match the locked design? | **Static gates + AI review** against CLAUDE.md |

**Ordering rule:** deterministic gates (CI, tests, invariant checks) are the
source of truth. AI review sits on top and can only ever *raise concerns*,
never grant a pass. A red gate is never overridden by a green review.

---

## The verification stack (cheapest → most judgment)

1. **Build runs.** Every runbook prompt ends with Claude Code executing
   something (`pip install`, the mock run, `pytest`). If it didn't run, stop.
2. **CI gates** (`.github/workflows/ci.yml`). On every push: ruff, pytest
   (which includes `test_invariants.py`), the mock pipeline end-to-end, and an
   idempotent resume. No credentials, no torch. This is the backbone.
3. **Invariant tests** (`test_invariants.py` + `test_executor.py`). The
   CLAUDE.md rules as executable checks — static ones in test_invariants,
   behavioral ones (add_cost once, no-resubmit, open_attempt-before-network)
   in test_executor.
4. **Adversarial AI review** (prompt below). For risky diffs only. Finds
   design drift that tests don't encode. Advisory, never auto-merges.

---

## Cadence — tiered to avoid review fatigue (which causes rubber-stamping)

- **Every commit, automated:** CI runs. Zero effort. Read the result.
- **Every commit, ~60s human:** open the PR's *Files changed* tab; confirm
  only the intended files moved and no foundation file was touched.
- **Risky commits — run the review prompt in a SEPARATE session:** P2
  (store), P7 (executor), P9 (pipeline). These have judgment that tests
  partially miss.
- **Milestones — full integrity sweep:** after P10 (green mock), P11 (tests),
  P13 (real CLIP). Review prompt in "whole-tree" mode, not just the diff.

Why "separate session": the builder has motivated reasoning about code it just
wrote. A fresh context with an adversarial mandate catches more.

---

## Dependency-split refinement (do this at P0 / P1)

Because torch is lazy-loaded and mock mode never touches CLIP, move the heavy
deps OUT of the default install so CI and casual cloners stay fast:

```toml
[project]
dependencies = [
  "pydantic>=2", "pydantic-settings", "aiosqlite", "httpx",
  "tenacity", "typer", "anthropic", "openai", "google-generativeai",
]

[project.optional-dependencies]
clip = ["torch", "open-clip-torch"]          # only needed for REAL drift scoring
dev  = ["pytest", "pytest-asyncio", "ruff"]
```

`pip install -e .` → light, runs mock mode and tests.
`pip install -e ".[clip]"` → adds real CLIP when you wire up P13.
CI installs `.[dev]` only — no 2GB torch download on every push.

---

## The reusable review prompt

Run this in a SEPARATE Claude Code session pointed at the same repo, AFTER a
risky build prompt. It does not write code.

```
You are reviewing, not building. Do not modify any file. Your mandate is to
FIND PROBLEMS — invariant violations, regressions, design drift — not to
approve. If you are unsure whether something is a problem, flag it.

1. Read CLAUDE.md. Treat its invariants and "what NOT to do" list as the spec.
2. Show the diff of the work under review: `git diff main...HEAD` (or the last
   commit if on main).
3. Run the deterministic gates and paste their RAW output, no summarizing:
   - ruff check .
   - pytest -q
   - da run assets/test.png "review smoke" --mock --run-id review-smoke
4. For EACH of these, state PASS/FAIL with the specific file:line evidence:
   - open_attempt() is called before any network call in executor.py
   - new Attempt rows are created only in the quality-retry loop
   - a PASSED shot is never re-submitted (resume path)
   - add_cost() is called once per submission, never per poll
   - route()/drift_threshold() appear only in planner.py
   - the LLM never sets model or min_drift_score
   - no module-level torch/open_clip import anywhere
   - no datetime.utcnow(); no global settings singleton in phases
   - the five foundation files are unchanged
5. List any concern with file:line and why it matters.
6. End with a VERDICT: "gates green + no concerns" OR "needs changes: <list>".
   If any gate failed, the verdict is automatically "needs changes" regardless
   of the rest.

Do not fix anything. Report only.
```

Feed the verdict back into the next build prompt ("the reviewer flagged X at
line Y — fix it, then re-run gates"). Never let the reviewer both find and fix
in one pass — that collapses the separation that makes it useful.

---

## Definition of done, per prompt

A runbook step is done when ALL of:
- Claude Code's "list of changes" matches the PR's Files-changed tab.
- No foundation file was modified (unless the step explicitly restructures).
- CI is green (ruff + pytest + mock run).
- For risky steps: the review prompt's verdict is "gates green + no concerns".

Only then merge to main and move to the next prompt.
```
