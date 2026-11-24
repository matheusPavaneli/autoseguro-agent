"""Fakes compartilhados: nenhum teste usa rede nem LLM real.

FakeProvider cumpre o contrato LLMProvider; o fato de o agente rodar com ele e a
prova de que o agente depende so da abstracao.
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.core import Agent
from agent.llm.base import ChatMessage, LLMError, LLMReply, ToolCall, ToolSpec
from agent.quote_client import QuoteClient
from agent.trace import JsonlTracer

TODAY = dt.date(2026, 11, 23)
PLANS = json.loads((Path(__file__).resolve().parent.parent / "quote-service/data/plans.json").read_text("utf-8"))

QUOTE_OK = {
    "plano_id": "completo", "plano_nome": "Completo", "premio_mensal": 272.87, "franquia": 3000,
    "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
    "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.3},
    "carencia": {"coberturas": ["roubo", "furto"], "dias": 30, "observacao": "..."},
    "moeda": "BRL",
    "primeiro_pagamento_pro_rata": {"dias_no_mes": 31, "dias_cobrados": 17, "valor_primeiro_pagamento": 149.64},
}


class FakeProvider:
    name = "fake"

    def __init__(self, replies: Sequence[LLMReply | Exception]) -> None:
        self._replies = list(replies)
        self.seen: list[list[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> LLMReply:
        self.seen.append(list(messages))
        if not self._replies:
            raise AssertionError("FakeProvider chamado mais vezes que o roteiro")
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def say(text: str) -> LLMReply:
    return LLMReply(text=text)


def call(name: str, **args: Any) -> LLMReply:
    return LLMReply(text=None, tool_calls=(ToolCall(id=f"t_{name}", name=name, arguments=args),))


COTAR = dict(plano_id="completo", idade=35, veiculo_ano=2022, data_inicio="2026-12-15")
LLM_DOWN = LLMError("HTTP 503: down")

Responder = Callable[[httpx.Request], httpx.Response | Exception]


def scripted_transport(responses: Sequence[httpx.Response | Exception]) -> tuple[httpx.MockTransport, list]:
    """Transport que devolve as respostas em ordem; registra os requests de /quote."""
    queue = list(responses)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planos":
            return httpx.Response(200, json=PLANS)
        calls.append(request)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return httpx.MockTransport(handler), calls


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.jsonl"


@pytest.fixture
def make_agent(log_path: Path):
    def _make(replies: Sequence[LLMReply | Exception], quote_responses: Sequence[httpx.Response | Exception] = ()):
        tracer = JsonlTracer(log_path)
        transport, calls = scripted_transport(quote_responses)
        client = QuoteClient("http://quote", tracer, transport=transport, sleep=lambda _s: None)
        llm = FakeProvider(replies)
        agent = Agent(llm, client, tracer, today=lambda: TODAY)
        return agent, llm, calls

    return _make


def events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
