"""Validated server configuration with non-serializable secret storage."""

from __future__ import annotations

from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class Settings:
    app_environment: str = "development"
    reka_api_key: str = field(default="", repr=False)
    reka_chat_base_url: str = "https://api.reka.ai/v1"
    reka_vision_base_url: str = "https://vision-agent.api.reka.ai"
    reka_model: str = "reka-flash"
    reka_prompt_version: str = "1.0.0"
    reka_timeout_seconds: float = 20.0
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)

    @classmethod
    def from_environment(cls) -> "Settings":
        try:
            from dotenv import load_dotenv
        except ImportError:
            pass
        else:
            load_dotenv()
        origins = tuple(
            item.strip()
            for item in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
            if item.strip()
        )
        settings = cls(
            app_environment=os.environ.get("APP_ENVIRONMENT", "development").strip().lower(),
            reka_api_key=os.environ.get("REKA_API_KEY", "").strip(),
            reka_chat_base_url=os.environ.get(
                "REKA_CHAT_BASE_URL",
                os.environ.get("REKA_BASE_URL", "https://api.reka.ai/v1"),
            ).strip(),
            reka_vision_base_url=os.environ.get(
                "REKA_VISION_BASE_URL", "https://vision-agent.api.reka.ai"
            ).strip(),
            reka_model=os.environ.get("REKA_MODEL", "reka-flash").strip(),
            reka_prompt_version=os.environ.get("REKA_PROMPT_VERSION", "1.0.0").strip(),
            reka_timeout_seconds=float(os.environ.get("REKA_TIMEOUT_SECONDS", "20")),
            cors_origins=origins,
        )
        if not 1 <= settings.reka_timeout_seconds <= 120:
            raise ValueError("REKA_TIMEOUT_SECONDS must be between 1 and 120")
        if not settings.reka_chat_base_url.startswith("https://"):
            raise ValueError("REKA_CHAT_BASE_URL must use HTTPS")
        if not settings.reka_vision_base_url.startswith("https://"):
            raise ValueError("REKA_VISION_BASE_URL must use HTTPS")
        if not settings.cors_origins:
            raise ValueError("At least one CORS origin is required")
        if settings.app_environment not in {"development", "test", "production"}:
            raise ValueError("APP_ENVIRONMENT must be development, test, or production")
        return settings

    @property
    def reka_configured(self) -> bool:
        return bool(self.reka_api_key)
