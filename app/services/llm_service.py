"""Thin, provider-agnostic LLM client (Gemini, Anthropic, OpenAI) over plain HTTPS.

Returns raw JSON text plus token usage. Retries 429/5xx with backoff, and can fall back to a
second provider/model. Secrets never appear in logs or error messages.
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.errors import LLMError, LLMNotConfiguredError

logger = logging.getLogger("llm")

# USD per 1M tokens (input, output), as of 2026-11-24. The OpenAI and 3.5 Flash-Lite rows are
# unverified. Override with LLM_INPUT_PRICE_PER_M / LLM_OUTPUT_PRICE_PER_M.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "gpt-6.1-terra": (0.50, 2.50),
}

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    latency_ms: int


def estimate_cost(model: str, input_tokens: int, output_tokens: int, settings: Settings) -> float | None:
    in_price = settings.get("LLM_INPUT_PRICE_PER_M")
    out_price = settings.get("LLM_OUTPUT_PRICE_PER_M")
    if in_price and out_price:
        prices = (float(in_price), float(out_price))
    else:
        prices = MODEL_PRICING.get(model)
    if not prices:
        return None
    return round(input_tokens * prices[0] / 1e6 + output_tokens * prices[1] / 1e6, 6)


class LLMService:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    # public
    async def generate_json(self, system: str, user: str, max_output_tokens: int = 6000) -> LLMResult:
        chain = [(self.settings.provider, self.settings.model)]
        if self.settings.fallback_provider:
            chain.append((self.settings.fallback_provider, self.settings.fallback_model))
        last: Exception | None = None
        # cap the primary attempt (retries included) so a slow provider hands over to the fallback
        budget = self.settings.get_float("LLM_PRIMARY_BUDGET_SECONDS", 30.0)
        for i, (provider, model) in enumerate(chain):
            if not self.settings.api_key(provider):
                last = last or LLMNotConfiguredError(
                    f"No API key configured for provider '{provider}'. Set {provider.upper()}_API_KEY."
                )
                continue
            has_next = i < len(chain) - 1
            try:
                call = self._call(provider, model, system, user, max_output_tokens)
                if has_next and budget > 0:
                    try:
                        return await asyncio.wait_for(call, timeout=budget)
                    except asyncio.TimeoutError:
                        raise LLMError(f"{provider} exceeded the {budget:.0f}s budget.")
                return await call
            except LLMError as exc:
                logger.warning("LLM %s/%s failed: %s", provider, model, exc.message)
                last = exc
        raise last or LLMNotConfiguredError("No LLM provider is configured.")

    # internals
    async def _call(self, provider: str, model: str, system: str, user: str, max_tokens: int) -> LLMResult:
        builders = {"gemini": self._gemini, "anthropic": self._anthropic, "openai": self._openai}
        if provider not in builders:
            raise LLMNotConfiguredError(f"Unknown LLM provider '{provider}'. Use gemini, anthropic or openai.")
        url, headers, body, parse = builders[provider](model, system, user, max_tokens)
        started = time.perf_counter()
        data = await self._post(url, headers, body, provider)
        text, tin, tout = parse(data)
        if not text:
            raise LLMError(f"{provider} returned an empty response.")
        return LLMResult(text, tin, tout, provider, model, int((time.perf_counter() - started) * 1000))

    async def _post(self, url: str, headers: dict, body: dict, provider: str) -> dict:
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout, transport=self.transport) as client:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    resp = await client.post(url, headers=headers, json=body)
                except httpx.TimeoutException:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise LLMError(f"{provider} request timed out.")
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                except httpx.HTTPError as exc:
                    raise LLMError(f"Could not reach {provider} ({type(exc).__name__}).")
                if resp.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if resp.status_code in (401, 403):
                    raise LLMError(f"{provider} rejected the API key (HTTP {resp.status_code}).")
                if resp.status_code >= 400:
                    raise LLMError(f"{provider} returned HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json()
        raise LLMError(f"{provider} request failed.")

    def _gemini(self, model, system, user, max_tokens):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.settings.api_key("gemini")}  # header, not URL, so it never lands in logs
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2, "maxOutputTokens": max_tokens},
        }

        def parse(d):
            cands = d.get("candidates") or []
            parts = (cands[0].get("content", {}).get("parts", []) if cands else [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            u = d.get("usageMetadata", {})
            return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)

        return url, headers, body, parse

    def _anthropic(self, model, system, user, max_tokens):
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": self.settings.api_key("anthropic"), "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                # thinking tokens count against max_tokens and truncated the JSON
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": user}]}

        def parse(d):
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            u = d.get("usage", {})
            return text, u.get("input_tokens", 0), u.get("output_tokens", 0)

        return url, headers, body, parse

    def _openai(self, model, system, user, max_tokens):
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.api_key('openai')}"}
        body = {"model": model, "max_completion_tokens": max_tokens, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}

        def parse(d):
            choices = d.get("choices") or []
            text = (choices[0].get("message", {}).get("content") or "") if choices else ""
            u = d.get("usage", {})
            return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

        return url, headers, body, parse
