"""Phase 1 — vision extraction (STEP 5).

VisionExtractor wraps any VisionProvider (the image+prompt -> text transport)
and turns its raw output into a validated SceneModel. The provider does the
model call; this class owns the structured-extraction contract: prompt,
fence-stripping, JSON parse, schema validation, and a single repair round.

The extraction prompt is generated from the Pydantic schema of the fields we
ask for, so prompt and model can never drift apart.
"""

import json

from pydantic import BaseModel, Field, ValidationError

from directoragent.schema import SceneModel
from directoragent.vision_providers import VisionProvider


class VisionExtractionError(RuntimeError):
    """Raised when extraction fails even after the one repair attempt."""


# Every SceneModel field except source_photo_path (which we already know — it's
# the input path, not something the model should invent). Kept as a comment, not
# a docstring, so it never leaks into model_json_schema()'s description and into
# the prompt we send the model.
class _SceneExtraction(BaseModel):
    subject: str
    environment: str
    lighting: str
    mood: str
    objects: list[str] = Field(default_factory=list)
    color_palette: list[str] = Field(default_factory=list)


def _build_prompt() -> str:
    schema = json.dumps(_SceneExtraction.model_json_schema(), indent=2)
    return (
        "You are a film visual analyst. Study the photograph and extract a "
        "structured scene model describing what a director would need to "
        "re-stage it.\n\n"
        "Respond with ONLY a single JSON object conforming to this JSON "
        "Schema. No prose, no explanation, no markdown code fences:\n\n"
        f"{schema}"
    )


_EXTRACTION_PROMPT = _build_prompt()


def _strip_fences(text: str) -> str:
    """Remove a leading ```json / ``` fence and its closing ``` if present."""
    s = text.strip()
    if s.startswith("```"):
        newline = s.find("\n")
        s = s[newline + 1:] if newline != -1 else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _repair_prompt(raw: str, error: Exception) -> str:
    return (
        "Your previous response could not be parsed into the required JSON "
        "object.\n\n"
        f"Previous response:\n{raw}\n\n"
        f"Error:\n{error}\n\n"
        "Reply with ONLY the corrected JSON object matching the schema. "
        "No prose, no markdown code fences."
    )


class VisionExtractor:
    """Implements the VisionClient protocol over a VisionProvider transport."""

    def __init__(self, provider: VisionProvider):
        self._provider = provider

    async def extract_scene(self, photo_path: str) -> SceneModel:
        raw = await self._provider.complete(photo_path, _EXTRACTION_PROMPT)
        try:
            extracted = self._parse(raw)
        except (json.JSONDecodeError, ValidationError) as err:
            # One repair round: hand the model its own output and the error.
            raw = await self._provider.complete(photo_path, _repair_prompt(raw, err))
            try:
                extracted = self._parse(raw)
            except (json.JSONDecodeError, ValidationError) as err2:
                raise VisionExtractionError(
                    "Vision extraction failed after one repair attempt: "
                    f"{err2}\n--- raw response ---\n{raw}"
                ) from err2
        return SceneModel(source_photo_path=photo_path, **extracted.model_dump())

    @staticmethod
    def _parse(raw: str) -> _SceneExtraction:
        data = json.loads(_strip_fences(raw))
        return _SceneExtraction.model_validate(data)
