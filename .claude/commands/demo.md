---
description: Run the mock-mode demo and summarise the storyboard result
---

1. Read `CLAUDE.md` first.
2. Run `bash scripts/demo.sh` from the repo root. It is mock-only: no credentials, no cost.
3. Summarise in under 12 lines:
   - the shot grid (shot, render_class, model, drift, attempts) from the A.4 run
   - total cost and first-try yield
   - what the `--review` gate did: plan and projected cost printed, run left in `planning`
     with no attempts until `resume`, and whether the second resume resubmitted anything
     (it should not: same job ids, same cost)
   - the C.1 retry path in one line: attempt 1 status and drift, attempt 2 status and drift,
     and the shot's total cost (expect failed_drift 0.50 -> passed 0.82, cost = 2 submissions)

If any step fails, report the failing step's header and its error instead of a summary.
Do not modify any file.
