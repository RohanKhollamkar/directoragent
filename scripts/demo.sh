#!/usr/bin/env bash
# Mock-mode demo: a full run, then the --review gate -> resume -> idempotent resume.
# Zero credentials, zero cost. Fresh run ids each invocation (timestamped).
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
PHOTO="assets/test.png"
SCENE="a lone traveller crosses a rain-soaked market at dusk"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="demo-${TS}"
GATE_ID="demo-gate-${TS}"

da() { "$PY" -m directoragent.cli "$@"; }

echo "== A.4  full mock run (run_id=${RUN_ID})"
da run "$PHOTO" "$SCENE" --mock --run-id "$RUN_ID"

echo "== B.1  plan only, --review gate (run_id=${GATE_ID})"
da run "$PHOTO" "$SCENE" --mock --review --run-id "$GATE_ID"

echo "== B.2  status after review (expect planning, all pending)"
da status "$GATE_ID"

echo "== B.3  resume: execute the reviewed plan"
da resume "$GATE_ID" --mock

echo "== B.4  status after resume (expect complete, all passed)"
da status "$GATE_ID"

echo "== B.5  resume again (expect no resubmission, same job ids and cost)"
da resume "$GATE_ID" --mock

echo "== C.1  retry path: attempt 1 forced to fail drift, attempt 2 passes"
"$PY" scripts/retry_demo.py
