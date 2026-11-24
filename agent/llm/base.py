"""Contrato do LLM, independente de provedor (inversao de dependencia).

O agente (agent/core.py) so conhece estes tipos. Cada provedor e um adapter que
traduz de/para o formato dele; a escolha fica em agent/llm/factory.py. Trocar o
provedor nao muda nenhuma linha do agente.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None  # preenchido quando role == "tool"


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


class LLMError(RuntimeError):
    """Qualquer falha do provedor: rede, auth, limite de taxa, resposta invalida."""


class LLMProvider(Protocol):
    name: str

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> LLMReply:
        """Devolve texto e/ou chamadas de ferramenta. Levanta LLMError em falha."""
        ...
