"""STEP 12.5 — REST transport tests.

No live calls (the egress proxy blocks platform.higgsfield.ai — verified). We
stub httpx.AsyncClient.request and assert: (a) the auth header, (b) each route's
method/path + param split, (c) REST->MCP envelope normalization against the
P12.4 captured shapes, driven THROUGH the real adapter, and (d) 4xx/5xx surface
as httpx.HTTPStatusError so the adapter's tenacity classifier still works.
"""

import httpx
import pytest

from directoragent.clients.higgsfield import (
    HiggsfieldClient,
    HiggsfieldError,
    _is_transient,
)
from directoragent.clients.higgsfield_rest_transport import (
    _ROUTES,
    HiggsfieldRestTransport,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://platform.higgsfield.ai/x")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError("err", request=req, response=resp)


def _install(monkeypatch, handler):
    """Patch AsyncClient.request; record calls, return handler(method, url, kwargs)."""
    calls: list[dict] = []

    async def fake_request(self, method, url, **kwargs):
        calls.append(
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "headers": dict(self.headers),
            }
        )
        return handler(method, url, kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    return calls


def _transport():
    return HiggsfieldRestTransport(key_id="test-key", key_secret="test-secret")


# --- (a) auth header --------------------------------------------------------
def test_auth_header_is_key_id_colon_secret():
    t = _transport()
    assert t._client.headers["Authorization"] == "Key test-key:test-secret"
    assert t._client.headers["Content-Type"] == "application/json"


def test_missing_credentials_raise():
    with pytest.raises(HiggsfieldError):
        HiggsfieldRestTransport(key_id="", key_secret="secret")
    with pytest.raises(HiggsfieldError):
        HiggsfieldRestTransport(key_id="id", key_secret="")


# --- (b) routes: method / path / param split --------------------------------
def test_routes_table_matches_sdk():
    assert _ROUTES["job_display"] == ("GET", "/requests/{id}/status")
    assert _ROUTES["media_upload"] == ("POST", "/files/generate-upload-url")
    assert _ROUTES["generate_video"][0] == "POST"


async def test_job_display_route(monkeypatch):
    calls = _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "in_progress", "request_id": "JOB1"}),
    )
    await _transport()("job_display", id="JOB1")
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "/requests/JOB1/status"
    # dict(httpx.Headers) lowercases keys; the value carries the auth scheme.
    assert calls[0]["headers"]["authorization"] == "Key test-key:test-secret"


async def test_generate_video_route_posts_body_without_get_cost(monkeypatch):
    calls = _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "queued", "request_id": "JOB2"}),
    )
    params = {"model": "wan2_6", "prompt": "ink", "duration": 5, "aspect_ratio": "16:9"}
    await _transport()("generate_video", params=params)
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "/v2/generate"
    assert calls[0]["json"] == params  # input posted directly (v2 convention)


async def test_media_upload_route_sends_content_type(monkeypatch):
    calls = _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(
            200, {"upload_url": "https://put/here", "public_url": "https://cdn/x.png"}
        ),
    )
    await _transport()("media_upload", filename="test.png")
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "/files/generate-upload-url"
    assert calls[0]["json"] == {"content_type": "image/png"}


# --- (c) envelope normalization vs the P12.4 MCP shapes ---------------------
# REST fixtures (flat V2Response) -> adapter-facing MCP shapes from CLAUDE.md.

async def test_media_upload_normalized_to_uploads_shape(monkeypatch):
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(
            200, {"upload_url": "https://put/here", "public_url": "https://cdn/x.png"}
        ),
    )
    out = await _transport()("media_upload", filename="test.png")
    # MCP shape the adapter's _upload_local parses: uploads[0] with these keys.
    entry = out["uploads"][0]
    assert entry["upload_url"] == "https://put/here"
    assert entry["media_id"] == "https://cdn/x.png"  # public_url stands in as id
    assert entry["url"] == "https://cdn/x.png"
    assert entry["content_type"] == "image/png"


async def test_media_import_url_is_passthrough_no_http(monkeypatch):
    calls = _install(monkeypatch, lambda m, u, k: FakeResponse(500))  # must NOT be hit
    out = await _transport()("media_import_url", url="https://cdn/ref.png", type="image")
    assert out["media_id"] == "https://cdn/ref.png"
    assert calls == []  # REST import is a no-op; no network


async def test_poll_and_fetch_through_the_real_adapter(monkeypatch):
    # Drive the ACTUAL adapter over the transport: a completed REST job with a
    # video url must surface as adapter poll()->"succeeded" and fetch_result()->url.
    video_url = "https://cdn/result.mp4"
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(
            200,
            {
                "status": "completed",
                "request_id": "JOB3",
                "video": {"url": video_url},
            },
        ),
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.poll("JOB3") == "succeeded"
    assert await hf.fetch_result("JOB3") == video_url


async def test_running_status_passthrough(monkeypatch):
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "queued", "request_id": "J"}),
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.poll("J") == "running"


async def test_nsfw_status_normalized_to_failed(monkeypatch):
    # REST-only terminal status the adapter's _FAILED set never saw (P12.4). The
    # transport must map it so it does NOT hit the defensive unknown->running.
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "nsfw", "request_id": "J"}),
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.poll("J") == "failed"


async def test_submit_through_the_real_adapter(monkeypatch):
    # image-reference source is a URL -> _resolve_media uses media_import_url
    # (passthrough), then generate_video returns the job id.
    from directoragent.schema import (
        Model,
        Reference,
        ReferenceType,
        RenderClass,
        Shot,
    )

    def handler(method, url, kwargs):
        assert url == "/v2/generate"
        return FakeResponse(200, {"status": "queued", "request_id": "SUBJOB"})

    _install(monkeypatch, handler)
    shot = Shot(
        shot_id="s", shot_name="n", shot_style="x", render_class=RenderClass.ABSTRACT_FLUID,
        narrative_beat="b", model=Model.WAN_2_6, model_reason="r", camera_motion="static",
        motion_preset="STATIC", prompt="ink",
        reference=Reference(type=ReferenceType.SOURCE_PHOTO, source="https://cdn/ref.png"),
        duration_s=5, min_drift_score=0.15,
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.submit(shot, "idem-1") == "SUBJOB"


# --- (d) 4xx/5xx surface as HTTPStatusError for the tenacity classifier ------
async def test_500_surfaces_as_httpstatuserror_and_is_transient(monkeypatch):
    _install(monkeypatch, lambda m, u, k: FakeResponse(500))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await _transport()("job_display", id="J")
    assert _is_transient(ei.value) is True  # tenacity WILL retry


async def test_400_surfaces_as_httpstatuserror_and_is_not_transient(monkeypatch):
    _install(monkeypatch, lambda m, u, k: FakeResponse(400))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await _transport()("job_display", id="J")
    assert _is_transient(ei.value) is False  # tenacity will NOT retry (4xx)


# --- unsupported / TODO(P12.5-live) routes ----------------------------------
async def test_get_cost_has_no_rest_route():
    with pytest.raises(HiggsfieldError, match="get_cost"):
        await _transport()(
            "generate_video", params={"model": "wan2_6", "get_cost": True}
        )


@pytest.mark.parametrize("tool", ["show_generations", "models_explore", "bogus_tool"])
async def test_unsupported_tools_raise(tool):
    with pytest.raises(HiggsfieldError, match="no REST route"):
        await _transport()(tool)
