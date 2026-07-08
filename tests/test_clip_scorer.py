"""STEP 13 — ClipDriftScorer tests.

Real CLIP never loads in CI (too heavy; the default env is torch-free). We
monkeypatch _ensure_model + the image/frame helpers to feed controlled
embeddings, assert the cosine math, exercise the return-0.0-never-raise failure
paths, and check in a fresh process that importing the module does not import
torch at module level.
"""

import subprocess
import sys

import httpx
import pytest

from directoragent.drift.clip_scorer import ClipDriftScorer


def _controlled(monkeypatch, scorer: ClipDriftScorer, ref_emb, cand_emb):
    """Bypass model loading and image decoding; feed fixed embeddings."""
    monkeypatch.setattr(
        ClipDriftScorer, "_ensure_model", lambda self: (None, None, None)
    )
    monkeypatch.setattr(
        ClipDriftScorer, "_load_reference", staticmethod(lambda path: "REF_IMG")
    )
    monkeypatch.setattr(
        ClipDriftScorer, "_candidate_frame", staticmethod(lambda d, ct: "CAND_IMG")
    )
    embs = {"REF_IMG": ref_emb, "CAND_IMG": cand_emb}
    monkeypatch.setattr(ClipDriftScorer, "_embed", lambda self, img: embs[img])


def _local_candidate(monkeypatch, data: bytes, content_type: str = "video/mp4"):
    async def fake_fetch(self, url):
        return data, content_type

    monkeypatch.setattr(ClipDriftScorer, "_fetch_candidate", fake_fetch)


# --- (a) cosine math with controlled embeddings ------------------------------
async def test_identical_embeddings_score_one(monkeypatch):
    scorer = ClipDriftScorer()
    _controlled(monkeypatch, scorer, [1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    _local_candidate(monkeypatch, b"fake-bytes")
    assert await scorer.score("ref.png", "https://cdn/x.mp4") == pytest.approx(1.0)


async def test_orthogonal_embeddings_score_zero(monkeypatch):
    scorer = ClipDriftScorer()
    _controlled(monkeypatch, scorer, [1.0, 0.0], [0.0, 1.0])
    _local_candidate(monkeypatch, b"fake-bytes")
    assert await scorer.score("ref.png", "https://cdn/x.mp4") == pytest.approx(0.0)


async def test_opposite_embeddings_clamped_to_zero(monkeypatch):
    # CLIP cosines can go negative; the scorer clamps to [0, 1].
    scorer = ClipDriftScorer()
    _controlled(monkeypatch, scorer, [1.0, 0.0], [-1.0, 0.0])
    _local_candidate(monkeypatch, b"fake-bytes")
    assert await scorer.score("ref.png", "https://cdn/x.mp4") == 0.0


def test_cosine_scale_invariance():
    # Pure function: same direction, different magnitudes -> 1.0.
    assert ClipDriftScorer._cosine([2.0, 0.0], [8.0, 0.0]) == pytest.approx(1.0)
    assert ClipDriftScorer._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # zero vector


# --- (b) failure paths: return 0.0 + WARNING, never raise --------------------
async def test_undownloadable_candidate_returns_zero(monkeypatch, caplog):
    async def dead_get(self, url, **kw):
        raise httpx.ConnectError("dead host")

    monkeypatch.setattr(httpx.AsyncClient, "get", dead_get)
    scorer = ClipDriftScorer()
    with caplog.at_level("WARNING"):
        assert await scorer.score("ref.png", "https://dead/x.mp4") == 0.0
    assert any("clip score" in r.message for r in caplog.records)


async def test_zero_byte_candidate_returns_zero(monkeypatch, caplog):
    _local_candidate(monkeypatch, b"")
    scorer = ClipDriftScorer()
    with caplog.at_level("WARNING"):
        assert await scorer.score("ref.png", "https://cdn/empty.mp4") == 0.0
    assert any("empty candidate" in r.message for r in caplog.records)


async def test_undecodable_video_returns_zero(monkeypatch, caplog):
    # Garbage bytes reach _candidate_frame; whatever it raises (ImportError for
    # a missing av in the torch-free env, or a decode error with av installed)
    # must be swallowed into 0.0.
    _local_candidate(monkeypatch, b"not-a-video", "video/mp4")
    monkeypatch.setattr(
        ClipDriftScorer, "_ensure_model", lambda self: (None, None, None)
    )
    monkeypatch.setattr(
        ClipDriftScorer, "_load_reference", staticmethod(lambda path: "REF_IMG")
    )
    scorer = ClipDriftScorer()
    with caplog.at_level("WARNING"):
        assert await scorer.score("ref.png", "https://cdn/broken.mp4") == 0.0
    assert any("-> 0.0" in r.message for r in caplog.records)


async def test_missing_local_candidate_returns_zero(caplog):
    scorer = ClipDriftScorer()
    with caplog.at_level("WARNING"):
        assert await scorer.score("ref.png", "/no/such/file.mp4") == 0.0


# --- (c) lazy loading: module import must not pull torch ---------------------
def test_module_import_does_not_load_torch():
    code = (
        "import directoragent.drift.clip_scorer, sys; "
        "banned = {'torch', 'open_clip', 'av', 'PIL'}; "
        "loaded = banned & set(sys.modules); "
        "assert not loaded, f'lazily-required modules imported eagerly: {loaded}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
