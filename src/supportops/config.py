"""Environment-backed application configuration."""

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    demo_mode: bool = True
    db_path: Path = Path("data/supportops.sqlite3")
    approval_threshold: float = 50.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 0.05
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            demo_mode=_bool_env("DEMO_MODE", True),
            db_path=Path(os.getenv("SUPPORTOPS_DB_PATH", "data/supportops.sqlite3")),
            approval_threshold=float(os.getenv("APPROVAL_THRESHOLD", "50")),
            max_retries=int(os.getenv("MAX_RETRIES", "2")),
            retry_base_delay_seconds=float(os.getenv("RETRY_BASE_DELAY_SECONDS", "0.05")),
            llm_api_key=os.getenv("LLM_API_KEY") or None,
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "20")),
        )
        if settings.approval_threshold < 0:
            raise ValueError("APPROVAL_THRESHOLD must be non-negative.")
        if settings.max_retries < 0 or settings.max_retries > 10:
            raise ValueError("MAX_RETRIES must be between 0 and 10.")
        if settings.retry_base_delay_seconds < 0:
            raise ValueError("RETRY_BASE_DELAY_SECONDS must be non-negative.")
        if not settings.demo_mode and not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required when DEMO_MODE=false.")
        return settings


def get_settings() -> Settings:
    return Settings.from_env()