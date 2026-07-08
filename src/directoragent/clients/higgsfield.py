"""Real Higgsfield MCP adapter (STEP 12.3).

Implements the HiggsfieldClient Protocol against the live Higgsfield MCP tools
(generate_video, media_upload/import/confirm, job_display, show_generations).

Key facts, confirmed against the live catalog via models_explore and the first
live generation (P12.4, job 58c50606, wan2_6 @ 5s, 13 credits):
  - internal Model -> catalog id: seedance_2_0 / kling3_0 / wan2_6 / veo3_1.
  - start-image media role per model: start_image for Seedance/Kling/Veo,
    image_references for Wan (it has no start_image role).
  - get_cost is a no-spend preflight, shape {"cost":{"credits":N,"credits_exact":N}};
    veo3_1 @ 8s = 22 credits, wan2_6 @ 5s = 13 credits.
  - generate_video and job_display wrap the job in {"results":[{...}]}; the
    completed job carries its asset at results[0].results.rawUrl.
  - status vocabulary (observed live): pending -> in_progress -> completed.
  - the API has NO idempotency key on submit (see reconcile()).
  - there is NO camera-motion parameter: motion_preset is folded into the prompt
    text here, never sent as a generate_video parameter.

Transport: the MCP tools are reached through an injected async `call_tool`
seam — in production an MCP client session created with the api_key. It is
injectable so the adapter's logic is exercisable without a live server binding.
Transient retry (5xx / timeout) lives on that MCP call boundary via tenacity;
4xx (bad request) raises immediately. The retry sits on the single-call
boundary, NOT the whole method, so the media handshake is not blindly replayed.
"""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from directoragent.schema import Model, Shot

log = logging.getLogger(__name__)

# internal Model -> Higgsfield catalog model id (confirmed via models_explore).
MODEL_CATALOG_ID: dict[Model, str] = {
    Model.SEEDANCE_2: "seedance_2_0",
    Model.KLING_3: "kling3_0",
    Model.WAN_2_6: "wan2_6",
    Model.VEO_3_1: "veo3_1",
}

# start-image media role per catalog model (from models_explore medias[].roles).
_START_IMAGE_ROLE: dict[str, str] = {
    "seedance_2_0": "start_image",
    "kling3_0": "start_image",
    "wan2_6": "image_references",   # Wan has no start_image role
    "veo3_1": "start_image",
}

# All four catalog models support 16:9 — a safe deterministic storyboard framing.
_DEFAULT_ASPECT_RATIO = "16:9"

# motion_preset -> prompt phrase. Motion is folded into the prompt; it is NEVER
# a generate_video parameter (the API exposes no camera-motion enum).
_MOTION_PHRASE: dict[str, str] = {
    "PUSH_IN": "slow push-in",
    "PULL_OUT": "pull-out",
    "PAN_LEFT": "pan left",
    "PAN_RIGHT": "pan right",
    "TILT_UP": "tilt up",
    "TILT_DOWN": "tilt down",
    "ORBIT": "orbital camera move",
    "STATIC": "locked-off static camera",
    "HANDHELD": "handheld camera",
    "CRANE": "crane camera move",
}

# poll() status normalization. Vocabulary confirmed on the first live run
# (P12.4): submit returns "pending", polling shows "in_progress", terminal is
# "completed". The failure path was not exercised live (success on the only
# job), so _FAILED keeps conservative candidates — anything unrecognized falls
# through to the defensive unknown -> running branch in poll().
_SUCCEEDED = {"completed"}
_FAILED = {"failed", "error", "canceled", "cancelled", "rejected"}
_RUNNING = {"pending", "in_progress", "queued", "processing"}

ToolCaller = Callable[..., Awaitable[Any]]


class HiggsfieldError(RuntimeError):
    """Non-retryable adapter error (bad request, missing result, no transport)."""


def _is_transient(exc: BaseException) -> bool:
    """Retry only 5xx and timeouts/transport errors; 4xx raises immediately."""
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _first(obj: Any, keys: tuple[str, ...]) -> Any:
    """First present, non-None key from a dict (defensive response parsing)."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) is not None:
                return obj[k]
    return None


def _job_entry(resp: Any) -> dict:
    """Unwrap a job response. generate_video and job_display both wrap the job
    in {"results": [{...}]} (confirmed live at P12.4); tolerate an unwrapped
    single-job dict as a fallback."""
    results = _first(resp, ("results",))
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    if isinstance(resp, dict):
        return resp
    raise HiggsfieldError(f"unrecognized job response shape: {resp!r}")


_RETRY = retry(
    retry=retry_if_exception(_is_transient),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(4),
    reraise=True,
)


class HiggsfieldClient:
    def __init__(self, api_key: str, call_tool: ToolCaller | None = None):
        self._api_key = api_key
        self._call_tool = call_tool                  # MCP transport seam (async)
        self._media_cache: dict[str, str] = {}       # source path -> media_id

    # --- MCP / network call boundary (the retried seam) ---------------------
    @_RETRY
    async def _mcp(self, tool: str, **params: Any) -> Any:
        if self._call_tool is None:
            raise HiggsfieldError(
                f"no MCP transport configured for {tool!r}; construct "
                "HiggsfieldClient(api_key, call_tool=<mcp session>)"
            )
        return await self._call_tool(tool, **params)

    @_RETRY
    async def _put_bytes(self, url: str, data: bytes, content_type: str) -> None:
        # The presigned URL signs the content-type header (X-Amz-SignedHeaders
        # includes content-type), so the PUT must send the exact Content-Type
        # media_upload was told about or S3 rejects it with 403.
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(
                url, content=data, headers={"Content-Type": content_type}
            )
            resp.raise_for_status()

    # --- Prompt / params ----------------------------------------------------
    @staticmethod
    def _prompt_with_motion(shot: Shot) -> str:
        preset = (shot.motion_preset or "").upper()
        phrase = _MOTION_PHRASE.get(preset, preset.lower().replace("_", " "))
        return f"{shot.prompt} Camera: {phrase}." if phrase else shot.prompt

    def _generate_params(self, shot: Shot, media_id: str | None) -> dict:
        model_id = MODEL_CATALOG_ID[shot.model]
        params: dict[str, Any] = {
            "model": model_id,
            "prompt": self._prompt_with_motion(shot),
            "duration": int(shot.duration_s),
            "aspect_ratio": _DEFAULT_ASPECT_RATIO,
        }
        if media_id is not None:
            params["medias"] = [{"value": media_id, "role": _START_IMAGE_ROLE[model_id]}]
        return params

    # --- Media handshake ----------------------------------------------------
    async def _resolve_media(self, source: str) -> str:
        """Register the reference image once per source path; cache the media_id
        so six shots off the same photo import it once, not six times."""
        if source in self._media_cache:
            return self._media_cache[source]
        if source.startswith(("http://", "https://")):
            resp = await self._mcp("media_import_url", url=source, type="image")
            # Shape confirmed live at P12.4: {"media_id": <uuid>, "type",
            # "content_type", "source_url"} — the id is already confirmed.
            media_id = _first(resp, ("media_id", "id", "uuid"))
            if not media_id:
                raise HiggsfieldError(f"media_import_url returned no media_id: {resp!r}")
        else:
            media_id = await self._upload_local(source)
        self._media_cache[source] = media_id
        return media_id

    async def _upload_local(self, path: str) -> str:
        p = Path(path)
        resp = await self._mcp("media_upload", filename=p.name)
        # Shape confirmed live at P12.4: {"uploads": [{"upload_url": <presigned
        # PUT url>, "media_id": <uuid>, "url": <cdn url>, "content_type": ...}]}.
        entry = resp
        for key in ("uploads", "medias", "files", "items"):
            if isinstance(resp, dict) and isinstance(resp.get(key), list) and resp[key]:
                entry = resp[key][0]
                break
        media_id = _first(entry, ("media_id", "id", "uuid"))
        upload_url = _first(entry, ("upload_url", "put_url"))
        if not media_id or not upload_url:
            raise HiggsfieldError(f"media_upload response missing id/url: {resp!r}")
        content_type = _first(entry, ("content_type",)) or "image/png"
        await self._put_bytes(upload_url, p.read_bytes(), content_type)
        await self._mcp("media_confirm", media_id=media_id, type="image")
        return media_id

    # --- Protocol methods ---------------------------------------------------
    async def preflight_cost(self, shot: Shot) -> float:
        # No media in the preflight: get_cost is model/duration/quality-driven
        # (verified: veo3_1 @ 8s -> 22 credits with no media), and requiring an
        # upload just to preview cost would couple budgeting to a media
        # handshake. submit() sends the same params PLUS the resolved media.
        params = self._generate_params(shot, media_id=None)
        params["get_cost"] = True
        resp = await self._mcp("generate_video", params=params)
        cost = _first(resp, ("cost",))
        credits = _first(cost, ("credits_exact", "credits")) if cost else None
        if credits is None:
            raise HiggsfieldError(f"no cost in get_cost response: {resp!r}")
        return float(credits)

    async def submit(self, shot: Shot, idem_key: str) -> str:
        media_id = await self._resolve_media(shot.reference.source)
        params = self._generate_params(shot, media_id)
        # idem_key is intentionally NOT sent — the API has no idempotency key.
        # reconcile() recovers a crashed submission by re-deriving the content
        # fingerprint from the persisted Shot, so nothing is recorded here.
        resp = await self._mcp("generate_video", params=params)
        # Confirmed live at P12.4: {"results": [{"id": <job uuid>, "status":
        # "pending", "model": ..., "params": {...}}]}.
        job_id = _first(_job_entry(resp), ("id", "job_id"))
        if job_id is None:
            raise HiggsfieldError(f"no job_id in generate_video response: {resp!r}")
        return str(job_id)

    async def poll(self, job_id: str) -> str:
        resp = await self._mcp("job_display", id=job_id)
        raw = _first(_job_entry(resp), ("status",))
        status = str(raw).strip().lower() if raw is not None else ""
        if status in _SUCCEEDED:
            return "succeeded"
        if status in _FAILED:
            return "failed"
        if status in _RUNNING:
            return "running"
        # Unknown, non-terminal state: keep polling rather than crash / false-fail.
        log.warning(
            "higgsfield poll: unknown status %r for job %s; treating as running",
            raw, job_id,
        )
        return "running"

    async def fetch_result(self, job_id: str) -> str:
        resp = await self._mcp("job_display", id=job_id)
        # Confirmed live at P12.4: the completed job entry carries its asset at
        # entry["results"]["rawUrl"] (a dict, nested inside the outer
        # {"results": [...]} envelope).
        results = _first(_job_entry(resp), ("results", "result"))
        if isinstance(results, dict):
            url = _first(results, ("rawUrl", "raw_url", "url"))
            if isinstance(url, str) and url:
                return url
        if isinstance(results, str) and results:
            return results
        raise HiggsfieldError(f"no result asset for job {job_id}: {resp!r}")

    async def reconcile(self, idem_key: str, shot: Shot) -> str | None:
        """Best-effort content fingerprint — NOT key-based (the API has no
        idempotency key; `idem_key` is unused here). The fingerprint {catalog
        model id, full generation prompt incl. the folded motion phrase} is
        derived from the persisted Shot at call time, so recovery survives a
        cross-process crash. The prompt is IDENTICAL across a shot's
        quality-retry attempts, so anything but exactly one match is ambiguous:
        zero or MULTIPLE matches return None and the caller submits fresh (an
        accepted, rare double-charge)."""
        fp = self._generate_params(shot, media_id=None)
        # job_display/generate_video use a {"results": [...]} envelope (P12.4),
        # so check it here too; "items" kept as a defensive fallback.
        resp = await self._mcp("show_generations", type="video", size=24)
        items = _first(resp, ("results", "items"))
        if items is None:
            items = resp if isinstance(resp, list) else []
        matches: list[str] = []
        for item in items:
            params = _first(item, ("params",)) or {}
            if params.get("model") == fp["model"] and params.get("prompt") == fp["prompt"]:
                jid = _first(item, ("id", "job_id"))
                if jid:
                    matches.append(str(jid))
        if len(matches) != 1:
            if matches:
                log.warning(
                    "higgsfield reconcile: %d fingerprint matches for shot %s "
                    "— ambiguous, submitting fresh",
                    len(matches), shot.shot_id,
                )
            return None
        return matches[0]
