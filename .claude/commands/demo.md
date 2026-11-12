---
description: Run the mock-mode demo and summarise the storyboard result
---

1. Read `CLAUDE.md` first.
2. Run `bash scripts/demo.sh` from the repo root. It is mock-only: no credentials, no cost.
3. Summarise, in this order:
   - **Input:** the reference photo (path, size, dimensions) and the scene description, from
     A.5 [1]. If you have a tool that shows a file to the user, attach the photo.
   - **Vision:** the extracted SceneModel in one or two lines (subject, environment, lighting,
     mood), from A.5 [2]. Say that mock mode returns a canned scene and plan, so they do not
     follow the description.
   - **Planner:** the arc, then a short table per shot: beat, render_class (and whether it
     follows or overrides the arc's lean), routed model, drift threshold, duration, estimated
     cost; then the projected total, from A.5 [3].
   - **Results:** a table for the A.4 run built from the A.6 ledger, one row per shot, with columns:
     shot, render_class, model, drift, attempts, status (success / failed), last-attempt cost,
     total shot cost; then a totals row (attempts, shots succeeded, run total cost).
     Add the C.1 retry shot as a final row in the same columns, marked as C.1.
   - first-try yield
   - what the `--review` gate did: plan and projected cost printed, run left in `planning`
     with no attempts until `resume`, and whether the second resume resubmitted anything
     (it should not: same job ids, same cost; the B.6 ledger should show 1 attempt per shot)
   - the C.1 retry path in one line: attempt 1 status and drift, attempt 2 status and drift,
     and the shot's total cost (expect failed_drift 0.50 -> passed 0.82, cost = 2 submissions)

If any step fails, report the failing step's header and its error instead of a summary.
Do not modify any file.
