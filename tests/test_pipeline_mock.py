"""Full pipeline in mock mode (STEP 11) — storyboard + plan/review flow."""

import json
from pathlib import Path

from directoragent import pipeline
from directoragent.config import Settings
from directoragent.schema import RunStatus, Storyboard
from directoragent.state.sqlite_store import SqliteStateStore


def _settings(**over) -> Settings:
    base = dict(mock_mode=True, state_db_path=".directoragent/state.db", max_cost_usd=100.0)
    base.update(over)
    return Settings(**base)


async def _db_status(settings: Settings, run_id: str) -> RunStatus:
    """The status `da status`/`da list` see — read back via load_run."""
    store = SqliteStateStore(settings.state_db_path)
    try:
        state = await store.load_run(run_id)
        assert state is not None
        return state.status
    finally:
        await store.close()


async def test_full_mock_pipeline_yields_storyboard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolate .directoragent/ writes
    sb = await pipeline.run("assets/p.png", "a noir chase", "run1", _settings())

    assert isinstance(sb, Storyboard)
    assert len(sb.results) == 6
    assert 0.0 <= sb.first_try_yield <= 1.0
    sb_path = Path(".directoragent/runs/run1/storyboard.json")
    assert sb_path.exists()
    data = json.loads(sb_path.read_text())
    assert data["run_id"] == "run1" and len(data["results"]) == 6

    # Lifecycle: a finished run is persisted as COMPLETE (STEP 14.1).
    assert await _db_status(_settings(), "run1") == RunStatus.COMPLETE


async def test_plan_only_then_resume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = _settings()

    # Plan-only: persists a PLANNING run, generates nothing, returns None.
    out = await pipeline.run("assets/p.png", "noir", "run2", settings, plan_only=True)
    assert out is None

    store = SqliteStateStore(settings.state_db_path)
    state = await store.load_run("run2")
    await store.close()
    assert state is not None
    assert state.status == RunStatus.PLANNING
    assert state.attempts == {}  # no generation happened

    # Resume completes it into a full storyboard, PLANNING -> COMPLETE in DB.
    sb = await pipeline.run("", "", "run2", settings)
    assert isinstance(sb, Storyboard)
    assert len(sb.results) == 6
    assert Path(".directoragent/runs/run2/storyboard.json").exists()
    assert await _db_status(settings, "run2") == RunStatus.COMPLETE
