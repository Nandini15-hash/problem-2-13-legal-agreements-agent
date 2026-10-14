"""All configuration comes from environment variables, so one image runs anywhere."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(v: str | None, default: bool) -> bool:
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    database_url: str = "sqlite:///./lexflow.db"
    playbook_path: str | None = None
    mode: str = "auto"                  # auto | rules | hybrid
    api_key: str | None = None          # required on /api/* when set
    base_url: str = "http://localhost:8080"
    hooks: list[str] = field(default_factory=lambda: ["console"])  # console | file:<path> | webhook:<url> | slack:<url>
    hook_secret: str = "dev-secret-change-me"
    auto_send: bool = True              # auto-approved contracts go straight out for signature
    llm_cache_dir: str | None = ".lexflow-cache"
    llm_timeout: float = 120.0
    llm_max_calls: int = 500
    redact_pii: bool = False


def get_settings() -> Settings:
    e = os.environ
    return Settings(
        database_url=e.get("DATABASE_URL", Settings.database_url),
        playbook_path=e.get("LEXFLOW_PLAYBOOK") or None,
        mode=e.get("LEXFLOW_MODE", "auto"),
        api_key=e.get("LEXFLOW_API_KEY") or None,
        base_url=e.get("LEXFLOW_BASE_URL", Settings.base_url).rstrip("/"),
        hooks=[h.strip() for h in e.get("LEXFLOW_HOOKS", "console").split(",") if h.strip()],
        hook_secret=e.get("LEXFLOW_HOOK_SECRET", Settings.hook_secret),
        auto_send=_bool(e.get("LEXFLOW_AUTO_SEND"), True),
        llm_cache_dir=e.get("LEXFLOW_LLM_CACHE", Settings.llm_cache_dir) or None,
        llm_timeout=float(e.get("LEXFLOW_LLM_TIMEOUT", "120")),
        llm_max_calls=int(e.get("LEXFLOW_LLM_MAX_CALLS", "500")),
        redact_pii=_bool(e.get("LEXFLOW_REDACT_PII"), False),
    )
