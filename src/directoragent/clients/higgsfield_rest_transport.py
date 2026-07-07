"""REST transport for the Higgsfield adapter (STEP 12.5).

An authenticated httpx call_tool seam that lets HiggsfieldClient run OUTSIDE a
Claude session (deployable path). It is ADDITIVE: the agent-mediated transport
(the Claude connector invoking mcp__Higgsfield__* in-session, proven at P12.4)
is untouched. Both satisfy the same `call_tool(tool, **params) -> dict`
contract; pipeline.py picks one by config.

Contract derived from the official SDK (github.com/higgsfield-ai/higgsfield-js,
v2 client) — every route/shape decision below cites the SDK line it came from:
  - base https://platform.higgsfield.ai, auth header
    `Authorization: Key KEY_ID:KEY_SECRET`                 (v2/client.ts:109)
  - submit POST to a per-model endpoint, body = the input  (v2/client.ts:271-283)
  - poll   GET /requests/{request_id}/status               (v2/client.ts:182)
  - upload POST /files/generate-upload-url {content_type}
           -> {upload_url, public_url}, then PUT the bytes  (client.ts:139-160)
  - flat V2Response {status, request_id, images:[{url}],
    video:{url}} — NOT the MCP {"results":[...]} envelope   (v2/types.ts:87-94)
  - status vocab queued|in_progress|completed|failed|nsfw   (v2/types.ts:77)

The adapter (clients/higgsfield.py) was written against the MCP wire shapes
(P12.4). To keep it shape-stable across transports, ALL REST->MCP normalization
lives HERE — the adapter never learns it is talking to REST.

TODO(P12.5-live): live smoke (models_explore + get_cost) pending first run
outside the sandbox / an allowlisted environment. The egress proxy blocks
platform.higgsfield.ai from the Claude sandbox (CONNECT 403, verified P12.4/12.5),
so the routes marked TODO(P12.5-live) below are derived from the SDK but have
NOT been exercised end-to-end. Confirm and tighten them on that first run.
"""

import logging
import mimetypes
from typing import Any

import httpx

from directoragent.clients.higgsfield import HiggsfieldError

log = logging.getLogger(__name__)

# tool -> (method, path template) for the tools backed by a real HTTP endpoint
# in the SDK. {id} is filled from params. Tools that are REST no-ops
# (media_import_url, media_confirm) or unsupported (show_generations,
# models_explore, get_cost) are handled explicitly in __call__, NOT here.
_ROUTES: dict[str, tuple[str, str]] = {
    "generate_video": ("POST", "{generate_path}"),   # per-model, TODO(P12.5-live)
    "job_display": ("GET", "/requests/{id}/status"),  # v2/client.ts:182
    "media_upload": ("POST", "/files/generate-upload-url"),  # client.ts:140
}

# TODO(P12.5-live): the per-model REST generation endpoint is backend-schema
# driven (ModelSchema.endpoint, loaded from the CMS at runtime — v2/types.ts:61).
# The open-source SDK ships only example paths (/v1/image2video/dop,
# /v1/text2image/soul — v2/types.ts:50-54), never the wan2_6/veo3_1/... catalog
# paths. This placeholder keeps submit runnable/testable; confirm the real path
# (likely per-model) via models_explore / the live schema on the first
# out-of-sandbox run, then map it per Model.
_DEFAULT_GENERATE_PATH = "/v2/generate"

# REST V2RequestStatus -> the status vocabulary the adapter's poll() sets know
# (higgsfield.py _SUCCEEDED/_FAILED/_RUNNING). Only the strings that DIFFER from
# what the adapter recognizes are remapped; the rest pass through. Crucially
# `nsfw` is terminal-failure in REST (v2/types.ts:77) but is NOT in the adapter's
# _FAILED set, so without this it would hit the defensive unknown->running branch
# and poll forever. `canceled` (v1 JobStatus, types.ts:7) folds to failed too.
_STATUS_MAP: dict[str, str] = {
    "nsfw": "failed",
    "canceled": "failed",
    "cancelled": "failed",
}


class HiggsfieldRestTransport:
    """Async `call_tool` callable backed by the Higgsfield REST API.

    Inject as HiggsfieldClient(api_key, call_tool=HiggsfieldRestTransport(...)).
    Raises httpx.HTTPStatusError UNSWALLOWED on 4xx/5xx so the adapter's tenacity
    classifier keeps working (5xx/timeout retried in-adapter, 4xx immediate).
    """

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        base_url: str = "https://platform.higgsfield.ai",
        generate_path: str = _DEFAULT_GENERATE_PATH,
    ):
        if not key_id or not key_secret:
            raise HiggsfieldError(
                "HiggsfieldRestTransport needs both key_id and key_secret "
                "(REST auth is `Authorization: Key KEY_ID:KEY_SECRET`)"
            )
        self._generate_path = generate_path
        # One shared client. Auth header per v2/client.ts:109; 120s timeout
        # matches the SDK default (config.ts:17). raise_for_status is called
        # per-request so transport errors reach the adapter's tenacity seam.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=120.0,
            headers={
                "Authorization": f"Key {key_id}:{key_secret}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- the call_tool seam -------------------------------------------------
    async def __call__(self, tool: str, **params: Any) -> dict:
        if tool == "generate_video":
            return await self._generate_video(params)
        if tool == "job_display":
            return await self._job_display(params)
        if tool == "media_upload":
            return await self._media_upload(params)
        if tool == "media_import_url":
            return self._media_import_url(params)
        if tool == "media_confirm":
            return self._media_confirm(params)
        if tool in ("show_generations", "models_explore"):
            # SDK has no generations-list / models-catalog endpoint (only
            # listSoulIds + schema-loader). reconcile()/discovery over REST need
            # these confirmed live.
            raise HiggsfieldError(
                f"no REST route for tool {tool!r} — not in the SDK "
                "(TODO(P12.5-live): confirm the live endpoint)"
            )
        raise HiggsfieldError(f"no REST route for tool {tool!r}")

    # --- HTTP boundary ------------------------------------------------------
    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        resp = await self._client.request(method, path, json=json)
        resp.raise_for_status()  # 4xx/5xx -> httpx.HTTPStatusError, unswallowed
        return resp.json()

    # --- per-tool handlers + normalization ----------------------------------
    async def _generate_video(self, params: dict) -> dict:
        body = dict(params.get("params") or {})
        if body.pop("get_cost", False):
            # No preflight/get_cost anywhere in the SDK.
            raise HiggsfieldError(
                "get_cost preflight has no REST equivalent "
                "(TODO(P12.5-live): confirm whether the platform exposes a "
                "no-spend cost route; until then budget via the MCP transport)"
            )
        method, _ = _ROUTES["generate_video"]
        # v2 posts the input object directly as the body (v2/client.ts:271-283).
        v2 = await self._request(method, self._generate_path, json=body)
        return self._normalize_job(v2)

    async def _job_display(self, params: dict) -> dict:
        job_id = params["id"]
        method, tmpl = _ROUTES["job_display"]
        v2 = await self._request(method, tmpl.format(id=job_id))
        return self._normalize_job(v2, fallback_id=job_id)

    async def _media_upload(self, params: dict) -> dict:
        # Adapter calls media_upload(filename=...) only; REST needs a
        # content_type (client.ts:139-148), so derive it from the extension.
        filename = params.get("filename", "upload.bin")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        method, path = _ROUTES["media_upload"]
        rest = await self._request(method, path, json={"content_type": content_type})
        # REST returns {upload_url, public_url} (types.ts:66-69) — no media_id and
        # no confirm step. The public_url IS the reference used as the generation
        # input, so it stands in for the adapter's media_id. Normalize to the MCP
        # "uploads" shape the adapter's _upload_local parses (CLAUDE.md P12.4).
        upload_url = rest.get("upload_url")
        public_url = rest.get("public_url")
        if not upload_url or not public_url:
            raise HiggsfieldError(
                f"generate-upload-url response missing url(s): {rest!r}"
            )
        return {
            "uploads": [
                {
                    "upload_url": upload_url,
                    "media_id": public_url,
                    "url": public_url,
                    "content_type": content_type,
                }
            ]
        }

    def _media_import_url(self, params: dict) -> dict:
        # REST has no import step: a public URL is passed straight through as the
        # image reference (v2 input takes image_url directly — v2/types.ts:4-7).
        # Normalize to the MCP media_import_url shape (media_id key) with the URL
        # standing in as the reference id — no network call.
        url = params["url"]
        return {"media_id": url, "type": params.get("type", "image"), "source_url": url}

    def _media_confirm(self, params: dict) -> dict:
        # REST upload (PUT to the presigned url) needs no confirm step
        # (client.ts:153-160). No-op that echoes the id for shape stability.
        media_id = params.get("media_id")
        return {"media_id": media_id, "status": "confirmed"}

    # --- envelope normalization (the heart of transport-shape-stability) ----
    def _normalize_job(self, v2: Any, fallback_id: str | None = None) -> dict:
        """Flat V2Response -> the MCP {"results":[{...}]} envelope the adapter
        unwraps via _job_entry(). Asset URL goes to results[0].results.rawUrl
        (video.url or images[0].url), status is remapped to the adapter's vocab.
        Cites v2/types.ts:87-94 (V2Response) and JobSet.ts:102-128 (image/video
        -> raw result)."""
        if not isinstance(v2, dict):
            raise HiggsfieldError(f"unexpected REST job response: {v2!r}")
        raw_status = str(v2.get("status", "") or "").strip().lower()
        status = _STATUS_MAP.get(raw_status, raw_status)
        job_id = v2.get("request_id") or fallback_id
        entry: dict[str, Any] = {"id": job_id, "status": status}

        url = None
        video = v2.get("video")
        images = v2.get("images")
        if isinstance(video, dict) and video.get("url"):
            url = video["url"]
        elif isinstance(images, list) and images and isinstance(images[0], dict):
            url = images[0].get("url")
        if url:
            # Adapter's fetch_result reads results[0].results.rawUrl (a dict).
            entry["results"] = {"rawUrl": url}

        return {"results": [entry]}
