"""STEP 12.5/D2 — REST transport tests.

No live calls (NO paid submit; the egress proxy also blocks
platform.higgsfield.ai). We stub httpx.AsyncClient.request and assert against
the D2-confirmed Cloud API contract: (a) the auth header, (b) each route's
method/path — submit is POST /{model_id} with the real DoP Standard body,
(c) FLAT-REST -> MCP envelope normalization driven THROUGH the real adapter,
(d) the 5-status vocabulary, (e) unmapped render_classes raise a clear error,
(f) 4xx/5xx surface as httpx.HTTPStatusError so the adapter's tenacity
classifier still works, and (g) the static-cost preflight fallback.
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
    REST_MODEL_CATALOG,
    HiggsfieldRestTransport,
)
from directoragent.routing import estimate_cost
from directoragent.schema import (
    Model,
    Reference,
    ReferenceType,
    RenderClass,
    Shot,
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


def _shot(render_class=RenderClass.COMPLEX_MOTION, model=Model.KLING_3):
    return Shot(
        shot_id="s", shot_name="n", shot_style="x", render_class=render_class,
        narrative_beat="b", model=model, model_reason="r", camera_motion="static",
        motion_preset="STATIC", prompt="ink",
        reference=Reference(type=ReferenceType.SOURCE_PHOTO, source="https://cdn/ref.png"),
        duration_s=5, min_drift_score=0.15,
    )


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


# --- (b) routes: the D2-confirmed Cloud API contract ------------------------
def test_routes_table_matches_confirmed_contract():
    assert _ROUTES["generate_video"] == ("POST", "/{model_id}")
    assert _ROUTES["job_display"] == ("GET", "/requests/{id}/status")
    assert _ROUTES["cancel"] == ("POST", "/requests/{id}/cancel")
    assert _ROUTES["media_upload"] == ("POST", "/files/generate-upload-url")


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


async def test_cancel_route(monkeypatch):
    calls = _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "canceled", "request_id": "JOB1"}),
    )
    await _transport()("cancel", id="JOB1")
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "/requests/JOB1/cancel"


async def test_submit_posts_model_id_path_with_dop_body(monkeypatch):
    # Submit path IS the model id; body is the confirmed DoP Standard shape —
    # no model key, no duration, no aspect_ratio.
    calls = _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "queued", "request_id": "JOB2"}),
    )
    params = {
        "model": "kling3_0",   # COMPLEX_MOTION's MCP id -> DoP Standard over REST
        "prompt": "ink dispersing",
        "duration": 5,
        "aspect_ratio": "16:9",
        "medias": [{"value": "https://cdn/ref.png", "role": "start_image"}],
    }
    await _transport()("generate_video", params=params)
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "/higgsfield-ai/dop/standard"
    assert calls[0]["json"] == {
        "seed": None,
        "prompt": "ink dispersing",
        "motions": None,   # TODO(D3-motions): real DoP motion param, null for now
        "image_url": "https://cdn/ref.png",
        "enhance_prompt": True,
    }


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


# --- REST model catalog: one mapped class, three that must raise clearly ----
def test_rest_catalog_maps_complex_motion_to_dop_standard():
    assert (
        REST_MODEL_CATALOG[RenderClass.COMPLEX_MOTION].model_id
        == "higgsfield-ai/dop/standard"
    )


@pytest.mark.parametrize("mcp_id", ["seedance_2_0", "wan2_6", "veo3_1"])
async def test_unmapped_render_classes_raise_clear_error(monkeypatch, mcp_id):
    calls = _install(monkeypatch, lambda m, u, k: FakeResponse(500))  # must NOT be hit
    with pytest.raises(HiggsfieldError, match="no REST model mapping"):
        await _transport()("generate_video", params={"model": mcp_id, "prompt": "x"})
    assert calls == []  # raised before any URL was hit


async def test_unknown_mcp_model_id_raises():
    with pytest.raises(HiggsfieldError, match="unknown MCP catalog model id"):
        await _transport()("generate_video", params={"model": "soul_v2", "prompt": "x"})


# --- (c) envelope normalization vs the adapter-facing MCP shapes ------------
# Recorded FLAT REST responses -> the {"results":[{...}]} shapes the adapter
# unwraps (CLAUDE.md P12.4).

async def test_media_upload_normalized_to_uploads_shape(monkeypatch):
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(
            200, {"upload_url": "https://put/here", "public_url": "https://cdn/x.png"}
        ),
    )
    out = await _transport()("media_upload", filename="test.png")
    # MCP shape the adapter's _upload_local parses: uploads[0] with these keys.
    # The public_url stands in as media_id and later becomes the DoP image_url.
    entry = out["uploads"][0]
    assert entry["upload_url"] == "https://put/here"
    assert entry["media_id"] == "https://cdn/x.png"
    assert entry["url"] == "https://cdn/x.png"
    assert entry["content_type"] == "image/png"


async def test_media_import_url_is_passthrough_no_http(monkeypatch):
    calls = _install(monkeypatch, lambda m, u, k: FakeResponse(500))  # must NOT be hit
    out = await _transport()("media_import_url", url="https://cdn/ref.png", type="image")
    assert out["media_id"] == "https://cdn/ref.png"
    assert calls == []  # a public URL goes straight into image_url; no network


async def test_poll_and_fetch_through_the_real_adapter(monkeypatch):
    # Drive the ACTUAL adapter over the transport: a recorded completed FLAT
    # response with the asset at video.url must surface as adapter
    # poll()->"succeeded" and fetch_result()->the url.
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


async def test_images_url_fallback_for_image_models(monkeypatch):
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(
            200,
            {
                "status": "completed",
                "request_id": "JOB4",
                "images": [{"url": "https://cdn/result.png"}],
            },
        ),
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.fetch_result("JOB4") == "https://cdn/result.png"


# --- (d) the confirmed 5-status vocabulary ----------------------------------
@pytest.mark.parametrize(
    ("rest_status", "adapter_status"),
    [
        ("queued", "running"),
        ("in_progress", "running"),
        ("completed", "succeeded"),
        ("nsfw", "failed"),      # REST-only terminal (refunds); must not poll forever
        ("failed", "failed"),    # refunds too
    ],
)
async def test_status_vocabulary(monkeypatch, rest_status, adapter_status):
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": rest_status, "request_id": "J"}),
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.poll("J") == adapter_status


async def test_unknown_status_stays_running_defensively(monkeypatch):
    _install(
        monkeypatch,
        lambda m, u, k: FakeResponse(200, {"status": "warming_up", "request_id": "J"}),
    )
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.poll("J") == "running"


async def test_submit_through_the_real_adapter(monkeypatch):
    # URL reference -> media_import_url passthrough -> the URL lands in the DoP
    # body's image_url; the queued response yields the request_id as job id.
    def handler(method, url, kwargs):
        assert url == "/higgsfield-ai/dop/standard"
        return FakeResponse(200, {"status": "queued", "request_id": "SUBJOB"})

    calls = _install(monkeypatch, handler)
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    assert await hf.submit(_shot(), "idem-1") == "SUBJOB"
    body = calls[0]["json"]
    assert body["image_url"] == "https://cdn/ref.png"
    assert body["motions"] is None
    assert body["enhance_prompt"] is True
    assert "duration" not in body and "aspect_ratio" not in body and "model" not in body
    # Motion still travels as the folded prompt phrase (MCP-style), since the
    # DoP motions param is TODO(D3-motions).
    assert body["prompt"] == "ink Camera: locked-off static camera."


# --- (g) static-cost preflight fallback (RESOLVED P12.5-live #2) ------------
async def test_preflight_cost_uses_static_table_no_http(monkeypatch):
    calls = _install(monkeypatch, lambda m, u, k: FakeResponse(500))  # must NOT be hit
    hf = HiggsfieldClient(api_key="unused", call_tool=_transport())
    cost = await hf.preflight_cost(_shot())
    assert cost == pytest.approx(estimate_cost(Model.KLING_3, 5))
    assert calls == []  # no REST cost endpoint exists; estimate is local


# --- (f) 4xx/5xx surface as HTTPStatusError for the tenacity classifier ------
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


# --- tools absent from the REST API ------------------------------------------
@pytest.mark.parametrize("tool", ["show_generations", "models_explore", "bogus_tool"])
async def test_unsupported_tools_raise(tool):
    with pytest.raises(HiggsfieldError, match="no REST route"):
        await _transport()(tool)
