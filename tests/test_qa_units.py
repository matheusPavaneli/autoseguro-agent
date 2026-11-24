"""QA: lacunas de unidade que os testes originais nao cobrem."""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from agent.core import brl, format_quote
from agent.handoff import HANDOFF_MESSAGES, HandoffReason, detect_inbound
from agent.llm.base import ChatMessage, LLMError, ToolCall, ToolSpec
from agent.llm.factory import ConfigError, build_provider
from agent.llm.openai_compat import OpenAICompatProvider
from agent.pii import extract_cep, find_pii, mask
from agent.price_guard import SAFE_MESSAGE, has_money
from agent.prompts import plan_ids, system_prompt, tools

from .conftest import PLANS, QUOTE_OK

AFTER = dict(media=False, previous_was_media=False, quote_delivered=True)
BEFORE = dict(media=False, previous_was_media=False, quote_delivered=False)


# ---------------- handoff.detect_inbound ----------------

def test_every_handoff_reason_has_a_fixed_message_without_price():
    assert set(HANDOFF_MESSAGES) == set(HandoffReason)
    assert not any(has_money(m) for m in HANDOFF_MESSAGES.values())


@pytest.mark.parametrize("text,expected", [
    ("quero falar com um vendedor pra fechar", HandoffReason.LEAD_ASKED_HUMAN),  # humano vence fechar
    ("achei caro, quero um consultor", HandoffReason.LEAD_ASKED_HUMAN),         # humano vence preco
    ("fechado, mas achei caro", HandoffReason.PRICE_OBJECTION),  # ambigua: objecao (ambos vao ao humano)
    ("vi mais barato na Porto Seguro", HandoffReason.PRICE_OBJECTION),
    ("o preco ta salgado", HandoffReason.PRICE_OBJECTION),
    ("como eu pago?", None),  # pergunta: fica com o LLM (code-review: "como pago a franquia?" e duvida)
    ("MANDA O BOLETO", HandoffReason.WANTS_TO_CLOSE),
])
def test_precedence_after_quote(text, expected):
    assert detect_inbound(text, **AFTER) is expected


@pytest.mark.parametrize("text", ["fechado!", "achei caro", "tem desconto?", "quero contratar"])
def test_close_and_price_do_not_count_before_a_quote(text):
    assert detect_inbound(text, **BEFORE) is None


def test_human_request_counts_before_a_quote_and_media_rules():
    assert detect_inbound("quero falar com alguém", **BEFORE) is HandoffReason.LEAD_ASKED_HUMAN
    # midia: a 1a nao passa, a 2a seguida passa; texto de midia nao e lido como pedido de humano
    assert detect_inbound("[audio] humano", media=True, previous_was_media=False, quote_delivered=True) is None
    assert detect_inbound("[audio]", media=True, previous_was_media=True, quote_delivered=False) \
        is HandoffReason.MEDIA_UNREADABLE


# regressao QA 2026-11-23 (era xfail, corrigido): _CLOSE ignora negacao/contexto; 'contratar'/'fechar'/'fechado'  em f
@pytest.mark.parametrize("text", ["nao vou contratar agora, obrigado", "preciso pensar antes de fechar",
                                  "o carro fica em estacionamento fechado"])
def test_negated_or_unrelated_close_words_are_not_close_intent(text):
    assert detect_inbound(text, **AFTER) is not HandoffReason.WANTS_TO_CLOSE


# regressao QA 2026-11-23 (era xfail, corrigido): _PRICE aceita 'ta/esta/está' mas nao 'tá' (grafia mais comum)
def test_franquia_ta_alta_with_accent_is_price_objection():
    assert detect_inbound("a franquia tá alta", **AFTER) is HandoffReason.PRICE_OBJECTION


# ---------------- price_guard ----------------

@pytest.mark.parametrize("text", ["R$10", "R$ 199", "R$ １９９", "custa 1.299,00", "uns 300 REAIS"])
def test_guard_catches_compact_nbsp_fullwidth_and_upper(text):
    assert has_money(text)


@pytest.mark.parametrize("text", ["Plano Completo, 30 dias de carencia", "voce tem 75 anos", "inicio 2026-12-01",
                                  SAFE_MESSAGE])
def test_guard_lets_plain_integers_dates_and_its_own_message_pass(text):
    assert not has_money(text)


# regressao QA 2026-11-23 (era xfail, corrigido): has_money nao pega preco inteiro sem R$/reais nem decimal de 1 casa
@pytest.mark.parametrize("text", ["fica 199 por mes", "a mensalidade e 189", "sai por 199,9", "R $199",
                                  "uns 150 conto"])
def test_guard_catches_bare_integer_prices(text):
    assert has_money(text)


# ---------------- pii ----------------

def test_email_with_digit_runs_is_masked_whole():
    for raw in ["ana07310100@gmail.com", "joao.52998224725@hotmail.com", "x+11912345678@bol.com.br"]:
        assert mask(f"email {raw} ok") == "email [EMAIL] ok"


@pytest.mark.parametrize("raw,token", [
    ("(11) 91234-5678", "[TELEFONE]"), ("+5511912345678", "[TELEFONE]"), ("11 91234 5678", "[TELEFONE]"),
    ("07310100", "[CEP]"), ("ABC-1234", "[PLACA]"), ("abc1d23", "[PLACA]"),
])
def test_common_variants_are_masked(raw, token):
    assert mask(f"dado {raw} fim") == f"dado {token} fim"


# regressao QA 2026-11-23 (era xfail, corrigido): formatos brasileiros comuns de PII nao sao mascarados  (fixo, celula
@pytest.mark.parametrize("raw", ["(11) 3456-7890", "11 9 8765-4321", "07.310-100", "529 982 247 25"])
def test_other_common_formats_are_masked(raw):
    assert find_pii(mask(raw)) == [] and raw not in mask(raw)
    assert any(ch.isdigit() for ch in raw) and not any(ch.isdigit() for ch in mask(raw))


def test_extract_cep_ignores_digit_runs_inside_email():
    assert extract_cep("email ana07310100@gmail.com") is None
    assert extract_cep("email ana07310100@gmail.com, cep 21040-100") == "21040-100"


def test_extract_cep_takes_first_and_normalises():
    assert extract_cep("cep 07310100") == "07310-100"
    # dois CEPs distintos: ambiguo, nao escolhe (code-review: o 1o tirava o agravo de regiao)
    assert extract_cep("trabalho no 01310-100, durmo no 21040-100") is None
    assert extract_cep("ano 2022, 35 anos") is None


# ---------------- format_quote / brl ----------------

@pytest.mark.parametrize("v,s", [(0, "R$ 0,00"), (3000, "R$ 3.000,00"), (1234567.891, "R$ 1.234.567,89"),
                                 (149.6, "R$ 149,60")])
def test_brl(v, s):
    assert brl(v) == s


def test_format_quote_without_pro_rata_or_carencia_and_unknown_coverage():
    d = {**QUOTE_OK, "coberturas": ["colisao", "nova_cobertura"], "carencia": {"coberturas": [], "dias": 30}}
    d.pop("primeiro_pagamento_pro_rata")
    text = format_quote(d)
    assert "proporcional" not in text and "Carencia" not in text
    assert "nova_cobertura" in text and brl(d["premio_mensal"]) in text


def test_format_quote_money_comes_only_from_the_api_dict():
    text = format_quote(QUOTE_OK)
    import re
    amounts = re.findall(r"R\$ [\d.]+,\d{2}", text)
    allowed = {brl(QUOTE_OK["premio_mensal"]), brl(QUOTE_OK["franquia"]),
               brl(QUOTE_OK["primeiro_pagamento_pro_rata"]["valor_primeiro_pagamento"])}
    assert amounts and set(amounts) <= allowed


# ---------------- prompts ----------------

def test_system_prompt_has_no_money_and_carries_date_plans_and_cep_flag():
    p = system_prompt(PLANS, dt.date(2026, 11, 23), cep_status="sim")
    assert not has_money(p)
    for plan in PLANS["planos"]:
        assert plan["nome"] in p and str(plan["base_mensal"]) not in p and str(plan["franquia"]) not in p
    assert "2026-11-23" in p and "CEP ja informado nesta conversa: sim" in p
    assert "CEP ja informado nesta conversa: nao" in system_prompt(PLANS, dt.date(2026, 11, 23), cep_status="nao")


def test_tools_schema_matches_plans():
    cotar, humano = tools(PLANS)
    assert cotar.parameters["properties"]["plano_id"]["enum"] == plan_ids(PLANS) == ["essencial", "completo", "premium"]
    assert set(cotar.parameters["required"]) == {"plano_id", "idade", "veiculo_ano", "data_inicio"}
    assert humano.parameters["required"] == ["motivo"]


# ---------------- factory ----------------

def test_factory_is_case_insensitive_and_blank_overrides_fall_back():
    p = build_provider({"LLM_PROVIDER": "GROQ", "LLM_API_KEY": "k", "LLM_MODEL": "", "LLM_BASE_URL": ""})
    assert p.model == "openai/gpt-oss-120b" and p.base_url == "https://api.groq.com/openai/v1"


def test_factory_blank_provider_is_a_config_error_not_default():
    """Comportamento atual (documentado): LLM_PROVIDER= vazio nao cai no default groq."""
    with pytest.raises(ConfigError):
        build_provider({"LLM_PROVIDER": "", "LLM_API_KEY": "k"})


# ---------------- openai_compat: traducao ----------------

def _provider(handler, **kw) -> tuple[OpenAICompatProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    return OpenAICompatProvider(base_url="https://llm.test/v1/", api_key="sk-test-123", model="m",
                                transport=httpx.MockTransport(h), **kw), seen


def _ok(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message}]})


def test_request_translation_messages_tools_and_auth():
    p, seen = _provider(lambda r: _ok({"content": "oi"}))
    tc = ToolCall(id="t1", name="cotar", arguments={"idade": 35, "obs": "ação"})
    msgs = [ChatMessage("system", "s"), ChatMessage("user", "u"),
            ChatMessage("assistant", None, tool_calls=(tc,)), ChatMessage("tool", '{"ok":1}', tool_call_id="t1")]
    spec = ToolSpec("cotar", "d", {"type": "object", "properties": {}})
    assert p.complete(msgs, [spec]).text == "oi"
    req = seen[0]
    assert str(req.url) == "https://llm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-123"
    body = json.loads(req.content)
    assert body["model"] == "m" and body["temperature"] == 0.2
    assert body["tools"] == [{"type": "function", "function": {"name": "cotar", "description": "d",
                                                               "parameters": {"type": "object", "properties": {}}}}]
    assert body["messages"][2] == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "cotar", "arguments": '{"idade": 35, "obs": "ação"}'}}]}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": '{"ok":1}'}


def test_no_tools_key_when_tools_empty_and_response_parsing():
    p, seen = _provider(lambda r: _ok({"content": None, "tool_calls": [
        {"id": "a", "function": {"name": "cotar", "arguments": '{"idade": 40}'}},
        {"id": "b", "function": {"name": "encaminhar_humano"}}]}))
    reply = p.complete([ChatMessage("user", "x")], [])
    assert "tools" not in json.loads(seen[0].content)
    assert reply.text is None
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [("a", "cotar", {"idade": 40}),
                                                                      ("b", "encaminhar_humano", {})]


@pytest.mark.parametrize("resp", [httpx.Response(200, json={"choices": []}), httpx.Response(200, text="<html>"),
                                  _ok({"content": None, "tool_calls": [{"id": "a", "function": {
                                      "name": "cotar", "arguments": "{idade: 40"}}]})])
def test_malformed_responses_become_llm_error(resp):
    p, _ = _provider(lambda r: resp)
    with pytest.raises(LLMError, match="formato"):
        p.complete([ChatMessage("user", "x")], [])


def test_http_error_status_is_in_llm_error_and_body_truncated():
    p, _ = _provider(lambda r: httpx.Response(500, text="x" * 1000))
    with pytest.raises(LLMError) as ei:
        p.complete([ChatMessage("user", "x")], [])
    assert str(ei.value).startswith("HTTP 500: ") and len(str(ei.value)) <= len("HTTP 500: ") + 300


@pytest.mark.parametrize("text,expected", [
    ("nao quero falar com atendente, prefiro cotar aqui", None),
    ("vou falar com alguem aqui de casa e te aviso", None),
    ("preciso falar com o vendedor da concessionaria antes", None),
    ("vou querer cotar o premium tambem", None),
    ("quero esse mesmo carro no premium", None),
    ("e o premium, fica muito mais caro?", None),
    ("o carro e azul e tem menos de 5 anos", None),
    ("moro em Porto Alegre, pelo menos 3 anos", None),
    ("quero fechar ainda hoje", HandoffReason.WANTS_TO_CLOSE),
    ("nao achei caro, quero fechar", HandoffReason.WANTS_TO_CLOSE),
    ("me passa pra um consultor", HandoffReason.LEAD_ASKED_HUMAN),
])
def test_second_review_handoff_precision(text, expected):
    """Regressao da 2a passada do code-review: palavra-chave solta disparava handoff errado."""
    assert detect_inbound(text, **AFTER) is expected


@pytest.mark.parametrize("text", ["como pago a franquia se eu bater o carro?",
                                  "vou fechar com a porto, la ta mais barato",
                                  "quero contratar o premium em vez desse, cota pra mim?"])
def test_questions_competitor_close_and_requote_are_not_our_close(text):
    """Regressao do code-review do novo ciclo: pergunta, fechar com concorrente e pedir outra cotacao."""
    assert detect_inbound(text, **AFTER) is not HandoffReason.WANTS_TO_CLOSE


@pytest.mark.parametrize("text", ["quero fechar com voces", "Gostei, quero fechar essa cotacao",
                                  "fechado, pode emitir a cotacao", "quero fechar, mas antes uma duvida: tem guincho?"])
def test_close_with_us_is_close(text):
    """Regressao da 2a passada do novo ciclo: 'fechar com voces' e 'essa cotacao' eram vetados."""
    assert detect_inbound(text, **AFTER) is HandoffReason.WANTS_TO_CLOSE


def test_objection_then_question_and_short_questions():
    assert detect_inbound("Achei caro, tem desconto?", **AFTER) is HandoffReason.PRICE_OBJECTION
    for t in ["e o contrato?", "fecha?", "aceito cartao?"]:
        assert detect_inbound(t, **AFTER) is None, t


@pytest.mark.parametrize("text", ["sim", "quero seguir", "pode seguir", "sim, quero esse", "vou querer"])
def test_yes_to_the_fixed_followup_is_close(text):
    assert detect_inbound(text, **AFTER, followup_asked=True) is HandoffReason.WANTS_TO_CLOSE
    assert detect_inbound("sim", **AFTER) is None  # sem o seguimento, "sim" sozinho nao diz nada
