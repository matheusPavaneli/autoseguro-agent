"""Adapter para qualquer API compativel com OpenAI Chat Completions.

Cobre Groq, Ollama (self-hosted), OpenAI, OpenRouter, vLLM: so mudam base_url,
api_key e model. E o unico arquivo que conhece esse formato.

Falha: 429, 5xx e timeout repetem UMA vez (respeitando Retry-After ate 5 s), porque o
free tier devolve 429 isolado com frequencia; o resto vira LLMError e o agente faz
handoff. Mensagem de erro nunca carrega a key (alguns provedores a ecoam no 401).
"""
from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from .base import ChatMessage, LLMError, LLMReply, ToolCall, ToolSpec

_KEY_LIKE = re.compile(r"\b(gsk_|sk-)[A-Za-z0-9_-]{8,}")
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_WAIT_S = 5.0


class OpenAICompatProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_s: float = 30.0,
                 temperature: float = 0.2, transport: httpx.BaseTransport | None = None,
                 retries: int = 1, sleep: Callable[[float], None] = time.sleep) -> None:
        self.name = f"openai-compat:{model}"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self._temperature = temperature
        self._retries = retries
        self._sleep = sleep
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout_s, transport=transport,
                                  headers={"Authorization": f"Bearer {api_key}"})

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> LLMReply:
        body: dict[str, Any] = {"model": self.model, "temperature": self._temperature,
                                "messages": [_to_wire(m) for m in messages]}
        if tools:
            body["tools"] = [_tool_to_wire(t) for t in tools]
        r = self._post_with_retry(body)
        try:
            msg = r.json()["choices"][0]["message"]
            calls = tuple(_parse_call(tc) for tc in msg.get("tool_calls") or [])
            text = msg.get("content")
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            raise LLMError(f"resposta fora do formato esperado: {type(e).__name__}") from e
        if text is not None and not isinstance(text, str):
            raise LLMError("content nao e texto")
        return LLMReply(text=text, tool_calls=calls)

    def _post_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                r = self._http.post("/chat/completions", json=body)
            except httpx.TimeoutException as e:
                if last:
                    raise LLMError(f"timeout: {self._redact(str(e))}") from e
                self._sleep(1.0)
                continue
            except httpx.HTTPError as e:  # inclui DecodingError e erros de protocolo
                raise LLMError(f"{type(e).__name__}: {self._redact(str(e))}") from e
            if r.status_code == 200:
                return r
            # 400 tool_use_failed (Groq): o modelo gerou chamada de ferramenta quebrada; repetir costuma resolver
            if (r.status_code in _RETRY_STATUS or (r.status_code == 400 and "tool_use_failed" in r.text)) and not last:
                self._sleep(_retry_wait(r))
                continue
            raise LLMError(f"HTTP {r.status_code}: {self._redact(r.text[:300])}")
        raise LLMError("sem resposta")  # inalcancavel: o loop sempre retorna ou levanta

    def _redact(self, text: str) -> str:
        if self._key and self._key != "not-needed":
            text = text.replace(self._key, "[KEY]")
        return _KEY_LIKE.sub("[KEY]", text)


def _retry_wait(r: httpx.Response) -> float:
    """Retry-After do provedor, limitado a [0, 5] s; valor negativo, nan ou data HTTP vira 2 s."""
    try:
        v = float(r.headers.get("retry-after", "2"))
    except ValueError:
        return 2.0
    return min(max(v, 0.0), _MAX_RETRY_WAIT_S) if math.isfinite(v) else 2.0


def _parse_call(tc: dict[str, Any]) -> ToolCall:
    args = json.loads(tc["function"].get("arguments") or "{}")
    if not isinstance(args, dict):
        raise ValueError("arguments da ferramenta nao e um objeto JSON")
    return ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args)


def _tool_to_wire(t: ToolSpec) -> dict[str, Any]:
    return {"type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}


def _to_wire(m: ChatMessage) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""}
    out: dict[str, Any] = {"role": m.role, "content": m.content or ""}
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
            for c in m.tool_calls
        ]
    return out
