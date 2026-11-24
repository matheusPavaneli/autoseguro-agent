"""Apoio dos testes de QA: sobe o quote-service REAL num processo proprio e calcula o
premio esperado de forma independente (a partir do plans.json, nao do codigo do servico).

O servico le QUOTE_FAILURE_RATE etc. no import, entao cada configuracao e um processo.
Portas 8001-8008 (8000 e da sessao principal; 8009 fica para scripts/qa_live.py).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = ROOT / "quote-service"
PLANS: dict[str, Any] = json.loads((SERVICE_DIR / "data/plans.json").read_text("utf-8"))
PORTS = range(8001, 8009)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _service_cmd(port: int) -> list[str]:
    venv_py = SERVICE_DIR / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if venv_py.exists():
        # mesmo interpretador que `uv run` usaria, mas sem processo intermediario (teardown limpo)
        return [str(venv_py), "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"]
    return ["uv", "run", "-q", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"]


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


@contextlib.contextmanager
def quote_service(failure_rate: float = 0.0, slow_rate: float = 0.0, slow_seconds: float = 8.0,
                  seed: int | None = None) -> Iterator[str]:
    """Sobe o quote-service real e devolve a base URL; derruba no fim."""
    port = next((p for p in PORTS if _port_free(p)), None)
    if port is None:
        raise RuntimeError("nenhuma porta livre em 8001-8008")
    env = {**os.environ, "QUOTE_FAILURE_RATE": str(failure_rate), "QUOTE_SLOW_RATE": str(slow_rate),
           "QUOTE_SLOW_SECONDS": str(slow_seconds)}
    env.pop("QUOTE_SEED", None)
    if seed is not None:
        env["QUOTE_SEED"] = str(seed)
    proc = subprocess.Popen(_service_cmd(port), cwd=SERVICE_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 40
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"quote-service saiu com codigo {proc.returncode}")
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("quote-service nao respondeu /health em 40 s")
            time.sleep(0.2)
        yield url
    finally:
        _kill(proc)


# ---------- regra de negocio, recalculada de forma independente ----------

def _band(rules: list[dict[str, Any]], value: int, lo: str, hi: str) -> float | None:
    for f in rules:
        if f[lo] <= value <= f[hi]:
            return None if f.get("recusar") else f["multiplicador"]
    return None


def expected_premium(plano_id: str, idade: int, veiculo_ano: int, cep: str, year: int | None = None) -> float | None:
    """None = recusa esperada. Mesma ordem de multiplicacao da regra publicada (base * idade * veiculo * regiao)."""
    year = dt.date.today().year if year is None else year
    r = PLANS["regras"]
    plano = next(p for p in PLANS["planos"] if p["id"] == plano_id)
    m_idade = _band(r["faixa_etaria"], idade, "idade_min", "idade_max")
    m_veic = _band(r["idade_veiculo"], year - veiculo_ano, "anos_min", "anos_max")
    if m_idade is None or m_veic is None:
        return None
    high = cep.replace("-", "")[:2] in r["regiao_cep"]["prefixos_alto_risco"]
    m_reg = r["regiao_cep"]["multiplicador"] if high else 1.0
    return round(plano["base_mensal"] * m_idade * m_veic * m_reg, 2)


def free_port_count() -> int:
    return sum(_port_free(p) for p in PORTS)


__all__ = ["PLANS", "expected_premium", "quote_service", "free_port_count"]
