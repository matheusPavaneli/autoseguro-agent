from __future__ import annotations

import pytest

from agent.llm.factory import PRESETS, ConfigError, build_provider


@pytest.mark.parametrize("name", ["groq", "ollama", "openai"])
def test_each_provider_resolves_url_and_default_model(name):
    p = build_provider({"LLM_PROVIDER": name, "LLM_API_KEY": "x"})
    assert p.base_url == PRESETS[name].base_url and p.model == PRESETS[name].model


def test_overrides_and_ollama_without_key():
    p = build_provider({"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3.1:8b", "LLM_BASE_URL": "http://gpu:11434/v1"})
    assert p.model == "llama3.1:8b" and p.base_url == "http://gpu:11434/v1"


def test_missing_key_and_unknown_provider_fail_clearly():
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        build_provider({"LLM_PROVIDER": "groq"})
    with pytest.raises(ConfigError, match="desconhecido"):
        build_provider({"LLM_PROVIDER": "nope"})


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "Wed, 21 Oct 2026 07:28:00 GMT", "999"])
def test_retry_after_is_clamped_and_never_crashes(value):
    """Regressao do code-review: Retry-After -1/nan levava ValueError ao time.sleep."""
    import httpx

    from agent.llm.openai_compat import _retry_wait
    assert 0.0 <= _retry_wait(httpx.Response(429, headers={"retry-after": value})) <= 5.0


def test_groq_tool_use_failed_400_is_retried_once():
    import httpx

    from agent.llm.base import ChatMessage
    from agent.llm.openai_compat import OpenAICompatProvider
    replies = [httpx.Response(400, json={"error": {"code": "tool_use_failed"}}),
               httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "oi"}}]})]
    p = OpenAICompatProvider(base_url="https://x.test/v1", api_key="k", model="m",
                             transport=httpx.MockTransport(lambda _r: replies.pop(0)), sleep=lambda _s: None)
    assert p.complete([ChatMessage("user", "oi")], []).text == "oi"
