"""Vision provider transports.

A VisionProvider does ONE thing: take an image + a prompt, return raw model
text. That's the entire surface a user must implement to plug in their own
model. All the structured-extraction logic (prompting, parsing, validation,
repair) lives in vision.py and is shared across every provider.

NOTE: provider SDK call shapes drift between versions. These are adapters —
verify the request shape against your installed SDK version. The contract
they satisfy (image + prompt -> text) is stable; the SDK call inside is not.
"""

import base64
import json
import mimetypes
from pathlib import Path
from typing import Protocol

from directoragent.schema import SceneModel  # only used by the mock to emit a valid shape


class VisionProvider(Protocol):
    async def complete(self, image_path: str, prompt: str) -> str:
        """Send image + prompt to the model, return its raw text response."""
        ...


def _encode_image(image_path: str) -> tuple[str, str]:
    """Return (base64_data, media_type)."""
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
    media_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    return data, media_type


# --- OpenAI -----------------------------------------------------------------
class OpenAIVisionProvider:
    def __init__(self, model: str = "gpt-4o"):
        from openai import AsyncOpenAI  # imported lazily so unused SDKs aren't required
        self._client = AsyncOpenAI()
        self._model = model

    async def complete(self, image_path: str, prompt: str) -> str:
        data, media_type = _encode_image(image_path)
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media_type};base64,{data}"}},
                ],
            }],
        )
        return resp.choices[0].message.content or ""


# --- Anthropic --------------------------------------------------------------
class AnthropicVisionProvider:
    def __init__(self, model: str = "claude-sonnet-4-6"):
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic()
        self._model = model

    async def complete(self, image_path: str, prompt: str) -> str:
        data, media_type = _encode_image(image_path)
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


# --- Gemini -----------------------------------------------------------------
class GeminiVisionProvider:
    def __init__(self, model: str = "gemini-2.5-pro"):
        import google.generativeai as genai
        self._genai = genai
        self._model_name = model

    async def complete(self, image_path: str, prompt: str) -> str:
        data, media_type = _encode_image(image_path)
        # Gemini requires a Part dict, not raw bytes
        image_part = {"mime_type": media_type, "data": base64.b64decode(data)}
        model = self._genai.GenerativeModel(self._model_name)
        resp = await model.generate_content_async([image_part, prompt])
        return resp.text or ""


# --- Mock (no credentials, valid shape) -------------------------------------
class MockVisionProvider:
    """Returns deterministic valid JSON so the whole pipeline runs offline."""
    async def complete(self, image_path: str, prompt: str) -> str:
        return json.dumps({
            "subject": "a lone figure in a long coat",
            "environment": "rain-slicked neon city street at night",
            "lighting": "high-contrast neon, wet reflections, deep shadows",
            "mood": "moody, cinematic, noir",
            "objects": ["umbrella", "streetlight", "puddle", "distant car"],
            "color_palette": ["#0a0e1a", "#ff2e88", "#00e5ff", "#1a1f3a"],
        })


_PROVIDERS = {
    "openai": OpenAIVisionProvider,
    "anthropic": AnthropicVisionProvider,
    "gemini": GeminiVisionProvider,
    "mock": MockVisionProvider,
}


def make_provider(name: str, model: str | None = None) -> VisionProvider:
    """Factory: name -> provider. Add a provider by registering it here, or
    implement VisionProvider.complete and pass the instance directly."""
    name = name.lower()
    if name not in _PROVIDERS:
        raise ValueError(f"unknown vision provider {name!r}; have {list(_PROVIDERS)}")
    cls = _PROVIDERS[name]
    return cls(model) if model and name != "mock" else cls()
