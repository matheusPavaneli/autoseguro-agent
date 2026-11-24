"""QA: orcamento de latencia no pior caso e concorrencia do tracer."""
from __future__ import annotations

import json
import random
import threading
import time

import httpx

from agent.pii import mask
from agent.quote_client import DEFAULT_TIMEOUTS_S, QuoteClient, QuoteOk, QuoteUnavailable
from agent.trace import JsonlTracer

from .qa_support import PORTS, _port_free, quote_service

PAYLOAD = {"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "cep": "01310-100"}


def test_worst_case_budget_with_default_settings_is_16_5_to_17_s(log_path):
    """Toda tentativa estoura o timeout: custo = soma dos timeouts + backoff com jitter (calculado, sem esperar)."""
    sleeps: list[float] = []

    def always_timeout(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/planos":
            return httpx.Response(200, json={})
        raise httpx.ReadTimeout("slow")

    for seed in range(200):
        sleeps.clear()
        c = QuoteClient("http://q", JsonlTracer(log_path), transport=httpx.MockTransport(always_timeout),
                        sleep=sleeps.append, rng=random.Random(seed))
        assert isinstance(c.quote(PAYLOAD, conversation_id="c"), QuoteUnavailable)
        budget = sum(DEFAULT_TIMEOUTS_S) + sum(sleeps)
        assert len(sleeps) == 2 and 16.5 <= budget <= 17.0, budget


def test_scaled_worst_case_measured_against_real_slow_server(log_path):
    """Mesmo desenho, escala 1/15, com o servico real lento: o tempo medido bate com a soma."""
    timeouts, backoff = (0.2, 0.2, 0.6), 0.05
    with quote_service(failure_rate=0.0, slow_rate=1.0, slow_seconds=3) as url:
        c = QuoteClient(url, JsonlTracer(log_path), timeouts_s=timeouts, backoff_base_s=backoff)
        t0 = time.perf_counter()
        r = c.quote(PAYLOAD, conversation_id="c")
        elapsed = time.perf_counter() - t0
    assert isinstance(r, QuoteUnavailable) and r.last_error == "timeout"
    floor = sum(timeouts) + backoff * 3  # 0,05 + 0,10 sem jitter
    assert floor <= elapsed <= floor + 3.0, elapsed  # folga para CI lento (code-review)


def test_connection_refused_is_bounded_by_the_same_budget(log_path):
    port = next(p for p in reversed(PORTS) if _port_free(p))
    c = QuoteClient(f"http://127.0.0.1:{port}", JsonlTracer(log_path))
    t0 = time.perf_counter()
    r = c.quote(PAYLOAD, conversation_id="c")
    elapsed = time.perf_counter() - t0
    assert isinstance(r, QuoteUnavailable) and r.last_error.startswith("conn_error")
    assert elapsed <= 17.5, elapsed
    print(f"\n[perf] conexao recusada: {elapsed:.2f} s para 3 tentativas")


def test_ok_path_overhead_is_small(log_path):
    with quote_service() as url:
        c = QuoteClient(url, JsonlTracer(log_path))
        lat = []
        for _ in range(20):
            t0 = time.perf_counter()
            assert isinstance(c.quote(PAYLOAD, conversation_id="c"), QuoteOk)
            lat.append(time.perf_counter() - t0)
    lat.sort()
    print(f"\n[perf] /quote ok p50 {lat[10] * 1000:.0f} ms, max {lat[-1] * 1000:.0f} ms")
    assert lat[10] < 1.5  # folga para CI lento (code-review)


def test_tracer_threads_never_interleave_lines(log_path):
    tracer = JsonlTracer(log_path)
    n_threads, per_thread, filler = 16, 300, "palavra " * 400 + " cpf 529.982.247-25 "

    def work(t: int) -> None:
        for i in range(per_thread):
            tracer.emit("message_in", conversation_id=f"c_{t}", seq=i, text=filler)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    lines = log_path.read_text("utf-8").splitlines()
    assert len(lines) == n_threads * per_thread
    seen: dict[str, list[int]] = {}
    for line in lines:
        e = json.loads(line)  # linha intercalada nao seria JSON valido
        assert "529.982.247-25" not in e["text"]
        seen.setdefault(e["conversation_id"], []).append(e["seq"])
    assert all(v == list(range(per_thread)) for v in seen.values())


# regressao QA 2026-11-23 (era xfail, corrigido): _EMAIL em pii.py e quadratico num token longo sem espaco (ReDoS):  3
def test_mask_is_linear_on_long_tokens():
    t0 = time.perf_counter()
    mask("a" * 30_000)
    assert time.perf_counter() - t0 < 0.15  # medido 2 ms; a regex quadratica levava 360 ms (mask corta em 10k)
