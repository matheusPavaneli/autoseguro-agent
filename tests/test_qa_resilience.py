"""QA: caos no LLM (via o adapter real com MockTransport) e respostas estranhas da /quote."""
from __future__ import annotations

import json

import httpx
import pytest

from agent.core import QUOTE_FOLLOWUP, Agent, InboundMessage
from agent.handoff import AFTER_HANDOFF_MESSAGE, HANDOFF_MESSAGES, HandoffReason
from agent.llm.base import LLMReply, ToolCall
from agent.llm.openai_compat import OpenAICompatProvider
from agent.quote_client import QuoteClient
from agent.trace import JsonlTracer

from .conftest import COTAR, QUOTE_OK, TODAY, call, events, say, scripted_transport

TECH = HANDOFF_MESSAGES[HandoffReason.LLM_UNAVAILABLE]


def _real_llm_agent(log_path, llm_responses, quote_responses=()):
    """Agente com o adapter OpenAI-compat REAL; o provedor responde pela fila llm_responses."""
    queue = list(llm_responses)
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    llm = OpenAICompatProvider(base_url="https://llm.test/v1", api_key="k", model="m",
                               transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    tracer = JsonlTracer(log_path)
    transport, calls = scripted_transport(quote_responses)
    agent = Agent(llm, QuoteClient("http://quote", tracer, transport=transport, sleep=lambda _s: None), tracer,
                  today=lambda: TODAY)
    return agent, seen, calls


def _msg(content=None, tool_calls=None) -> httpx.Response:
    m = {"role": "assistant", "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": m}]})


def _tc(name, args: str, id_="t1"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": args}}


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"),
                                     httpx.Response(429, json={"error": {"message": "rate limit"}}),
                                     httpx.Response(500, text="boom"),
                                     httpx.Response(200, text="not json"),
                                     _msg(tool_calls=[_tc("cotar", "{idade: 3")])],
                         ids=["timeout", "conn", "429", "500", "html200", "bad-args-json"])
def test_llm_failures_end_in_handoff_not_exception(log_path, failure):
    # falha persistente: 429/5xx/timeout sao repetidos 1x pelo adapter, entao a fila tem a falha duas vezes
    agent, _, _ = _real_llm_agent(log_path, [failure, failure])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("oi")) == [TECH]
    assert conv.handoff is HandoffReason.LLM_UNAVAILABLE
    llm_ev = [e for e in events(log_path) if e["event"] == "llm_call"]
    assert llm_ev[-1]["status"] == "error" and llm_ev[-1]["error"]
    assert agent.handle(conv, InboundMessage("alo?")) == [AFTER_HANDOFF_MESSAGE]


def test_single_429_is_retried_once_and_conversation_continues(log_path):
    """Era achado de design do QA (um 429 isolado do free tier ia direto ao humano); agora o adapter
    repete 1x respeitando Retry-After (ate 5 s) e a conversa segue."""
    agent, seen, _ = _real_llm_agent(log_path, [httpx.Response(429, text="rate"), _msg("Ola!")])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("oi")) == ["Ola!"]
    assert len(seen) == 2 and conv.handoff is None


# regressao QA 2026-11-23 (era xfail, corrigido): arguments JSON valido mas nao-objeto (null, lista, string) gera  Att
@pytest.mark.parametrize("args", ["null", "[1, 2]", '"plano completo"'])
def test_non_object_tool_arguments_do_not_crash(log_path, args):
    agent, _, _ = _real_llm_agent(log_path, [_msg(tool_calls=[_tc("cotar", args)]), _msg("Pode repetir?")])
    conv = agent.start()
    conv.cep = "07310-100"
    out = agent.handle(conv, InboundMessage("cota"))
    assert out


@pytest.mark.parametrize("bad", [{"idade": "trinta"}, {"veiculo_ano": "dois mil e vinte"}, {"data_inicio": "15/12"},
                                 {"idade": None}, {"plano_id": "ouro"}])
def test_wrong_typed_tool_args_go_back_to_llm_without_api_call(make_agent, bad):
    agent, llm, calls = make_agent([call("cotar", **{**COTAR, **bad}), say("Pode confirmar os dados?")])
    conv = agent.start()
    conv.cep = "07310-100"
    assert agent.handle(conv, InboundMessage("cota")) == ["Pode confirmar os dados?"]
    assert calls == [] and "erro" in (llm.seen[-1][-1].content or "")


def test_missing_arg_and_past_date_are_reported_to_llm(make_agent):
    args = {k: v for k, v in COTAR.items() if k != "data_inicio"}
    agent, llm, calls = make_agent([call("cotar", **args), call("cotar", **{**COTAR, "data_inicio": "2026-11-22"}),
                                    say("Qual a data?")])
    conv = agent.start()
    conv.cep = "07310-100"
    agent.handle(conv, InboundMessage("cota"))
    tool_msgs = [m.content for m in llm.seen[-1] if m.role == "tool"]
    assert "dados_incompletos_ou_invalidos" in tool_msgs[0] and "data_inicio_no_passado" in tool_msgs[1]
    assert calls == []


def test_float_and_string_numbers_are_coerced(make_agent):
    """Documenta: idade 35.9 vira 35 (int trunca) e '2022' vira 2022; nao ha checagem de faixa no agente."""
    agent, _, calls = make_agent([call("cotar", **{**COTAR, "idade": 35.9, "veiculo_ano": "2022"}), say("ok")],
                                 [httpx.Response(200, json=QUOTE_OK)])
    conv = agent.start()
    conv.cep = "07310-100"
    agent.handle(conv, InboundMessage("cota"))
    sent = json.loads(calls[0].read())
    assert sent["idade"] == 35 and sent["veiculo_ano"] == 2022


def test_unknown_tool_is_reported_and_conversation_continues(make_agent):
    agent, llm, _ = make_agent([call("aplicar_desconto", pct=50), say("Nao consigo dar desconto.")])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("me da desconto")) == ["Nao consigo dar desconto."]
    assert "ferramenta desconhecida" in (llm.seen[-1][-1].content or "") and conv.handoff is None


# regressao QA 2026-11-23 (era xfail, corrigido): resposta vazia do LLM (content null/'' sem tool_calls) e enviada ao
@pytest.mark.parametrize("text", [None, "", "   ", "​"])
def test_empty_llm_reply_is_not_sent_as_empty_message(make_agent, text):
    agent, _, _ = make_agent([LLMReply(text=text), say("Oi! Posso ajudar?")])
    conv = agent.start()
    out = agent.handle(conv, InboundMessage("oi"))
    assert all(o.strip() for o in out)


def test_text_plus_tool_call_only_the_code_built_quote_reaches_the_lead(make_agent):
    reply = LLMReply(text="Vou cotar: fica uns R$ 99,00", tool_calls=(ToolCall("t", "cotar", COTAR),))
    agent, llm, _ = make_agent([reply], [httpx.Response(200, json=QUOTE_OK)])
    conv = agent.start()
    conv.cep = "07310-100"
    out = agent.handle(conv, InboundMessage("cota"))
    assert len(out) == 2 and out[1] == QUOTE_FOLLOWUP and "99,00" not in "".join(out)
    assert len(llm.seen) == 1


def test_quote_plus_handoff_in_same_reply_sends_quote_then_handoff(make_agent):
    reply = LLMReply(text=None, tool_calls=(ToolCall("a", "cotar", COTAR),
                                            ToolCall("b", "encaminhar_humano", {"motivo": "quer_fechar"})))
    agent, _, _ = make_agent([reply], [httpx.Response(200, json=QUOTE_OK)])
    conv = agent.start()
    conv.cep = "07310-100"
    out = agent.handle(conv, InboundMessage("cota e ja quero fechar"))
    assert out[0].startswith("Cotacao do plano") and out[1] == HANDOFF_MESSAGES[HandoffReason.WANTS_TO_CLOSE]
    assert conv.handoff is HandoffReason.WANTS_TO_CLOSE and conv.quotes


def test_unknown_handoff_motive_falls_back_to_out_of_scope(make_agent):
    agent, _, _ = make_agent([call("encaminhar_humano", motivo="sei_la")])
    conv = agent.start()
    agent.handle(conv, InboundMessage("quero cancelar minha apolice"))
    assert conv.handoff is HandoffReason.OUT_OF_SCOPE


def test_repeated_tool_loop_stops_after_max_rounds(make_agent):
    agent, llm, _ = make_agent([call("ferramenta_x")] * 4)
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("oi")) == [HANDOFF_MESSAGES[HandoffReason.TOOL_LOOP_EXHAUSTED]]
    assert len(llm.seen) == 4


# regressao QA 2026-11-23 (era xfail, corrigido): /quote 200 com corpo nao-JSON ou sem campos esperados faz  r.json()/
@pytest.mark.parametrize("resp", [httpx.Response(200, text="<html>gateway</html>"),
                                  httpx.Response(200, json={"plano_nome": "Completo"}),
                                  httpx.Response(200, json=[1, 2])])
def test_malformed_200_from_quote_api_does_not_crash(make_agent, resp):
    agent, _, _ = make_agent([call("cotar", **COTAR), say("ok")], [resp, resp, resp])
    conv = agent.start()
    conv.cep = "07310-100"
    out = agent.handle(conv, InboundMessage("cota"))
    assert out and not any("R$" in o for o in out)
