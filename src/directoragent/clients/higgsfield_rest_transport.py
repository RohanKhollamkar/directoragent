"""REST transport for the Higgsfield adapter (STEP 12.5, corrected at D2).

An authenticated httpx call_tool seam that lets HiggsfieldClient run OUTSIDE a
Claude session (deployable path). It is ADDITIVE: the agent-mediated transport
(the Claude connector invoking mcp__Higgsfield__* in-session, proven at P12.4)
is untouched. Both satisfy the same `call_tool(tool, **params) -> dict`
contract; pipeline.py picks one by config.

Contract confirmed at D2 against the Higgsfield Cloud API docs + the DoP
Standard reference (the P12.5 version was built against SDK-derived guesses):
  - base https://platform.higgsfield.ai, auth `Authorization: Key KEY_ID:KEY_SECRET`
  - submit POST /{model_id} — the model id IS the path
    (e.g. higgsfield-ai/dop/standard); response
    {"status":"queued","request_id":<uuid>,"status_url":..,"cancel_url":..}
  - poll   GET  /requests/{request_id}/status
  - cancel POST /requests/{request_id}/cancel
  - completed response is FLAT: {"status":"completed","request_id":..,
    "images":[{"url":..}],"video":{"url":..}} — video asset at video.url,
    NOT the MCP results[0].results.rawUrl
  - status vocabulary: queued|in_progress (running), completed (succeeded),
    nsfw|failed (failed; BOTH refund credits)
  - upload POST /files/generate-upload-url {content_type}
    -> {upload_url, public_url}, PUT the bytes, then the public_url is the
    generation input (image_url)
  - NO cost endpoint exists (submit/status/cancel only — confirmed), and no
    generations-list endpoint either.

TWO CATALOGS, one seam: the MCP connector exposes the Seedance/Kling/Veo/Wan
catalog; the REST Cloud API exposes a DIFFERENT product line (DoP/Soul) with a
separate credit pool (MCP plus-plan credits vs cloud.higgsfield.ai API
credits). REST_MODEL_CATALOG below maps the pipeline's render_classes onto the
REST catalog; D2 wires one model (DoP Standard) as the demonstration — mapping
the remaining render_classes to REST models is a documented config step.

The adapter (clients/higgsfield.py) was written against the MCP wire shapes
(P12.4). To keep it shape-stable across transports, ALL REST->MCP normalization
lives HERE — the adapter never learns it is talking to REST.

RESOLVED(P12.5-live #1): real submit endpoint is POST /{model_id} — the
/v2/generate placeholder is gone.
RESOLVED(P12.5-live #2): no REST cost endpoint exists, so get_cost preflight
degrades to the static COST_PER_SECOND estimate (routing.py); actual credits
reconcile post-submit from the terminal status (nsfw/failed refund, so only a
`completed` job costs). Live smoke (first paid REST submit) remains deploy-time:
the sandbox egress proxy blocks platform.higgsfield.ai, so everything here is
stub-tested against the recorded shapes above.
"""

import logging
import mimetypes
from collections.abc import Callable
from typing import Any, NamedTuple

import httpx

from directoragent.clients.higgsfield import MODEL_CATALOG_ID, HiggsfieldError
from directoragent.routing import ROUTING, estimate_cost
from directoragent.schema import Model, RenderClass

log = logging.getLogger(__name__)

# tool -> (method, path template) for the tools backed by a real HTTP endpoint.
# {model_id}/{id} are filled per call. Tools that are REST no-ops
# (media_import_url, media_confirm) or absent from the REST API
# (show_generations, models_explore) are handled explicitly in __call__.
_ROUTES: dict[str, tuple[str, str]] = {
    "generate_video": ("POST", "/{model_id}"),          # model id IS the path
    "job_display": ("GET", "/requests/{id}/status"),
    "cancel": ("POST", "/requests/{id}/cancel"),
    "media_upload": ("POST", "/files/generate-upload-url"),
}


def _dop_standard_body(mcp_params: dict) -> dict:
    """MCP-shaped generate params -> the confirmed DoP Standard request body.

    DoP takes NO duration and NO aspect_ratio; the start image is a public URL
    in image_url (over REST, media_import_url passes URLs through and
    media_upload yields a public_url, so medias[0].value is always a URL here).

    `motions` is a REAL DoP parameter — unlike the MCP path, where motion has
    no parameter and is folded into the prompt text. Wired as null for now (the
    folded prompt phrase still carries the intent).
    TODO(D3-motions): map motion_preset -> a DoP motion value from
    GET /v1/motions once the catalog is fetched.
    """
    medias = mcp_params.get("medias") or []
    image_url = medias[0].get("value") if medias else None
    return {
        "seed": None,
        "prompt": mcp_params.get("prompt"),
        "motions": None,
        "image_url": image_url,
        "enhance_prompt": True,
    }


class RestModel(NamedTuple):
    model_id: str                          # the submit path, POST /{model_id}
    build_body: Callable[[dict], dict]     # MCP params -> REST request body


# render_class -> REST Cloud model. The REST catalog (DoP/Soul) is a different
# product than the MCP catalog, so this is a SEPARATE mapping from ROUTING, not
# a re-route: the planner already routed the shot (invariant #3 intact — the
# routing function is never called here); this only translates the
# already-routed shot into the REST product namespace. D2 maps
# COMPLEX_MOTION -> DoP Standard: DoP
# ("Director of Photography") is the cinematic image->video model whose
# signature capability is real camera-motion control (the `motions` parameter),
# matching COMPLEX_MOTION's routing intent. Mapping the other three
# render_classes to REST models is a documented config step.
REST_MODEL_CATALOG: dict[RenderClass, RestModel] = {
    RenderClass.COMPLEX_MOTION: RestModel(
        model_id="higgsfield-ai/dop/standard",
        build_body=_dop_standard_body,
    ),
}

# The transport receives MCP catalog ids (the adapter's _generate_params sets
# params["model"] from MODEL_CATALOG_ID). Invert the two frozen tables to get
# back to the render_class / Model the id came from. Pure data reads of the
# ROUTING table — the routing function itself is never called (invariant #3).
_MCP_ID_TO_RENDER_CLASS: dict[str, RenderClass] = {
    MODEL_CATALOG_ID[model]: rc for rc, (model, _reason) in ROUTING.items()
}
_MCP_ID_TO_MODEL: dict[str, Model] = {v: k for k, v in MODEL_CATALOG_ID.items()}

# REST status -> the vocabulary the adapter's poll() sets know (higgsfield.py
# _SUCCEEDED/_FAILED/_RUNNING). Confirmed vocabulary: queued/in_progress ->
# running, completed -> succeeded, nsfw/failed -> failed (both REFUND credits).
# Only strings the adapter would NOT recognize are remapped (queued/in_progress/
# completed/failed pass through); crucially `nsfw` is terminal-failure in REST
# but not in the adapter's _FAILED set, so without this it would hit the
# defensive unknown->running branch and poll forever. `canceled` (the cancel
# endpoint's terminal state) folds to failed too.
_STATUS_MAP: dict[str, str] = {
    "nsfw": "failed",
    "canceled": "failed",
    "cancelled": "failed",
}


class HiggsfieldRestTransport:
    """Async `call_tool` callable backed by the Higgsfield Cloud REST API.

    Inject as HiggsfieldClient(api_key, call_tool=HiggsfieldRestTransport(...)).
    Raises httpx.HTTPStatusError UNSWALLOWED on 4xx/5xx so the adapter's tenacity
    classifier keeps working (5xx/timeout retried in-adapter, 4xx immediate).
    """

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        base_url: str = "https://platform.higgsfield.ai",
    ):
        if not key_id or not key_secret:
            raise HiggsfieldError(
                "HiggsfieldRestTransport needs both key_id and key_secret "
                "(REST auth is `Authorization: Key KEY_ID:KEY_SECRET`)"
            )
        # One shared client; 120s timeout matches the SDK default.
        # raise_for_status is called per-request so transport errors reach the
        # adapter's tenacity seam.
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
        if tool == "cancel":
            return await self._cancel(params)
        if tool == "media_upload":
            return await self._media_upload(params)
        if tool == "media_import_url":
            return self._media_import_url(params)
        if tool == "media_confirm":
            return self._media_confirm(params)
        if tool in ("show_generations", "models_explore"):
            # Confirmed at D2: the REST API is submit/status/cancel only —
            # there is no generations-list or model-catalog endpoint, so
            # reconcile()/discovery are MCP-transport-only capabilities.
            raise HiggsfieldError(
                f"no REST route for tool {tool!r} — the Cloud API exposes only "
                "submit/status/cancel (confirmed D2); reconcile and catalog "
                "discovery need the agent-mediated MCP transport"
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
        mcp_model_id = body.get("model")
        if body.pop("get_cost", False):
            return self._static_cost(mcp_model_id, body)
        rest_model = self._rest_model_for(mcp_model_id)
        method, tmpl = _ROUTES["generate_video"]
        path = tmpl.format(model_id=rest_model.model_id)
        rest = await self._request(method, path, json=rest_model.build_body(body))
        return self._normalize_job(rest)

    def _rest_model_for(self, mcp_model_id: str | None) -> RestModel:
        render_class = _MCP_ID_TO_RENDER_CLASS.get(mcp_model_id or "")
        if render_class is None:
            raise HiggsfieldError(
                f"unknown MCP catalog model id {mcp_model_id!r} — cannot map "
                "to a REST model"
            )
        rest_model = REST_MODEL_CATALOG.get(render_class)
        if rest_model is None:
            raise HiggsfieldError(
                f"render_class {render_class.value} has no REST model mapping "
                "— REST exposes DoP/Soul, not the MCP catalog; see docs"
            )
        return rest_model

    def _static_cost(self, mcp_model_id: str | None, body: dict) -> dict:
        # RESOLVED(P12.5-live #2): the REST API has NO cost endpoint (confirmed
        # — only submit/status/cancel), so the preflight degrades to the static
        # COST_PER_SECOND estimate (placeholder cost-units, per CLAUDE.md).
        # Actual credits reconcile post-submit from the terminal status:
        # nsfw/failed REFUND, so only a completed job costs. No network call.
        model = _MCP_ID_TO_MODEL.get(mcp_model_id or "")
        if model is None:
            raise HiggsfieldError(
                f"unknown MCP catalog model id {mcp_model_id!r} — cannot "
                "estimate cost"
            )
        est = estimate_cost(model, float(body.get("duration") or 0))
        # Same shape the adapter's preflight_cost parses on the MCP path.
        return {"cost": {"credits": est, "credits_exact": est}}

    async def _job_display(self, params: dict) -> dict:
        job_id = params["id"]
        method, tmpl = _ROUTES["job_display"]
        rest = await self._request(method, tmpl.format(id=job_id))
        return self._normalize_job(rest, fallback_id=job_id)

    async def _cancel(self, params: dict) -> dict:
        job_id = params["id"]
        method, tmpl = _ROUTES["cancel"]
        return await self._request(method, tmpl.format(id=job_id))

    async def _media_upload(self, params: dict) -> dict:
        # Adapter calls media_upload(filename=...) only; REST needs a
        # content_type, so derive it from the extension.
        filename = params.get("filename", "upload.bin")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        method, path = _ROUTES["media_upload"]
        rest = await self._request(method, path, json={"content_type": content_type})
        # REST returns {upload_url, public_url} — no media_id and no confirm
        # step. After the adapter PUTs the bytes, the public_url IS the
        # generation input (it flows through medias[].value into the REST
        # body's image_url), so it stands in for the adapter's media_id.
        # Normalize to the MCP "uploads" shape the adapter's _upload_local
        # parses (CLAUDE.md P12.4).
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
        # REST has no import step: a public URL goes straight into the request
        # body's image_url (via medias[].value). Normalize to the MCP
        # media_import_url shape (media_id key) with the URL standing in as the
        # reference id — no network call.
        url = params["url"]
        return {"media_id": url, "type": params.get("type", "image"), "source_url": url}

    def _media_confirm(self, params: dict) -> dict:
        # REST upload (PUT to the presigned url) needs no confirm step.
        # No-op that echoes the id for shape stability.
        media_id = params.get("media_id")
        return {"media_id": media_id, "status": "confirmed"}

    # --- envelope normalization (the heart of transport-shape-stability) ----
    def _normalize_job(self, rest: Any, fallback_id: str | None = None) -> dict:
        """Flat REST response -> the MCP {"results":[{...}]} envelope the
        adapter unwraps via _job_entry(). Confirmed flat shapes (D2):
        submit -> {"status":"queued","request_id":..,"status_url":..,
        "cancel_url":..}; completed poll -> {"status":"completed",
        "request_id":..,"images":[{"url":..}],"video":{"url":..}}. Status comes
        from top-level `status` (remapped to the adapter's vocab); the asset
        URL from video.url (falling back to images[0].url for image models)
        goes to results[0].results.rawUrl."""
        if not isinstance(rest, dict):
            raise HiggsfieldError(f"unexpected REST job response: {rest!r}")
        raw_status = str(rest.get("status", "") or "").strip().lower()
        status = _STATUS_MAP.get(raw_status, raw_status)
        job_id = rest.get("request_id") or fallback_id
        entry: dict[str, Any] = {"id": job_id, "status": status}

        url = None
        video = rest.get("video")
        images = rest.get("images")
        if isinstance(video, dict) and video.get("url"):
            url = video["url"]
        elif isinstance(images, list) and images and isinstance(images[0], dict):
            url = images[0].get("url")
        if url:
            # Adapter's fetch_result reads results[0].results.rawUrl (a dict).
            entry["results"] = {"rawUrl": url}

        return {"results": [entry]}
