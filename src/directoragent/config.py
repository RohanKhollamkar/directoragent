"""Application settings (STEP 1).

A single pydantic-settings model that reads from the process environment and
an optional .env file. There is deliberately NO module-level `settings`
global: cli.py constructs one Settings() and threads it into pipeline.run(),
which hands the phases exactly what they need. Phases never import settings.

Provider API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY) are not
fields here — the vision SDKs read them straight from the environment. They
may still live in .env; `extra="ignore"` keeps them from breaking Settings().
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Vision phase
    vision_provider: Literal["openai", "anthropic", "gemini", "mock"] = "anthropic"
    vision_model: str | None = None  # blank -> provider default (see make_provider)

    # Higgsfield client.
    # Two transports behind the adapter's call_tool seam:
    #  - agent-mediated (in-session Claude connector) — not constructible here.
    #  - REST (deployable): needs key_id + key_secret; auth is
    #    `Authorization: Key KEY_ID:KEY_SECRET` against higgsfield_base_url.
    higgsfield_api_key: str | None = None  # legacy/agent-mediated; dead over REST
    higgsfield_key_id: str = ""
    higgsfield_key_secret: str = ""
    higgsfield_base_url: str = "https://platform.higgsfield.ai"

    # Run mode / safety rails
    mock_mode: bool = False
    max_cost_usd: float = 10.0

    # State store
    state_db_path: str = ".directoragent/state.db"

    # Logging
    log_level: str = "INFO"
