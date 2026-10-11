"""LLM access behind a tiny interface, so the provider can be swapped.

Failure behaviour (documented in the README):
  - no API key              -> LLMNotConfiguredError immediately, no network call
  - network error, 429, 5xx -> retried (default 2 retries, exponential backoff),
                               then LLMCallError
  - other API errors (401, 403, 400, 404...) -> LLMCallError immediately
  - empty / blocked reply   -> LLMCallError
"""
import os
import time
from typing import Optional, Protocol

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "gemini-3.8-flash"
RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMNotConfiguredError(LLMError):
    """No API key (or SDK) available. Learning cannot happen."""


class LLMCallError(LLMError):
    """The call failed (after retries where retrying makes sense)."""


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str) -> str: ...


class GeminiClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 retries: int = 4, backoff: float = 2.0):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.retries = retries
        self.backoff = backoff
        self._client = None

    def _get_client(self):
        if not self.api_key:
            raise LLMNotConfiguredError(
                "GEMINI_API_KEY is not set. Add it to .env to learn new formats.")
        if self._client is None:
            try:
                from google import genai
            except ImportError as e:
                raise LLMNotConfiguredError(
                    "The google-genai package is not installed "
                    "(pip install -r requirements.txt)") from e
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def complete(self, system: str, prompt: str) -> str:
        client = self._get_client()
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
        )
        for attempt in range(self.retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model, contents=prompt, config=config)
            except Exception as e:  # SDK and network errors have no common base class
                code = getattr(e, "code", None)
                retryable = code is None or code in RETRYABLE_CODES
                if not retryable or attempt == self.retries:
                    raise LLMCallError(
                        f"Gemini call failed ({code or type(e).__name__}): "
                        f"{str(e)[:300]}") from e
                time.sleep(self.backoff * (2 ** attempt))
                continue
            text = getattr(response, "text", None)
            if not text:
                raise LLMCallError("Gemini returned an empty reply (possibly blocked)")
            return text
        raise LLMCallError("unreachable")  # loop always returns or raises