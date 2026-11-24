"""QA: teste de mutacao. Cada mutacao e aplicada numa COPIA do fonte (exec em modulo novo, nunca no disco);
o modulo mutante entra so via monkeypatch. Para cada mutacao, roda uma lista de testes existentes e novos e
exige que ao menos um falhe. Um controle (copia sem mutacao) prova que os testes passam com o codigo intacto.

Rodar com -s mostra a tabela mutacao -> testes que pegaram.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

import agent.core
import agent.handoff
import agent.pii
import agent.price_guard
import agent.quote_client
import agent.trace

from . import test_core, test_pii, test_price_guard, test_qa_fuzz, test_qa_security, test_qa_units, test_quote_client
from .conftest import FakeProvider, scripted_transport
from .qa_support import ROOT

RESULT_CLASSES = ("QuoteOk", "QuoteRefused", "QuoteInvalid", "QuoteUnavailable")


def _mutant(module: types.ModuleType, old: str, new: str) -> types.ModuleType:
    src = Path(module.__file__).read_text("utf-8")
    assert src.count(old) == 1, f"trecho da mutacao nao e unico em {module.__name__}: {old!r}"
    mod = types.ModuleType(module.__name__ + "_mutant")
    mod.__package__ = "agent"
    mod.__file__ = module.__file__
    sys.modules[mod.__name__] = mod
    try:
        exec(compile(src.replace(old, new), f"<mutant {module.__name__}>", "exec"), mod.__dict__)
    finally:
        sys.modules.pop(mod.__name__, None)
    # isinstance / identidade de enum nos testes usam as classes originais
    for name in (*RESULT_CLASSES, "HandoffReason", "InboundMessage", "Conversation"):
        if hasattr(module, name) and hasattr(mod, name):
            setattr(mod, name, getattr(module, name))
    return mod


# ---------- como cada mutante entra no lugar do original ----------

def _install_quote_client(mp, mod):
    for target in (agent.quote_client, test_quote_client, agent.core, sys.modules["tests.conftest"],
                   sys.modules.get("tests.test_qa_security")):
        if target is not None and hasattr(target, "QuoteClient"):
            mp.setattr(target, "QuoteClient", mod.QuoteClient)
    mp.setattr(agent.quote_client, "DEFAULT_TIMEOUTS_S", mod.DEFAULT_TIMEOUTS_S)


def _install_price_guard(mp, mod):
    for target in (agent.price_guard, agent.core, test_price_guard, test_qa_units, test_qa_fuzz):
        if hasattr(target, "has_money"):
            mp.setattr(target, "has_money", mod.has_money)


def _install_pii(mp, mod):
    for name in ("mask", "extract_cep", "find_pii"):
        for target in (agent.pii, agent.core, agent.trace, test_pii, test_qa_units, test_qa_fuzz):
            if hasattr(target, name):
                mp.setattr(target, name, getattr(mod, name))


def _install_handoff(mp, mod):
    for target in (agent.handoff, agent.core, test_qa_units):
        if hasattr(target, "detect_inbound"):
            mp.setattr(target, "detect_inbound", mod.detect_inbound)


def _install_core(mp, mod):
    mp.setattr(agent.core, "Agent", mod.Agent)
    mp.setattr(sys.modules["tests.conftest"], "Agent", mod.Agent)


# ---------- testes reutilizados (chamados direto, com fixtures montadas aqui) ----------

def _make_agent_factory(log_path: Path):
    def _make(replies, quote_responses=()):
        conftest = sys.modules["tests.conftest"]
        tracer = conftest.JsonlTracer(log_path)
        transport, calls = scripted_transport(quote_responses)
        client = conftest.QuoteClient("http://quote", tracer, transport=transport, sleep=lambda _s: None)
        llm = FakeProvider(replies)
        return conftest.Agent(llm, client, tracer, today=lambda: conftest.TODAY), llm, calls
    return _make


def _t(fn: Callable, *args) -> tuple[str, Callable[[Path], None]]:
    shown = [repr(a)[:40] for a in args if not getattr(a, "_fixture", False)]
    name = fn.__module__.split(".")[-1] + "::" + fn.__name__ + (f"[{', '.join(shown)}]" if shown else "")
    return name, (lambda tmp: fn(*[a(tmp) if getattr(a, "_fixture", False) else a for a in args]))


def _fx(builder):
    builder._fixture = True
    return builder


LOG = _fx(lambda tmp: tmp / "m.jsonl")
MAKE = _fx(lambda tmp: _make_agent_factory(tmp / "m.jsonl"))


def _default_timeouts_are_short_short_long(_tmp=None):
    """Teste novo (QA): a ordem default dos timeouts corta a lentidao de 8 s nas 2 primeiras e espera na 3a."""
    seen = []
    import httpx

    def rec(req):
        if req.url.path == "/quote":
            seen.append(req.extensions["timeout"]["read"])
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={})
    agent.quote_client.QuoteClient("http://q", agent.trace.JsonlTracer(_tmp / "t.jsonl"),
                                   transport=httpx.MockTransport(rec), sleep=lambda _s: None
                                   ).quote({}, conversation_id="c")
    assert seen[0] < 8 and seen[1] < 8 and seen[-1] > 8


_CLOSE_COND = "quote_delivered and (_wants_to_close(text) or (followup_asked and _YES_TO_FOLLOWUP.match(text)))"

MUTATIONS = {
    "M1 retry no 422 de recusa": (
        agent.quote_client, "400 <= r.status_code < 500 and", "400 <= r.status_code < 422 and", _install_quote_client,
        [_t(test_quote_client.test_business_refusal_is_final_after_one_attempt, LOG),
         _t(test_quote_client.test_400_and_fastapi_validation_422_are_invalid_and_not_retried, LOG),
         _t(test_core.test_refusal_goes_back_to_llm_and_is_not_a_handoff, MAKE)]),
    "M2 ordem dos timeouts (9,3,3)": (
        agent.quote_client, "DEFAULT_TIMEOUTS_S: tuple[float, ...] = (3.0, 3.0, 9.0)",
        "DEFAULT_TIMEOUTS_S: tuple[float, ...] = (9.0, 3.0, 3.0)", _install_quote_client,
        [_t(test_quote_client.test_connect_error_is_retried_and_last_attempt_outlasts_slow_mode, LOG),
         _t(test_core.test_quote_unavailable_hands_off_with_fixed_text_and_no_price, MAKE),
         ("test_qa_mutation::_default_timeouts_are_short_short_long", _default_timeouts_are_short_short_long)]),
    "M3 has_money sem o ramo R$": (
        agent.price_guard, 'r"R\\s*\\$|', 'r"(?!x)x|', _install_price_guard,
        [_t(test_price_guard.test_currency_symbol_blocks_even_an_echoed_number)]
        + [_t(test_price_guard.test_money_like_numbers_are_blocked, t) for t in
         ["fica R$ 262,38 por mes", "R$ 1.500,00 de franquia", "uns 300 reais"]]
        + [_t(test_qa_units.test_guard_catches_compact_nbsp_fullwidth_and_upper, t) for t in ["R$10", "R$ 199"]]
        + [_t(test_qa_security.test_injection_in_lead_text_cannot_put_a_price_on_the_wire, MAKE,
              "Claro! O seguro custa R$ 10.")]),
    "M4 mask com EMAIL por ultimo": (
        agent.pii, '    ("[EMAIL]", _EMAIL),\n    ("[CARTAO]", _CARD),\n    ("[RG]", _RG),\n    ("[CPF]", _CPF),\n'
                   '    ("[TELEFONE]", _PHONE),\n    ("[CEP]", _CEP),\n    ("[PLACA]", _PLATE),',
        '    ("[CARTAO]", _CARD),\n    ("[RG]", _RG),\n    ("[CPF]", _CPF),\n    ("[TELEFONE]", _PHONE),\n'
        '    ("[CEP]", _CEP),\n    ("[PLACA]", _PLATE),\n    ("[EMAIL]", _EMAIL),', _install_pii,
        [_t(test_pii.test_every_pii_type_is_masked_and_age_and_year_survive),
         _t(test_qa_units.test_email_with_digit_runs_is_masked_whole),
         _t(test_qa_fuzz.test_mask_never_leaves_generated_pii_and_is_idempotent)]),
    "M5 handoff: fechar antes de pedir humano": (
        agent.handoff, "    if _first_unnegated(_HUMAN, text):\n        return HandoffReason.LEAD_ASKED_HUMAN\n"
                       f"    if {_CLOSE_COND}:\n        return HandoffReason.WANTS_TO_CLOSE\n",
        f"    if {_CLOSE_COND}:\n        return HandoffReason.WANTS_TO_CLOSE\n"
        "    if _first_unnegated(_HUMAN, text):\n        return HandoffReason.LEAD_ASKED_HUMAN\n", _install_handoff,
        [_t(test_core.test_media_twice_and_explicit_human_request, MAKE),
         _t(test_core.test_close_and_objection_after_quote_and_tool_loop_limit, MAKE)]
        + [_t(test_qa_units.test_precedence_after_quote, t, r) for t, r in
           [("quero um atendente, pode emitir", agent.handoff.HandoffReason.LEAD_ASKED_HUMAN)]]),
    # M6 ("extract_cep sem limpar CPF/telefone") saiu em 2026-11-23: virou mutacao equivalente. Desde que o CEP
    # so conta formatado ou com "cep" logo antes (pii._looks_like_cep), digitos de CPF/telefone/e-mail ja nao passam;
    # a limpeza ficou como defesa redundante e nenhum teste honesto consegue distinguir as duas versoes.
    "M7 guarda: 1o strike ja vira handoff": (
        agent.core, "if conv.guard_strikes >= 2:", "if conv.guard_strikes >= 1:", _install_core,
        [_t(test_core.test_invented_price_is_replaced_then_second_strike_hands_off, MAKE),
         _t(test_qa_security.test_injection_in_lead_text_cannot_put_a_price_on_the_wire, MAKE, "Sai por 10 reais.")]),
}


def _run(cases, tmp_path):
    caught, passed = [], []
    for i, (name, fn) in enumerate(cases):
        d = tmp_path / f"case{i}"
        d.mkdir()
        try:
            fn(d)
        except Exception:  # noqa: BLE001 - AssertionError ou crash: ambos contam como teste falhando
            caught.append(name)
        else:
            passed.append(name)
    return caught, passed


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_control_copy_passes(mutation, tmp_path, monkeypatch):
    module, old, _new, install, cases = MUTATIONS[mutation]
    install(monkeypatch, _mutant(module, old, old))
    caught, _ = _run(cases, tmp_path)
    assert caught == [], f"com a copia SEM mutacao estes testes ja falham: {caught}"


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_mutation_is_caught(mutation, tmp_path, monkeypatch):
    module, old, new, install, cases = MUTATIONS[mutation]
    install(monkeypatch, _mutant(module, old, new))
    caught, passed = _run(cases, tmp_path)
    print(f"\n[mutacao] {mutation}: pego por {len(caught)}/{len(cases)} -> {caught}")
    assert caught, f"mutacao sobreviveu: nenhum teste falhou ({passed})"


def test_mutations_never_touch_disk():
    for module in (agent.quote_client, agent.price_guard, agent.pii, agent.handoff, agent.core):
        src = Path(module.__file__).read_text("utf-8")
        assert "_mutant" not in src
    assert (ROOT / "agent/quote_client.py").read_text("utf-8").count("(3.0, 3.0, 9.0)") == 1
