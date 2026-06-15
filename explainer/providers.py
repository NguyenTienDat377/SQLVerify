"""
explainer/providers.py

Multi-LLM provider abstraction for generating human-readable explanations.
Swap between Claude, Gemini, and GPT by changing EXPLAINER_PROVIDER env var.

Supported providers:
    claude  → Anthropic Claude (default)
    gemini  → Google Gemini
    openai  → OpenAI GPT

Usage:
    from explainer.providers import get_provider
    provider = get_provider()
    explanation = await provider.explain(prompt)
"""

from __future__ import annotations

import os
import json
from abc import ABC, abstractmethod

import httpx


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    @abstractmethod
    async def explain(self, prompt: str) -> str:
        """
        Send prompt to the LLM and return a plain-text explanation.
        Raises ExplainerError on failure.
        """
        ...


class ExplainerError(Exception):
    """Raised when an LLM provider call fails."""
    pass


# ---------------------------------------------------------------------------
# Claude (Anthropic)
# ---------------------------------------------------------------------------

class ClaudeProvider(LLMProvider):
    """
    Calls the Anthropic Messages API.
    Requires ANTHROPIC_API_KEY in environment.
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    MODEL   = "claude-haiku-4-5-20251001"

    async def explain(self, prompt: str) -> str:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ExplainerError("ANTHROPIC_API_KEY is not set.")

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.MODEL,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.API_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            # Connection refused / timeout / DNS — i.e. the provider is down.
            # Surface as ExplainerError so the caller degrades gracefully and
            # the circuit breaker records a failure instead of the request
            # crashing with an unhandled httpx error.
            raise ExplainerError(f"Claude API request failed: {e}") from e

        if resp.status_code != 200:
            raise ExplainerError(
                f"Claude API error {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        return data["content"][0]["text"].strip()


# ---------------------------------------------------------------------------
# Gemini (Google)
# ---------------------------------------------------------------------------

class GeminiProvider(LLMProvider):
    """
    Calls the Google Gemini generateContent API.
    Requires GEMINI_API_KEY in environment.
    """

    MODEL = "gemini-2.0-flash"

    async def explain(self, prompt: str) -> str:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ExplainerError("GEMINI_API_KEY is not set.")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.MODEL}:generateContent?key={api_key}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 512},
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=body)
        except httpx.HTTPError as e:
            raise ExplainerError(f"Gemini API request failed: {e}") from e

        if resp.status_code != 200:
            raise ExplainerError(
                f"Gemini API error {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


# ---------------------------------------------------------------------------
# OpenAI GPT
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    """
    Calls the OpenAI Chat Completions API.
    Requires OPENAI_API_KEY in environment.
    """

    API_URL = "https://api.openai.com/v1/chat/completions"
    MODEL   = "gpt-4o-mini"

    async def explain(self, prompt: str) -> str:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ExplainerError("OPENAI_API_KEY is not set.")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.MODEL,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.API_URL, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise ExplainerError(f"OpenAI API request failed: {e}") from e

        if resp.status_code != 200:
            raise ExplainerError(
                f"OpenAI API error {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
}

def get_provider(provider_name: str | None = None) -> LLMProvider:
    """
    Return an LLMProvider instance based on EXPLAINER_PROVIDER env var.
    Defaults to Claude if not set.

    Args:
        provider_name: Override the env var (useful for testing).

    Returns:
        An LLMProvider instance ready to call.

    Raises:
        ExplainerError: If the provider name is unknown.
    """
    name = (provider_name or os.getenv("EXPLAINER_PROVIDER", "claude")).lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ExplainerError(
            f"Unknown provider '{name}'. Choose from: {list(_PROVIDERS.keys())}"
        )
    return cls()