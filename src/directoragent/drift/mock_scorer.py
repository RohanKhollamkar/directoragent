"""Mock drift scorer (STEP 4a).

Synthetic CLIP-style scores so --mock skips torch/open_clip entirely.
Implements the DriftScorer Protocol.

score() receives only (reference_path, candidate_url) — no render_class, no
threshold — so it can't know what "passing" means for a given shot. Instead
it's seeded to fail deterministically: the first `fail_first` calls return a
score below every threshold, and every call after returns a score above the
strictest threshold (FACE = 0.78). That lets a test drive the executor's
quality-retry loop a known number of times before a shot passes.

  fail_first=0 (default) -> every shot passes on the first attempt.
  fail_first=2           -> the first two scores fail, the third passes.
"""

import asyncio

_FAIL_SCORE = 0.50   # below every DRIFT_THRESHOLDS entry
_PASS_SCORE = 0.82   # above the strictest (FACE = 0.78)
_LATENCY_S = 0.01


class MockDriftScorer:
    def __init__(self, fail_first: int = 0) -> None:
        self._fail_first = fail_first
        self._calls = 0

    async def score(self, reference_path: str, candidate_url: str) -> float:
        await asyncio.sleep(_LATENCY_S)
        self._calls += 1
        if self._calls <= self._fail_first:
            return _FAIL_SCORE
        return _PASS_SCORE
