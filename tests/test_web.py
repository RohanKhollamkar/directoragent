"""STEP D3a — FastAPI layer tests (mock mode, TestClient, no network)."""

import subprocess
import sys
import time

from fastapi.testclient import TestClient

from directoragent.web import app


def _client(monkeypatch, tmp_path) -> TestClient:
    # cwd isolation puts .directoragent/ (state DB) under tmp_path, and no
    # .env / REST creds there means _settings() flips to mock mode.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HIGGSFIELD_KEY_ID", raising=False)
    monkeypatch.delenv("HIGGSFIELD_KEY_SECRET", raising=False)
    return TestClient(app)


def test_healthz(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_plan_with_just_a_description(monkeypatch, tmp_path):
    # The happy path needs no photo: the bundled assets/test.png is the
    # fallback, so a reviewer can hit /plan with only a description.
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json={"description": "a noir chase"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "planning"
        assert len(body["shots"]) == 6
        assert body["projected_total"] > 0
        for shot in body["shots"]:
            assert shot["render_class"] and shot["model"]
            assert shot["projected_cost"] >= 0

        # Nothing executed, nothing spent: every shot has zero attempts.
        run_id = body["run_id"]
        status = client.get(f"/runs/{run_id}").json()
        assert status["status"] == "planning"
        assert all(s["attempts"] == 0 for s in status["shots"])
        assert status["total_cost"] == 0


def test_plan_swagger_default_junk_treated_as_absent(monkeypatch, tmp_path):
    # Swagger's unedited example fills optional fields with the literal
    # "string". That must behave exactly like omitting them — valid plan,
    # never a 500 (D3c).
    body = {
        "description": "a noir chase",
        "photo_url": "string",
        "photo": "string",
        "arc": "string",
        "provider": "string",
    }
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json=body)
        assert r.status_code == 200
        assert len(r.json()["shots"]) == 6


def test_plan_empty_string_optionals_treated_as_absent(monkeypatch, tmp_path):
    body = {"description": "noir", "photo_url": "", "arc": "", "provider": ""}
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json=body)
        assert r.status_code == 200
        assert len(r.json()["shots"]) == 6


def test_plan_valid_photo_url_still_passes_through(monkeypatch, tmp_path):
    # Junk-cleaning must not eat real values.
    body = {"description": "noir", "photo_url": "https://example.com/photo.png"}
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json=body)
        assert r.status_code == 200
        assert len(r.json()["shots"]) == 6


def test_plan_rejects_unknown_provider_with_400(monkeypatch, tmp_path):
    # A real (non-placeholder) junk value is a clear 400, not a crash.
    body = {"description": "noir", "provider": "not-a-provider"}
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json=body)
        assert r.status_code == 400
        assert "unknown provider" in r.json()["detail"]


def test_plan_malformed_body_is_422_with_detail(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json={"photo_url": "https://x/y.png"})  # no description
        assert r.status_code == 422
        assert r.json()["detail"]
        r = client.post("/plan", json={"description": 123})  # wrong type
        assert r.status_code == 422
        assert r.json()["detail"]


def test_plan_rejects_unknown_arc(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        r = client.post("/plan", json={"description": "x", "arc": "bogus"})
        assert r.status_code == 400
        assert "unknown arc" in r.json()["detail"]


def test_execute_returns_202_then_completes(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        run_id = client.post("/plan", json={"description": "noir"}).json()["run_id"]

        t0 = time.monotonic()
        r = client.post(f"/runs/{run_id}/execute")
        elapsed = time.monotonic() - t0
        assert r.status_code == 202
        assert r.json() == {"run_id": run_id, "status": "executing"}
        # 202 means "accepted", not "done": the response must come back fast,
        # not after the whole mock run.
        assert elapsed < 2.0

        # Poll like a real client until the background thread finishes.
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/runs/{run_id}").json()
            if status["status"] == "complete":
                break
            time.sleep(0.2)
        assert status is not None and status["status"] == "complete"
        assert len(status["shots"]) == 6
        for shot in status["shots"]:
            assert shot["latest_status"] == "passed"
            assert shot["result_url"]
            assert shot["attempts"] >= 1
        assert status["total_cost"] > 0
        assert 0.0 <= status["first_try_yield"] <= 1.0


def test_runs_list_and_404(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        run_id = client.post("/plan", json={"description": "noir"}).json()["run_id"]

        listed = client.get("/runs").json()
        assert [e for e in listed if e["run_id"] == run_id and e["status"] == "planning"]

        assert client.get("/runs/does-not-exist").status_code == 404
        assert client.post("/runs/does-not-exist/execute").status_code == 404


def test_importing_web_does_not_load_torch():
    # Same invariant as the package: the HTTP layer must not drag in torch.
    code = "import directoragent.web, sys; assert 'torch' not in sys.modules"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
