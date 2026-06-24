"""Executable invariant gates.

This file turns the prose rules in CLAUDE.md into deterministic checks that
run on every push. These are the rules an AI reviewer might *say* hold but
can't be trusted to verify — so we make them mechanical. If one of these
fails, the design has drifted, full stop.

Some invariants (add_cost-once-per-submission, no-resubmit-on-PASSED,
open_attempt-before-network) are behavioral and live in test_executor.py
instead, because they require running the executor with spies. This file
covers the STATIC ones — the ones provable by reading the source tree.

Adjust SRC if your package path differs.
"""

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "directoragent"


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in str(p)]


# --- Invariant: torch / open_clip never imported at module level ------------
# (CLAUDE.md #6 / lazy-load rule) Importing directoragent must never pull torch.
def test_no_module_level_heavy_imports():
    offenders = []
    for f in _py_files(SRC):
        tree = ast.parse(f.read_text(), filename=str(f))
        for node in tree.body:  # module-level only, not inside funcs/classes
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                if n in {"torch", "open_clip"}:
                    offenders.append(f"{f.name}: module-level import of {n}")
    assert not offenders, offenders


def test_importing_package_does_not_load_torch():
    # Fresh interpreter so other tests can't have loaded torch first.
    code = "import directoragent, sys; assert 'torch' not in sys.modules"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- Invariant: route()/drift_threshold() called ONLY in the planner --------
# (CLAUDE.md #5) The executor must never route. routing.py defines them.
def test_routing_calls_only_in_planner():
    allowed = {"routing.py", "planner.py"}
    offenders = []
    for f in _py_files(SRC):
        if f.name in allowed:
            continue
        text = f.read_text()
        for call in ("route(", "drift_threshold("):
            if call in text:
                offenders.append(f"{f.name} calls {call}")
    assert not offenders, offenders


# --- Invariant: no deprecated datetime.utcnow() -----------------------------
def test_no_naive_utcnow():
    offenders = [f.name for f in _py_files(SRC) if "utcnow(" in f.read_text()]
    assert not offenders, offenders


# --- Invariant: no module-level settings singleton ----------------------------
# (Settings is constructed at the CLI boundary and injected.)
def test_no_global_settings_singleton():
    offenders = []
    for f in _py_files(SRC):
        text = f.read_text()
        if f.name != "cli.py" and "from directoragent.config import settings" in text:
            offenders.append(f"{f.name} imports a settings singleton")
    assert not offenders, offenders


# --- Invariant: foundation files unchanged since they were frozen -----------
# Generate tests/foundation_hashes.json ONCE after the P0 restructure:
#   python -c "import tests.test_invariants as t; t.write_foundation_hashes()"
# After that, any change to a foundation file fails this test until the hashes
# are intentionally regenerated in the same commit (which the reviewer will see).
FOUNDATION = ["schema.py", "protocols.py", "routing.py", "schema.sql", "vision_providers.py"]
HASHES_FILE = Path(__file__).resolve().parent / "foundation_hashes.json"


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_foundation_hashes() -> None:
    HASHES_FILE.write_text(json.dumps({n: _hash(SRC / n) for n in FOUNDATION}, indent=2))


@pytest.mark.skipif(not HASHES_FILE.exists(), reason="run write_foundation_hashes() after P0")
def test_foundation_files_unchanged():
    expected = json.loads(HASHES_FILE.read_text())
    drifted = [n for n in FOUNDATION if _hash(SRC / n) != expected.get(n)]
    assert not drifted, f"foundation files changed: {drifted}"
