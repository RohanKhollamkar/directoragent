"""Real CLIP drift scorer (STEP 13).

Implements the DriftScorer Protocol: CLIP cosine similarity between the source
reference photo (local path) and the generated candidate (for real Higgsfield
output, an .mp4 URL — results[0].results.rawUrl — from which one mid-point
frame is extracted; an image candidate is embedded directly).

Lazy loading is a hard invariant (CLAUDE.md #6): torch / open_clip / av / PIL
are imported ONLY inside the helpers called on the first score() —
`import directoragent` must never drag in torch (test_invariants enforces it).
Heavy CLIP work is sync/CPU-bound, so it runs in a thread via asyncio.to_thread
and never blocks the event loop during the parallel fan-out.

Failure policy: real generation fails in messy ways (dead URL, zero-byte file,
undecodable video, no extractable frame). Any failure returns 0.0 — which the
executor treats as drift failure -> quality retry — with a WARNING log. score()
never raises: a scorer exception must not crash the whole fan-out.

Dependencies live in the `clip` optional extra (torch, open-clip-torch, av,
pillow); the base install and CI stay torch-free.

TODO(P13-live): verify real CLIP scoring on an actual Higgsfield .mp4 output —
the first real end-to-end drift score needs a real generated video (same
deferral pattern as P12.5-live).
"""

import asyncio
import io
import logging
import math

import httpx

log = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 60.0


class ClipDriftScorer:
    def __init__(
        self,
        model_name: str = "ViT-B-32",
        pretrained: str = "laion2b_s34b_b79k",
    ):
        self._model_name = model_name
        self._pretrained = pretrained
        self._model = None       # populated once by _ensure_model()
        self._preprocess = None
        self._torch = None

    # --- lazy model loading (the ONLY place torch/open_clip are imported) ----
    def _ensure_model(self):
        if self._model is None:
            import open_clip
            import torch

            model, _, preprocess = open_clip.create_model_and_transforms(
                self._model_name, pretrained=self._pretrained
            )
            model.eval()  # CPU inference; no GPU assumption
            self._model = model
            self._preprocess = preprocess
            self._torch = torch
        return self._model, self._preprocess, self._torch

    # --- DriftScorer protocol -------------------------------------------------
    async def score(self, reference_path: str, candidate_url: str) -> float:
        try:
            data, content_type = await self._fetch_candidate(candidate_url)
            if not data:
                log.warning(
                    "clip score: empty candidate %s for ref %s -> 0.0",
                    candidate_url, reference_path,
                )
                return 0.0
            return await asyncio.to_thread(
                self._score_sync, reference_path, data, content_type
            )
        except Exception as exc:  # noqa: BLE001 — never crash the fan-out
            log.warning(
                "clip score failed for candidate %s vs ref %s -> 0.0 (%s: %s)",
                candidate_url, reference_path, type(exc).__name__, exc,
            )
            return 0.0

    # --- candidate acquisition -------------------------------------------------
    async def _fetch_candidate(self, candidate_url: str) -> tuple[bytes, str]:
        """Download an http(s) candidate; read a local path directly (mock runs
        pass file paths). Returns (bytes, content_type)."""
        if candidate_url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(
                timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as client:
                resp = await client.get(candidate_url)
                resp.raise_for_status()
                return resp.content, resp.headers.get("content-type", "")
        from pathlib import Path

        p = Path(candidate_url)
        suffix = p.suffix.lower()
        content_type = "image/" + suffix.lstrip(".") if suffix in {
            ".png", ".jpg", ".jpeg", ".webp"
        } else "video/mp4"
        return p.read_bytes(), content_type

    # --- sync CLIP path (runs in a thread) --------------------------------------
    def _score_sync(self, reference_path: str, data: bytes, content_type: str) -> float:
        self._ensure_model()
        ref_img = self._load_reference(reference_path)
        cand_img = self._candidate_frame(data, content_type)
        ref_emb = self._embed(ref_img)
        cand_emb = self._embed(cand_img)
        return self._cosine(ref_emb, cand_emb)

    @staticmethod
    def _load_reference(reference_path: str):
        from PIL import Image

        return Image.open(reference_path).convert("RGB")

    @staticmethod
    def _candidate_frame(data: bytes, content_type: str):
        """Image candidate -> decode directly. Video candidate -> one mid-point
        frame (single frame keeps it simple and cheap; sampling many is not
        worth it for a drift gate)."""
        from PIL import Image

        if content_type.lower().startswith("image/"):
            return Image.open(io.BytesIO(data)).convert("RGB")

        import av

        with av.open(io.BytesIO(data)) as container:
            stream = container.streams.video[0]
            if stream.duration is not None:
                container.seek(stream.duration // 2, stream=stream)
            for frame in container.decode(stream):
                return frame.to_image().convert("RGB")
        raise ValueError("no decodable video frame in candidate")

    def _embed(self, image) -> list[float]:
        """CLIP image embedding as a plain list (keeps _cosine torch-free)."""
        model, preprocess, torch = self._ensure_model()
        tensor = preprocess(image).unsqueeze(0)
        with torch.no_grad():
            emb = model.encode_image(tensor)
        return emb.squeeze(0).tolist()

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """L2-normalized cosine similarity, clamped to [0, 1] (CLIP cosines can
        go slightly negative; anything <= 0 is maximal drift anyway)."""
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        cos = sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
        return max(0.0, min(1.0, cos))
