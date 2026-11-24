from __future__ import annotations

import httpx

from agent.core import InboundMessage
from agent.trace import JsonlTracer

from .conftest import COTAR, QUOTE_OK, call, events, say


def test_every_event_is_traceable_and_log_has_no_raw_pii(make_agent, log_path):
    agent, _, _ = make_agent([say("Anotado!"), call("cotar", **COTAR), say("Quer fechar?")],
                             [httpx.Response(502), httpx.Response(200, json=QUOTE_OK)])
    conv = agent.start()
    agent.handle(conv, InboundMessage("Cpf 529.982.247-25, tenho 35 anos, cep 07310-100, placa ABC1D23"))
    agent.handle(conv, InboundMessage("cota o completo"))
    agent.end(conv)

    evs = events(log_path)
    assert all(e.get("conversation_id") == conv.id and e.get("ts") for e in evs)
    for e in evs:
        if e["event"] in {"message_in", "message_out"}:
            assert e["message_id"].startswith("m_")
        if e["event"] == "quote_attempt":
            assert {"quote_id", "attempt", "status", "latency_ms"} <= e.keys()
    assert [e["status"] for e in evs if e["event"] == "quote_attempt"] == [502, 200]
    assert evs[-1]["event"] == "conversation_end" and evs[-1]["status"] == "resolvida"

    raw = log_path.read_text("utf-8")
    for secret in ["529.982.247-25", "07310-100", "ABC1D23"]:
        assert secret not in raw


def test_ids_with_digit_runs_are_not_masked(log_path):
    """Regressao: na 1a execucao real o id m_ee2d<8 digitos> virou m_ee2d[CEP] no log."""
    JsonlTracer(log_path).emit("message_in", conversation_id="c_123456789abc", message_id="m_ee2d12345678",
                               quote_id="q_0012345678ab", reply_to="m_99887766aaaa", text="cep 07310-100")
    e = events(log_path)[0]
    assert (e["conversation_id"], e["message_id"], e["quote_id"], e["reply_to"]) == (
        "c_123456789abc", "m_ee2d12345678", "q_0012345678ab", "m_99887766aaaa")
    assert e["text"] == "cep [CEP]"


def test_lists_of_ids_are_not_masked(log_path):
    """Regressao do review: conversation_end.quote_ids com 8 digitos seguidos virava q_ab[CEP]cd."""
    JsonlTracer(log_path).emit("conversation_end", conversation_id="c", quote_ids=["q_ab12345678cd"])
    assert events(log_path)[0]["quote_ids"] == ["q_ab12345678cd"]
