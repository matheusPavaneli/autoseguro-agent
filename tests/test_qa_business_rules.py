"""QA: regras de negocio ponta a ponta. Agente (LLM fake) -> API real -> texto ao lead, comparado com o
premio recalculado de forma independente a partir do plans.json (tests/qa_support.expected_premium)."""
from __future__ import annotations

import datetime as dt
from calendar import monthrange

import pytest

from agent.core import Agent, InboundMessage, brl
from agent.quote_client import QuoteClient
from agent.trace import JsonlTracer

from .conftest import FakeProvider, call, events, say
from .qa_support import expected_premium, quote_service

TODAY = dt.date.today()
Y = TODAY.year
START = (TODAY + dt.timedelta(days=22)).isoformat()


@pytest.fixture(scope="module")
def url():
    with quote_service(failure_rate=0.0, slow_rate=0.0) as u:
        yield u


def _quote(url, log_path, *, plano="completo", idade=35, ano=Y - 4, cep="01310-100", inicio=START):
    tracer = JsonlTracer(log_path)
    llm = FakeProvider([call("cotar", plano_id=plano, idade=idade, veiculo_ano=ano, data_inicio=inicio),
                        say("Infelizmente nao da.")])
    agent = Agent(llm, QuoteClient(url, tracer), tracer, today=lambda: TODAY)
    conv = agent.start()
    conv.cep = cep
    out = agent.handle(conv, InboundMessage("cota"))
    return conv, out, [e for e in events(log_path) if e["event"] == "quote_result"][-1]


def _check(conv, out, result, expected):
    if expected is None:
        assert result["result"] == "QuoteRefused" and not conv.quotes and conv.refusals
        assert not any("R$" in o for o in out)
    else:
        assert result["result"] == "QuoteOk" and result["premio_mensal"] == expected
        assert f"Mensalidade: {brl(expected)}" in out[0]


@pytest.mark.parametrize("idade", [17, 18, 24, 25, 29, 30, 59, 60, 75, 76])
def test_age_band_edges(url, log_path, idade):
    _check(*_quote(url, log_path, idade=idade), expected_premium("completo", idade, Y - 4, "01310-100"))


@pytest.mark.parametrize("anos", [0, 5, 6, 10, 11, 20, 21])
def test_vehicle_age_edges(url, log_path, anos):
    _check(*_quote(url, log_path, ano=Y - anos), expected_premium("completo", 35, Y - anos, "01310-100"))


def test_next_model_year_is_priced_as_new_car(url, log_path):
    _check(*_quote(url, log_path, ano=Y + 1), expected_premium("completo", 35, Y, "01310-100"))


@pytest.mark.parametrize("cep", ["07310-100", "08000-000", "21040-100", "26000-000", "59000-000",
                                 "01310-100", "06000-000", "09000-000", "20000-000", "58000-000", "60000-000"])
def test_high_risk_cep_prefixes(url, log_path, cep):
    exp = expected_premium("essencial", 35, Y - 4, cep)
    conv, out, res = _quote(url, log_path, plano="essencial", cep=cep)
    _check(conv, out, res, exp)
    high = cep[:2] in {"07", "08", "21", "26", "59"}
    assert conv.quotes[0].data["multiplicadores"]["regiao"] == (1.3 if high else 1.0)


def _next(day: int, month: int | None = None) -> dt.date:
    """Proxima data >= hoje com esse dia (e mes, se dado)."""
    d = TODAY
    while True:
        try:
            cand = dt.date(d.year, month or d.month, day)
        except ValueError:
            cand = None
        if cand and cand >= TODAY:
            return cand
        d = dt.date(d.year + (1 if (month or d.month) == 12 or month else 0),
                    (d.month % 12) + 1 if not month else month, 1)


@pytest.mark.parametrize("inicio", [_next(1), _next(15), _next(31), _next(15, 2), _next(29, 2), _next(1, 2)],
                         ids=["dia1", "dia15", "dia31", "fev15", "fev29", "fev1"])
def test_pro_rata_first_payment(url, log_path, inicio):
    conv, out, res = _quote(url, log_path, inicio=inicio.isoformat())
    premio = expected_premium("completo", 35, Y - 4, "01310-100")
    _check(conv, out, res, premio)
    data = conv.quotes[0].data
    if inicio.day == 1:
        assert "primeiro_pagamento_pro_rata" not in data and "proporcional" not in out[0]
        return
    dias_mes = monthrange(inicio.year, inicio.month)[1]
    cobrados = dias_mes - inicio.day + 1
    valor = round(premio * cobrados / dias_mes, 2)
    assert data["primeiro_pagamento_pro_rata"] == {"dias_no_mes": dias_mes, "dias_cobrados": cobrados,
                                                   "valor_primeiro_pagamento": valor}
    assert f"({cobrados} de {dias_mes} dias): {brl(valor)}; depois {brl(premio)} por mes." in out[0]


@pytest.mark.parametrize("plano", ["essencial", "completo", "premium"])
def test_carencia_text_for_every_plan(url, log_path, plano):
    conv, out, _ = _quote(url, log_path, plano=plano)
    assert "- Carencia: roubo, furto so passam a valer 30 dias apos o inicio da vigencia." in out[0]
    assert brl(conv.quotes[0].data["franquia"]) in out[0]
