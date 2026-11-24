from __future__ import annotations

import httpx

from agent.core import QUOTE_FOLLOWUP, InboundMessage, brl
from agent.handoff import HANDOFF_MESSAGES, HandoffReason
from agent.price_guard import SAFE_MESSAGE_NO_QUOTE

from .conftest import COTAR, LLM_DOWN, QUOTE_OK, call, events, say

CEP_MSG = "Cpf 529.982.247-25, tenho 35 anos, cep 07310-100"


def _ready(agent):
    """Conversa com CEP ja no estado (extraido pelo codigo do texto cru)."""
    conv = agent.start()
    agent.handle(conv, InboundMessage(CEP_MSG))
    return conv


def _all_text_sent_to_llm(llm) -> str:
    return "\n".join(m.content or "" for msgs in llm.seen for m in msgs)


def test_quote_ok_sends_code_built_text_with_exact_api_values(make_agent):
    agent, llm, calls = make_agent([say("Anotado!"), call("cotar", **COTAR), say("Quer fechar?")],
                                   [httpx.Response(200, json=QUOTE_OK)])
    conv = _ready(agent)
    out = agent.handle(conv, InboundMessage("pode cotar o completo, comeca dia 15/12"))
    quote_text = out[0]
    assert brl(272.87) in quote_text and brl(3000) in quote_text and brl(149.64) in quote_text
    assert "30 dias" in quote_text and "17 de 31 dias" in quote_text
    assert out[1] == QUOTE_FOLLOWUP
    assert len(llm.seen) == 2  # 'Anotado!' + a chamada que pediu cotar; nenhuma depois da cotacao
    assert conv.status == "resolvida" and conv.handoff is None
    sent = calls[0].read().decode()
    assert '"cep":"07310-100"' in sent.replace(" ", "")


def test_quote_unavailable_hands_off_with_fixed_text_and_no_price(make_agent):
    agent, llm, calls = make_agent([say("Anotado!"), call("cotar", **COTAR)], [httpx.Response(503)] * 3)
    conv = _ready(agent)
    out = agent.handle(conv, InboundMessage("cota o completo"))
    assert out == [HANDOFF_MESSAGES[HandoffReason.QUOTE_UNAVAILABLE]]
    assert "R$" not in out[0]
    assert conv.handoff is HandoffReason.QUOTE_UNAVAILABLE and len(calls) == 3
    llm_calls = len(llm.seen)
    assert agent.handle(conv, InboundMessage("e ai?")) and len(llm.seen) == llm_calls  # LLM nao e mais chamado


def test_refusal_goes_back_to_llm_and_is_not_a_handoff(make_agent):
    refusal = {"error": "cotacao_recusada", "motivo": "Veiculo com mais de 20 anos nao e aceito."}
    agent, llm, _ = make_agent([say("Anotado!"), call("cotar", **{**COTAR, "veiculo_ano": 2004}),
                                say("Infelizmente nao aceitamos carros com mais de 20 anos.")],
                               [httpx.Response(422, json=refusal)])
    conv = _ready(agent)
    out = agent.handle(conv, InboundMessage("cota ai"))
    assert out == ["Infelizmente nao aceitamos carros com mais de 20 anos."]
    tool_msg = llm.seen[-1][-1]
    assert tool_msg.role == "tool" and "mais de 20 anos" in (tool_msg.content or "")
    assert conv.handoff is None and conv.status == "resolvida"


def test_quote_without_cep_does_not_call_api(make_agent):
    agent, llm, calls = make_agent([call("cotar", **COTAR), say("Qual o CEP?")])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("tenho 35 anos, corolla 2022")) == ["Qual o CEP?"]
    assert calls == [] and "falta_cep" in (llm.seen[-1][-1].content or "")


def test_raw_cpf_and_cep_never_reach_the_provider(make_agent):
    agent, llm, _ = make_agent([say("Anotado!")])
    conv = agent.start()
    agent.handle(conv, InboundMessage(CEP_MSG))
    sent = _all_text_sent_to_llm(llm)
    assert "529.982.247-25" not in sent and "07310-100" not in sent
    assert "[CPF]" in sent and "[CEP]" in sent and conv.cep == "07310-100"


def test_llm_error_becomes_handoff_without_raising(make_agent):
    agent, _, _ = make_agent([LLM_DOWN])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("oi")) == [HANDOFF_MESSAGES[HandoffReason.LLM_UNAVAILABLE]]
    assert conv.handoff is HandoffReason.LLM_UNAVAILABLE


def test_media_twice_and_explicit_human_request(make_agent):
    agent, llm, _ = make_agent([])
    conv = agent.start()
    first = agent.handle(conv, InboundMessage("[audio] mensagem de voz (18s)", "audio"))
    assert conv.handoff is None and "texto" in first[0]
    agent.handle(conv, InboundMessage("[documento] CNH_frente.pdf", "document"))
    assert conv.handoff is HandoffReason.MEDIA_UNREADABLE and llm.seen == []

    conv2 = agent.start()
    agent.handle(conv2, InboundMessage("quero falar com um atendente"))
    assert conv2.handoff is HandoffReason.LEAD_ASKED_HUMAN


def test_close_and_objection_after_quote_and_tool_loop_limit(make_agent):
    def quoted(extra_replies):
        agent, llm, _ = make_agent([say("Anotado!"), call("cotar", **COTAR), say("Quer fechar?"), *extra_replies],
                                   [httpx.Response(200, json=QUOTE_OK)])
        conv = _ready(agent)
        agent.handle(conv, InboundMessage("cota o completo"))
        return agent, conv

    agent, conv = quoted([])
    agent.handle(conv, InboundMessage("fechado!"))
    assert conv.handoff is HandoffReason.WANTS_TO_CLOSE

    agent, conv = quoted([])
    agent.handle(conv, InboundMessage("achei caro pra esse carro"))
    assert conv.handoff is HandoffReason.PRICE_OBJECTION

    loop = [call("cotar", **{**COTAR, "data_inicio": "2020-01-01"})] * 4
    agent, _, _ = make_agent(loop)
    conv = _ready_without_llm(agent)
    agent.handle(conv, InboundMessage("cota"))
    assert conv.handoff is HandoffReason.TOOL_LOOP_EXHAUSTED


def _ready_without_llm(agent):
    conv = agent.start()
    conv.cep = "07310-100"
    return conv


def test_invented_price_is_replaced_then_second_strike_hands_off(make_agent):
    agent, _, _ = make_agent([say("Fica uns R$ 199,90 por mes"), say("Na verdade 189,90")])
    conv = agent.start()
    # sem cotacao ainda: a mensagem segura pede os dados em vez de apontar para uma cotacao inexistente
    assert agent.handle(conv, InboundMessage("quanto fica?")) == [SAFE_MESSAGE_NO_QUOTE]
    agent.handle(conv, InboundMessage("e ai, quanto?"))
    assert conv.handoff is HandoffReason.UNSAFE_REPLY
    assert [e["event"] for e in events(agent._tracer.path)].count("guard_blocked") == 2


def test_next_model_year_is_sent_as_current_year(make_agent):
    agent, _, calls = make_agent([say("Anotado!"), call("cotar", **{**COTAR, "veiculo_ano": 2027}), say("ok")],
                                 [httpx.Response(200, json=QUOTE_OK)])
    conv = _ready(agent)
    agent.handle(conv, InboundMessage("cota"))
    assert '"veiculo_ano":2026' in calls[0].read().decode().replace(" ", "")


def test_invisible_characters_from_llm_are_stripped(make_agent):
    agent, _, _ = make_agent([say("Posso ajudar​​ com⁠ mais algo?﻿")])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("oi")) == ["Posso ajudar com mais algo?"]


def test_two_ceps_in_one_message_are_not_guessed(make_agent):
    """Regressao do code-review: o 1o CEP vencia e cotava sem o agravo do CEP onde o carro dorme."""
    agent, llm, calls = make_agent([say("Qual dos dois e onde o carro dorme?")])
    conv = agent.start()
    agent.handle(conv, InboundMessage("moro no 01310-100 mas o carro dorme no 07123-000"))
    assert conv.cep is None and conv.cep_ambiguous
    assert "ambiguo" in (llm.seen[-1][0].content or "")


def test_safe_message_before_any_quote_does_not_point_to_a_quote(make_agent):
    from agent.price_guard import SAFE_MESSAGE_NO_QUOTE
    agent, _, _ = make_agent([say("sai por volta de R$ 200 por mes")])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("quanto custa mais ou menos?")) == [SAFE_MESSAGE_NO_QUOTE]


def test_llm_close_or_objection_before_any_quote_is_refused(make_agent):
    """Regressao da 2a passada: o LLM encaminhava 'objecao_preco' sem cotacao e o lead ouvia 'passei sua cotacao'."""
    agent, llm, _ = make_agent([call("encaminhar_humano", motivo="objecao_preco"), say("Vamos cotar primeiro?")])
    conv = agent.start()
    assert agent.handle(conv, InboundMessage("seguro hoje em dia e muito caro ne")) == ["Vamos cotar primeiro?"]
    assert conv.handoff is None and "sem_cotacao_ainda" in (llm.seen[-1][-1].content or "")


def test_stray_eight_digits_do_not_replace_the_stored_cep(make_agent):
    agent, _, _ = make_agent([say("ok"), say("ok"), say("ok")])
    conv = agent.start()
    agent.handle(conv, InboundMessage("cep 07310-100"))
    agent.handle(conv, InboundMessage("meu RG e 12345678"))
    assert conv.cep == "07310-100"
    agent.handle(conv, InboundMessage("errei, o cep certo e 21040-100"))
    assert conv.cep == "21040-100"


def test_guard_strikes_reset_after_a_quote_turn(make_agent):
    agent, _, _ = make_agent([say("uns R$ 200"), call("cotar", **COTAR), say("agora R$ 199")],
                             [httpx.Response(200, json=QUOTE_OK)])
    conv = agent.start()
    conv.cep = "07310-100"
    agent.handle(conv, InboundMessage("quanto?"))      # strike 1
    agent.handle(conv, InboundMessage("cota"))         # cotacao entregue: zera
    agent.handle(conv, InboundMessage("e o premium?"))  # strike 1 de novo, nao 2
    assert conv.handoff is None and conv.guard_strikes == 1


def test_formatted_cep_correction_replaces_stored_cep(make_agent):
    agent, _, _ = make_agent([say("ok"), say("ok")])
    conv = agent.start()
    agent.handle(conv, InboundMessage("cep 01310-100"))
    agent.handle(conv, InboundMessage("na verdade o carro dorme na casa da minha mae, 07123-000"))
    assert conv.cep == "07123-000"


def test_tool_result_tells_llm_what_was_quoted_without_prices(make_agent):
    """Regressao: o LLM nao sabia da carencia e respondia duvida sobre a cotacao no escuro."""
    agent, llm, _ = make_agent([call("cotar", **COTAR), say("ok")], [httpx.Response(200, json=QUOTE_OK)])
    conv = agent.start()
    conv.cep = "07310-100"
    agent.handle(conv, InboundMessage("cota"))
    tool_msg = next(m for m in conv.history if m.role == "tool")
    assert '"dias": 30' in (tool_msg.content or "") and "roubo" in (tool_msg.content or "")
    assert "272" not in (tool_msg.content or "") and "3000" not in (tool_msg.content or "")


def test_bare_cep_reply_is_stored(make_agent):
    agent, _, _ = make_agent([say("ok")])
    conv = agent.start()
    agent.handle(conv, InboundMessage("07310100"))
    assert conv.cep == "07310-100"


def test_handoff_after_refusal_is_allowed_and_overflow_is_handled(make_agent):
    refusal = {"error": "cotacao_recusada", "motivo": "Idade acima do limite de aceitacao (75 anos)."}
    agent, _, calls = make_agent([call("cotar", **COTAR), say("Infelizmente..."),
                                  call("encaminhar_humano", motivo="quer_fechar")], [httpx.Response(422, json=refusal)])
    conv = agent.start()
    conv.cep = "07310-100"
    agent.handle(conv, InboundMessage("cota"))
    agent.handle(conv, InboundMessage("quero fechar mesmo assim"))
    assert conv.handoff is HandoffReason.WANTS_TO_CLOSE and len(calls) == 1

    agent2, llm2, _ = make_agent([call("cotar", **{**COTAR, "idade": float("inf")}), say("Qual sua idade?")])
    conv2 = agent2.start()
    conv2.cep = "07310-100"
    assert agent2.handle(conv2, InboundMessage("cota")) == ["Qual sua idade?"]
