from __future__ import annotations

import httpx
import pytest

from agent.quote_client import QuoteClient, QuoteInvalid, QuoteOk, QuoteRefused, QuoteUnavailable
from agent.trace import JsonlTracer

from .conftest import QUOTE_OK, events, scripted_transport

PAYLOAD = {"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "cep": "01310-100"}


def _client(log_path, responses, timeouts=(3.0, 3.0, 9.0)):
    transport, calls = scripted_transport(responses)
    seen_timeouts: list[float] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/quote":
            seen_timeouts.append(request.extensions["timeout"]["read"])
        return transport.handle_request(request)

    client = QuoteClient("http://quote", JsonlTracer(log_path), timeouts_s=timeouts,
                         transport=httpx.MockTransport(recording), sleep=lambda _s: None)
    return client, calls, seen_timeouts


def test_transient_5xx_then_success_retries_with_one_quote_id(log_path):
    client, calls, _ = _client(log_path, [httpx.Response(503), httpx.Response(503), httpx.Response(200, json=QUOTE_OK)])
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteOk) and r.attempts == 3 and r.data["premio_mensal"] == 272.87
    attempts = [e for e in events(log_path) if e["event"] == "quote_attempt"]
    assert [e["status"] for e in attempts] == [503, 503, 200]
    assert {e["quote_id"] for e in attempts} == {r.quote_id}


def test_timeouts_everywhere_end_unavailable_without_raising(log_path):
    client, calls, _ = _client(log_path, [httpx.ReadTimeout("slow")] * 3)
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteUnavailable) and r.attempts == 3 and r.last_error == "timeout"
    assert len(calls) == 3


def test_business_refusal_is_final_after_one_attempt(log_path):
    body = {"error": "cotacao_recusada", "motivo": "Veiculo com mais de 20 anos nao e aceito."}
    client, calls, _ = _client(log_path, [httpx.Response(422, json=body)])
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteRefused) and r.motivo == body["motivo"] and r.attempts == 1
    assert len(calls) == 1


def test_400_and_fastapi_validation_422_are_invalid_and_not_retried(log_path):
    client, calls, _ = _client(log_path, [httpx.Response(400, json={"error": "payload_invalido"})])
    assert isinstance(client.quote(PAYLOAD, conversation_id="c1"), QuoteInvalid)
    assert len(calls) == 1

    client, calls, _ = _client(log_path, [httpx.Response(422, json={"detail": [{"loc": ["body", "idade"]}]})])
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteInvalid) and r.attempts == 1
    assert len(calls) == 1


def test_connect_error_is_retried_and_last_attempt_outlasts_slow_mode(log_path):
    client, calls, timeouts = _client(
        log_path, [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(200, json=QUOTE_OK)])
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteOk) and r.attempts == 3
    assert timeouts[:2] == [3.0, 3.0]
    assert timeouts[-1] > 8.0  # QUOTE_SLOW_SECONDS default do mock


def test_200_with_broken_pro_rata_is_retried_not_crashed(log_path):
    """Regressao do review: pro-rata incompleto passava a validacao e derrubava format_quote."""
    broken = {**QUOTE_OK, "primeiro_pagamento_pro_rata": {"dias_no_mes": 30}}
    client, calls, _ = _client(log_path, [httpx.Response(200, json=broken), httpx.Response(200, json=QUOTE_OK)])
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteOk) and r.attempts == 2


def test_transient_4xx_and_decoding_errors_are_retried(log_path):
    """Regressao do code-review: 429/408 do gateway iam direto ao humano; DecodingError derrubava a CLI."""
    client, calls, _ = _client(log_path, [httpx.Response(429), httpx.DecodingError("bad gzip"),
                                          httpx.Response(200, json=QUOTE_OK)])
    r = client.quote(PAYLOAD, conversation_id="c1")
    assert isinstance(r, QuoteOk) and r.attempts == 3


def test_200_with_broken_carencia_or_coverages_is_not_accepted(log_path):
    for broken in ({**QUOTE_OK, "carencia": {"coberturas": ["roubo"]}}, {**QUOTE_OK, "coberturas": ["colisao", 3]}):
        client, _, _ = _client(log_path, [httpx.Response(200, json=broken)] * 3)
        r = client.quote(PAYLOAD, conversation_id="c1")
        assert isinstance(r, QuoteUnavailable) and r.last_error == "200_malformed"


@pytest.mark.parametrize("bad", [0, -10, True, float("nan")])
def test_200_with_non_positive_or_bool_or_nan_premium_is_not_a_quote(log_path, bad):
    """Regressao do code-review do novo ciclo: 'R$ 0,00' ou 'R$ nan' sairiam como preco."""
    body = {**QUOTE_OK, "premio_mensal": bad}
    client, _, _ = _client(log_path, [httpx.Response(200, content=__import__("json").dumps(body).encode(),
                                                     headers={"content-type": "application/json"})] * 3)
    assert isinstance(client.quote(PAYLOAD, conversation_id="c1"), QuoteUnavailable)


def test_zero_deductible_is_a_valid_quote(log_path):
    client, _, _ = _client(log_path, [httpx.Response(200, json={**QUOTE_OK, "franquia": 0})])
    assert isinstance(client.quote(PAYLOAD, conversation_id="c1"), QuoteOk)
