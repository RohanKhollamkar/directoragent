"""Real Higgsfield MCP adapter (STEP 12.3).

Implements the HiggsfieldClient Protocol against the live Higgsfield MCP tools
(generate_video, media_upload/import/confirm, job_display, show_generations).

Key facts, confirmed against the live catalog via models_explore:
  - internal Model -> catalog id: seedance_2_0 / kling3_0 / wan2_6 / veo3_1.
  - start-image media role per model: start_image for Seedance/Kling/Veo,
    image_references for Wan (it has no start_image role).
  - get_cost is a no-spend preflight; veo3_1 @ 8s returns {"cost":{"credits":22}}.
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

# poll() status normalization.
# TODO(P12.4): confirm the real status vocabulary from the first live run and
# tighten these sets (job_display's exact status strings were not observable
# non-spending — history was empty at discovery time).
_SUCCEEDED = {"completed", "succeeded", "success", "done", "finished", "ready"}
_FAILED = {"failed", "error", "errored", "canceled", "cancelled", "rejected"}
_RUNNING = {
    "queued", "pending", "processing", "running", "in_progress",
    "in-progress", "started", "submitted",
}

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
        self._fingerprints: dict[str, dict] = {}     # idem_key -> {model, prompt}

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
    async def _put_bytes(self, url: str, data: bytes) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(url, content=data)
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
        # TODO(P12.4): confirm media_upload's response shape on the first live
        # submit; parsed defensively here (presigned upload_url + media_id).
        entry = resp
        for key in ("medias", "files", "items"):
            if isinstance(resp, dict) and isinstance(resp.get(key), list) and resp[key]:
                entry = resp[key][0]
                break
        media_id = _first(entry, ("media_id", "id", "uuid"))
        upload_url = _first(entry, ("upload_url", "url", "put_url"))
        if not media_id or not upload_url:
            raise HiggsfieldError(f"media_upload response missing id/url: {resp!r}")
        await self._put_bytes(upload_url, p.read_bytes())
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
        # Record the fingerprint so reconcile() can best-effort recover the job.
        self._fingerprints[idem_key] = {
            "model": params["model"],
            "prompt": params["prompt"],
        }
        resp = await self._mcp("generate_video", params=params)
        job_id = _first(resp, ("id", "job_id"))
        if job_id is None:
            gen = _first(resp, ("generation", "job", "data"))
            job_id = _first(gen, ("id", "job_id")) if gen else None
        if job_id is None:
            raise HiggsfieldError(f"no job_id in generate_video response: {resp!r}")
        return str(job_id)

    async def poll(self, job_id: str) -> str:
        resp = await self._mcp("job_display", id=job_id)
        raw = _first(resp, ("status",))
        if raw is None:
            gen = _first(resp, ("generation", "job", "result", "data"))
            raw = _first(gen, ("status",)) if gen else None
        status = str(raw).strip().lower() if raw is not None else ""
        if status in _SUCCEEDED:
            return "succeeded"
        if status in _FAILED:
            return "failed"
        if status in _RUNNING:
            return "running"
        # Unknown, non-terminal state: keep polling rather than crash / false-fail.
        # TODO(P12.4): confirm real status vocabulary from the first live run.
        log.warning(
            "higgsfield poll: unknown status %r for job %s; treating as running",
            raw, job_id,
        )
        return "running"

    async def fetch_result(self, job_id: str) -> str:
        resp = await self._mcp("job_display", id=job_id)
        results = _first(resp, ("results", "result"))
        if isinstance(results, list) and results:
            first = results[0]
            url = _first(first, ("url", "asset_url", "video_url", "result_url"))
            if isinstance(url, str) and url:
                return url
            if isinstance(first, str) and first:
                return first
        if isinstance(results, str) and results:
            return results
        raise HiggsfieldError(f"no result asset for job {job_id}: {resp!r}")

    async def reconcile(self, idem_key: str) -> str | None:
        """Best-effort content fingerprint — NOT key-based (the API has no
        idempotency key). Matches recent generations on {model, prompt} recorded
        at submit time (prompts are unique per attempt). Returns a job_id, or
        None if no confident match (the caller then submits fresh — an accepted,
        rare double-cost). After a cross-process crash the fingerprint is gone,
        so this returns None and the caller re-submits."""
        fp = self._fingerprints.get(idem_key)
        if fp is None:
            return None
        resp = await self._mcp("show_generations", type="video", size=24)
        items = _first(resp, ("items",))
        if items is None:
            items = resp if isinstance(resp, list) else []
        for item in items:
            params = _first(item, ("params",)) or {}
            if params.get("model") == fp["model"] and params.get("prompt") == fp["prompt"]:
                jid = _first(item, ("id", "job_id"))
                if jid:
                    return str(jid)
        return None
