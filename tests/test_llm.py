from types import SimpleNamespace

import pytest

from app.llm import GeminiClient, LLMCallError, LLMNotConfiguredError


class APIError(Exception):
    def __init__(self, code):
        super().__init__(f"error {code}")
        self.code = code


def client_with(behaviours, **kw):
    models = SimpleNamespace(calls=0)

    def generate_content(**_):
        models.calls += 1
        b = behaviours.pop(0)
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(text=b)

    models.generate_content = generate_content
    g = GeminiClient(api_key="test-key", backoff=0, **kw)
    g._client = SimpleNamespace(models=models)
    return g, models


def test_no_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError, match="GEMINI_API_KEY"):
        GeminiClient(api_key=None).complete("sys", "prompt")


def test_transient_errors_are_retried():
    g, models = client_with([APIError(503), APIError(429), "{}"])
    assert g.complete("s", "p") == "{}" and models.calls == 3


def test_gives_up_after_retries():
    g, models = client_with([APIError(503)] * 3, retries=2)
    with pytest.raises(LLMCallError):
        g.complete("s", "p")
    assert models.calls == 3


def test_auth_errors_are_not_retried():
    g, models = client_with([APIError(401), "{}"])
    with pytest.raises(LLMCallError):
        g.complete("s", "p")
    assert models.calls == 1


def test_empty_reply_is_an_error():
    g, _ = client_with([""])
    with pytest.raises(LLMCallError, match="empty"):
        g.complete("s", "p")