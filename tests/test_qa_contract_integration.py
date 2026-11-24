"""QA: contrato e integracao contra o quote-service REAL (processo uvicorn proprio, porta 8001-8008)."""
from __future__ import annotations

import datetime as dt
import random
import time

import httpx
import pytest

from agent.core import Agent, InboundMessage, format_quote
from agent.handoff import HANDOFF_MESSAGES, HandoffReason
from agent.price_guard import has_money
from agent.prompts import COVERAGE_LABELS
from agent.quote_client import QuoteClient, QuoteOk, QuoteUnavailable
from agent.trace import JsonlTracer

from .conftest import FakeProvider, call, events, say
from .qa_support import quote_service

TODAY = dt.date.today()
START = (TODAY + dt.timedelta(days=22)).isoformat()


class RecordingGateway:
    """Embrulha o QuoteClient real e guarda os payloads que o AGENTE montou."""

    def __init__(self, inner: QuoteClient) -> None:
        self.inner, self.payloads = inner, []

    def plans(self):
        return self.inner.plans()

    def quote(self, payload, *, conversation_id):
        self.payloads.append(payload)
        return self.inner.quote(payload, conversation_id=conversation_id)


@pytest.fixture(scope="module")
def stable_url():
    with quote_service(failure_rate=0.0, slow_rate=0.0) as url:
        yield url


def _agent(url, log_path, replies, **client_kw):
    tracer = JsonlTracer(log_path)
    gw = RecordingGateway(QuoteClient(url, tracer, **client_kw))
    llm = FakeProvider(replies)
    return Agent(llm, gw, tracer, today=lambda: TODAY), llm, gw


def _ready(agent, cep="07310-100"):
    conv = agent.start()
    conv.cep = cep
    return conv


# ---------------- contrato ----------------

def test_agent_payload_matches_real_quoterequest_schema(stable_url, log_path):
    schema = httpx.get(f"{stable_url}/openapi.json").json()["components"]["schemas"]["QuoteRequest"]
    props, required = schema["properties"], set(schema.get("required", []))
    agent, _, gw = _agent(stable_url, log_path, [call("cotar", plano_id="Completo", idade=35, veiculo_ano=2022,
                                                      data_inicio=START)])
    agent.handle(_ready(agent), InboundMessage("cota"))
    payload = gw.payloads[0]
    assert set(payload) <= set(props) and required <= set(payload)
    assert isinstance(payload["idade"], int) and isinstance(payload["veiculo_ano"], int)
    assert payload["plano_id"] == "completo"  # o agente normaliza caixa
    assert props["idade"]["minimum"] == 0 and props["idade"]["maximum"] == 200
    assert props["veiculo_ano"]["minimum"] == 1950 and props["veiculo_ano"]["maximum"] == 2100
    dt.date.fromisoformat(payload["data_inicio"])


def test_real_quote_and_planos_shapes_match_what_the_agent_reads(stable_url):
    planos = httpx.get(f"{stable_url}/planos").json()
    for p in planos["planos"]:
        assert {"id", "nome", "coberturas"} <= p.keys()
        assert set(p["coberturas"]) <= set(COVERAGE_LABELS), "cobertura sem rotulo no agente"
    r = httpx.post(f"{stable_url}/quote", json={"plano_id": "premium", "idade": 40, "veiculo_ano": 2020,
                                                "cep": "01310-100", "data_inicio": START})
    d = r.json()
    assert r.status_code == 200
    assert {"plano_nome", "premio_mensal", "franquia", "coberturas", "carencia"} <= d.keys()
    assert {"coberturas", "dias"} <= d["carencia"].keys()
    if dt.date.fromisoformat(START).day != 1:
        assert {"dias_no_mes", "dias_cobrados", "valor_primeiro_pagamento"} <= d["primeiro_pagamento_pro_rata"].keys()
    format_quote(d)  # nao pode levantar com a resposta real
    refused = httpx.post(f"{stable_url}/quote", json={"plano_id": "completo", "idade": 80, "veiculo_ano": 2020})
    assert refused.status_code == 422 and refused.json()["error"] == "cotacao_recusada"
    invalid = httpx.post(f"{stable_url}/quote", json={"plano_id": "completo", "idade": 30, "veiculo_ano": 22})
    assert invalid.status_code == 422 and "error" not in invalid.json()  # 422 de validacao do FastAPI


# ---------------- integracao ----------------

def test_happy_path_against_real_api_quote_text_matches_api(stable_url, log_path):
    agent, _, _ = _agent(stable_url, log_path, [call("cotar", plano_id="completo", idade=35, veiculo_ano=2022,
                                                     data_inicio=START)])
    conv = _ready(agent)
    out = agent.handle(conv, InboundMessage("cota"))
    result = [e for e in events(log_path) if e["event"] == "quote_result"][0]
    assert result["result"] == "QuoteOk" and result["attempts"] == 1
    assert conv.quotes and out[0] == format_quote(conv.quotes[0].data)
    assert conv.status == "resolvida"


@pytest.mark.parametrize("override,needle", [({"idade": 80}, "75 anos"), ({"veiculo_ano": 2004}, "20 anos")])
def test_business_refusal_from_real_api_is_one_attempt_and_explained_by_llm(stable_url, log_path, override, needle):
    args = {"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "data_inicio": START, **override}
    agent, llm, _ = _agent(stable_url, log_path, [call("cotar", **args), say("Infelizmente nao conseguimos aceitar.")])
    conv = _ready(agent)
    out = agent.handle(conv, InboundMessage("cota"))
    assert out == ["Infelizmente nao conseguimos aceitar."]
    assert needle in (llm.seen[-1][-1].content or "")
    attempts = [e for e in events(log_path) if e["event"] == "quote_attempt"]
    assert [a["status"] for a in attempts] == [422]
    assert conv.handoff is None and conv.status == "resolvida" and not conv.quotes


# regressao QA 2026-11-23 (era xfail, corrigido): valor fora do schema (ano 1949, idade 201, ano de 2 digitos) vira  Q
@pytest.mark.parametrize("override", [{"veiculo_ano": 1949}, {"idade": 201}, {"veiculo_ano": 22}])
def test_out_of_schema_values_do_not_become_fake_instability(stable_url, log_path, override):
    args = {"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "data_inicio": START, **override}
    agent, _, _ = _agent(stable_url, log_path, [call("cotar", **args), say("Pode confirmar?")])
    conv = _ready(agent)
    out = agent.handle(conv, InboundMessage("cota"))
    assert conv.handoff is None and HANDOFF_MESSAGES[HandoffReason.INVALID_REQUEST] not in out


def test_failure_rate_1_hands_off_after_3_attempts_without_price(log_path):
    with quote_service(failure_rate=1.0) as url:
        client = QuoteClient(url, JsonlTracer(log_path), sleep=lambda _s: None)
        r = client.quote({"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "cep": "01310-100"},
                         conversation_id="c")
        assert isinstance(r, QuoteUnavailable) and r.attempts == 3
        statuses = [e["status"] for e in events(log_path) if e["event"] == "quote_attempt"]
        assert len(statuses) == 3 and set(statuses) <= {500, 502, 503}

        agent, _, _ = _agent(url, log_path, [call("cotar", plano_id="completo", idade=35, veiculo_ano=2022,
                                                  data_inicio=START)], sleep=lambda _s: None)
        conv = _ready(agent)
        out = agent.handle(conv, InboundMessage("cota"))
        assert out == [HANDOFF_MESSAGES[HandoffReason.QUOTE_UNAVAILABLE]] and not has_money(out[0])
        assert conv.handoff is HandoffReason.QUOTE_UNAVAILABLE


def test_slow_rate_1_recovers_on_third_attempt_with_default_timeouts(log_path):
    """QUOTE_SLOW_SECONDS=8: timeouts (3, 3, 9) cortam as duas primeiras e a 3a espera a resposta lenta."""
    with quote_service(failure_rate=0.0, slow_rate=1.0, slow_seconds=8) as url:
        client = QuoteClient(url, JsonlTracer(log_path))  # timeouts e backoff default, sleep real
        t0 = time.perf_counter()
        r = client.quote({"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "cep": "01310-100"},
                         conversation_id="c")
        elapsed = time.perf_counter() - t0
    assert isinstance(r, QuoteOk) and r.attempts == 3
    attempts = [e for e in events(log_path) if e["event"] == "quote_attempt"]
    assert [a["status"] for a in attempts] == ["timeout", "timeout", 200]
    assert [a["timeout_s"] for a in attempts] == [3.0, 3.0, 9.0]
    # 3 + 3 + 8 s de servidor + backoff (0,5..0,75) + (1,0..1,25) s
    assert 15.0 <= elapsed <= 17.5, elapsed


def _predict(seed: int, n_quotes: int, fail: float, slow: float, timeouts: tuple[float, ...], slow_s: float):
    """Replica o RNG do servico (roll, depois choice se falha) para prever o status de cada tentativa."""
    rng, out = random.Random(seed), []
    for _ in range(n_quotes):
        seq = []
        for t in timeouts:
            roll = rng.random()
            if roll < fail:
                seq.append(int(rng.choice(["500", "502", "503"])))
            elif roll < fail + slow and t < slow_s:
                seq.append("timeout")
            else:
                seq.append(200)
            if seq[-1] == 200:
                break
        out.append(seq)
    return out


def test_seeded_mix_is_reproducible_and_every_outcome_is_typed(log_path):
    seed, fail, slow, slow_s, timeouts = 1234, 0.3, 0.2, 1.5, (0.6, 0.6, 4.0)
    expected = _predict(seed, 12, fail, slow, timeouts, slow_s)
    with quote_service(failure_rate=fail, slow_rate=slow, slow_seconds=slow_s, seed=seed) as url:
        client = QuoteClient(url, JsonlTracer(log_path), timeouts_s=timeouts, backoff_base_s=0.01)
        results = [client.quote({"plano_id": "essencial", "idade": 30, "veiculo_ano": 2020, "cep": "01310-100"},
                                conversation_id=f"c{i}") for i in range(12)]
    by_quote: dict[str, list] = {}
    for e in events(log_path):
        if e["event"] == "quote_attempt":
            by_quote.setdefault(e["quote_id"], []).append(e["status"])
    assert [by_quote[r.quote_id] for r in results] == expected
    for r, seq in zip(results, expected, strict=True):
        assert isinstance(r, QuoteOk if seq[-1] == 200 else QuoteUnavailable)
    assert any(isinstance(r, QuoteOk) and r.attempts > 1 for r in results)  # o mix exercitou retry
