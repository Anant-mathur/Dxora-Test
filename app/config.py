"""Settings, read from environment variables (and an optional .env file). No secrets in code."""
import os
from dataclasses import dataclass, field
from typing import Mapping

from dotenv import load_dotenv

DEFAULT_PROVIDER = "gemini"
DEFAULT_MODELS = {
    "gemini": "gemini-3.1-flash-lite",
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-6.1-terra",
}
KEY_NAMES = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@dataclass
class Settings:
    env: Mapping[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return (self.env.get(key) or default).strip()

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get(key) or default)
        except ValueError:
            return default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key) or default)
        except ValueError:
            return default

    # LLM
    @property
    def provider(self) -> str:
        return self.get("LLM_PROVIDER", DEFAULT_PROVIDER).lower()

    @property
    def model(self) -> str:
        return self.get("LLM_MODEL", DEFAULT_MODELS.get(self.provider, ""))

    @property
    def fallback_provider(self) -> str:
        return self.get("LLM_FALLBACK_PROVIDER").lower()

    @property
    def fallback_model(self) -> str:
        return self.get("LLM_FALLBACK_MODEL", DEFAULT_MODELS.get(self.fallback_provider, ""))

    def api_key(self, provider: str) -> str:
        return self.get(KEY_NAMES.get(provider, ""))

    # limits
    @property
    def llm_timeout(self) -> float:
        return self.get_float("LLM_TIMEOUT_SECONDS", 60.0)

    @property
    def fetch_timeout(self) -> float:
        return self.get_float("FETCH_TIMEOUT_SECONDS", 15.0)

    @property
    def max_content_chars(self) -> int:
        return self.get_int("MAX_CONTENT_CHARS", 14000)

    @property
    def max_response_bytes(self) -> int:
        return self.get_int("MAX_RESPONSE_BYTES", 2_000_000)

    @property
    def cache_ttl_seconds(self) -> int:
        return self.get_int("CACHE_TTL_SECONDS", 3600)

    @property
    def rate_limit_per_minute(self) -> int:
        return self.get_int("RATE_LIMIT_PER_MINUTE", 10)

    @property
    def trust_proxy(self) -> bool:
        return self.get("TRUST_PROXY").lower() in ("1", "true", "yes")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(env=dict(os.environ))
