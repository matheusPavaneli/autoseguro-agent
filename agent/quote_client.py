"""Cliente da POST /quote com tratamento explicito de falha.

Classificacao (a decisao mais importante deste arquivo):
  200                                   -> QuoteOk
  422 com error == "cotacao_recusada"   -> QuoteRefused  (regra de negocio: resposta final, nao repete)
  408, 425, 429                         -> transitorio (gateway temporariamente indisponivel)
  qualquer outro 4xx                    -> QuoteInvalid  (payload nosso esta errado: repetir nao ajuda;
                                           inclui o 422 de validacao do proprio FastAPI)
  5xx / timeout / erro de conexao / 200 com corpo quebrado -> transitorio: repete com backoff + jitter;
                                           esgotou as tentativas -> QuoteUnavailable

Timeouts por tentativa: (3 s, 3 s, 9 s). As duas primeiras cortam a lentidao simulada
(8 s) rapido e tentam de novo; a ultima espera o suficiente para aproveitar uma resposta
lenta. Com 20% de erro + 10% de lentidao: p(indisponivel) = 0,30 * 0,30 * 0,20 = 1,8%.
A /quote e um calculo puro (sem efeito colateral), entao repetir e seguro.
"""
from __future__ import annotations

import math
import random
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .trace import Tracer

DEFAULT_TIMEOUTS_S: tuple[float, ...] = (3.0, 3.0, 9.0)


@dataclass(frozen=True)
class QuoteOk:
    quote_id: str
    data: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class QuoteRefused:
    quote_id: str
    motivo: str
    attempts: int


@dataclass(frozen=True)
class QuoteInvalid:
    quote_id: str
    detalhe: str
    attempts: int


@dataclass(frozen=True)
class QuoteUnavailable:
    quote_id: str
    last_error: str
    attempts: int


QuoteResult = QuoteOk | QuoteRefused | QuoteInvalid | QuoteUnavailable


class QuoteGateway(Protocol):
    def quote(self, payload: dict[str, Any], *, conversation_id: str) -> QuoteResult: ...

    def plans(self) -> dict[str, Any]: ...


class QuoteClient:
    def __init__(self, base_url: str, tracer: Tracer, *, timeouts_s: Sequence[float] = DEFAULT_TIMEOUTS_S,
                 backoff_base_s: float = 0.5, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None) -> None:
        if not timeouts_s:
            raise ValueError("timeouts_s precisa de ao menos uma tentativa")
        self._http = httpx.Client(base_url=base_url, transport=transport)
        self._tracer = tracer
        self._timeouts = tuple(timeouts_s)
        self._backoff = backoff_base_s
        self._sleep = sleep
        self._rng = rng or random.Random()

    def plans(self) -> dict[str, Any]:
        """GET /planos e estavel no mock; se falhar, o agente nao sobe (falha cedo e clara)."""
        r = self._http.get("/planos", timeout=5.0)
        r.raise_for_status()
        return r.json()

    def quote(self, payload: dict[str, Any], *, conversation_id: str) -> QuoteResult:
        quote_id = f"q_{uuid.uuid4().hex[:12]}"
        last_error = ""
        for attempt, timeout in enumerate(self._timeouts, start=1):
            t0 = time.perf_counter()
            r: httpx.Response | None = None
            try:
                r = self._http.post("/quote", json=payload, timeout=timeout)
                status: int | str = r.status_code
            except httpx.TimeoutException:
                status = "timeout"
            except httpx.RequestError as e:  # conexao, protocolo, corpo comprimido quebrado (DecodingError)
                status = f"conn_error:{type(e).__name__}"
            self._tracer.emit("quote_attempt", conversation_id=conversation_id, quote_id=quote_id,
                              attempt=attempt, timeout_s=timeout, status=status,
                              latency_ms=round((time.perf_counter() - t0) * 1000))

            if r is not None and r.status_code == 200:
                data = _valid_quote(r)
                if data is not None:
                    return QuoteOk(quote_id, data, attempt)
                # 200 com corpo quebrado (pagina de gateway, campo faltando): legado instavel, repete
                status = "200_malformed"
                self._tracer.emit("quote_attempt_malformed", conversation_id=conversation_id, quote_id=quote_id,
                                  attempt=attempt, body=r.text[:200])
                r = None
            if r is not None and 400 <= r.status_code < 500 and r.status_code not in _TRANSIENT_4XX:
                body = _json(r)
                if r.status_code == 422 and body.get("error") == "cotacao_recusada":
                    return QuoteRefused(quote_id, str(body.get("motivo", "cotacao recusada")), attempt)
                return QuoteInvalid(quote_id, f"HTTP {r.status_code}: {body}", attempt)

            last_error = str(status)
            if attempt < len(self._timeouts):
                self._sleep(self._backoff * 2 ** (attempt - 1) + self._rng.uniform(0, self._backoff / 2))
        return QuoteUnavailable(quote_id, last_error, len(self._timeouts))


# 408/425/429 sao condicao temporaria do gateway (timeout, cedo demais, limite), nao payload errado
_TRANSIENT_4XX = {408, 425, 429}
_REQUIRED = {"plano_nome": str, "premio_mensal": (int, float), "franquia": (int, float), "coberturas": list}


def _valid_quote(r: httpx.Response) -> dict[str, Any] | None:
    """Corpo de 200 so vale se tiver os campos que o agente usa para montar a cotacao."""
    body = _json(r)
    if not all(isinstance(body.get(k), t) for k, t in _REQUIRED.items()):
        return None
    # valor que vira preco na tela: numero de verdade (nao bool, nao NaN/Infinity), maior que zero
    # premio > 0; franquia pode ser 0 (plano sem franquia e valido)
    if not (_money_ok(body["premio_mensal"]) and body["premio_mensal"] > 0 and _money_ok(body["franquia"])):
        return None
    if not all(isinstance(c, str) for c in body["coberturas"]):
        return None
    car = body.get("carencia")
    if car is not None and not (isinstance(car, dict) and isinstance(car.get("dias"), int)
                                and isinstance(car.get("coberturas", []), list)
                                and all(isinstance(c, str) for c in car.get("coberturas", []))):
        return None
    pr = body.get("primeiro_pagamento_pro_rata")
    if pr is not None and not (isinstance(pr, dict) and all(
            isinstance(pr.get(k), (int, float)) for k in ("dias_cobrados", "dias_no_mes", "valor_primeiro_pagamento"))
            and _money_ok(pr["valor_primeiro_pagamento"])):
        return None
    return body


def _money_ok(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {"body": r.text[:200]}
    return body if isinstance(body, dict) else {"body": body}
