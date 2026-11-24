"""Unico ponto que decide QUAL provedor usar, a partir do ambiente.

LLM_PROVIDER=groq   (default) -> https://api.groq.com/openai/v1   (precisa LLM_API_KEY)
LLM_PROVIDER=ollama           -> http://localhost:11434/v1        (self-hosted, sem key)
LLM_PROVIDER=openai           -> https://api.openai.com/v1        (precisa LLM_API_KEY)
LLM_BASE_URL e LLM_MODEL sobrescrevem o default de qualquer um.

Provedor com API propria (ex.: Anthropic nativo): escrever um adapter que cumpra
LLMProvider e registrar aqui. O agente nao muda.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .base import LLMProvider
from .openai_compat import OpenAICompatProvider


@dataclass(frozen=True)
class _Preset:
    base_url: str
    model: str
    needs_key: bool


PRESETS: dict[str, _Preset] = {
    "groq": _Preset("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", True),
    "ollama": _Preset("http://localhost:11434/v1", "qwen2.5:7b-instruct", False),
    "openai": _Preset("https://api.openai.com/v1", "gpt-4o-mini", True),
}


class ConfigError(ValueError):
    pass


def build_provider(env: Mapping[str, str] | None = None) -> LLMProvider:
    env = os.environ if env is None else env
    name = env.get("LLM_PROVIDER", "groq").lower()
    preset = PRESETS.get(name)
    if preset is None:
        raise ConfigError(f"LLM_PROVIDER desconhecido: {name!r}. Opcoes: {', '.join(PRESETS)}")
    key = env.get("LLM_API_KEY", "")
    if preset.needs_key and not key:
        raise ConfigError(f"LLM_API_KEY nao definida (provedor {name}). Veja .env.example.")
    return OpenAICompatProvider(base_url=env.get("LLM_BASE_URL") or preset.base_url,
                                api_key=key or "not-needed",
                                model=env.get("LLM_MODEL") or preset.model)
