"""Terminal simulando o WhatsApp.

  uv run --env-file .env python -m agent.cli                         # conversa interativa
  uv run --env-file .env python -m agent.cli --script scripts/demo_feliz.txt --name demo_feliz

Grava logs/<nome>.jsonl (rastreio, ja mascarado) e logs/<nome>.md (transcricao gerada
a partir do jsonl, entao tambem mascarada). Linhas que comecam com [audio], [documento]
ou [imagem] simulam midia; linhas vazias e com # sao ignoradas no roteiro.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx

from .core import Agent, InboundMessage
from .llm.factory import ConfigError, build_provider
from .quote_client import QuoteClient
from .trace import JsonlTracer

_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MEDIA = re.compile(r"^\[(audio|documento|imagem)\]", re.IGNORECASE)
_MEDIA_TYPE = {"audio": "audio", "documento": "document", "imagem": "image"}


def parse_line(line: str) -> InboundMessage:
    m = _MEDIA.match(line)
    return InboundMessage(line, _MEDIA_TYPE[m.group(1).lower()]) if m else InboundMessage(line)


def _script_lines(path: Path) -> Iterator[str]:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def _interactive() -> Iterator[str]:
    while True:
        try:
            line = input("Lead> ").strip()
        except EOFError:
            return
        if line.lower() in {"sair", "exit", "quit"}:
            return
        if line:
            yield line


def render_transcript(jsonl: Path) -> str:
    events = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines() if x.strip()]
    out = [f"# Transcricao: {jsonl.stem}", "",
           f"Gerado de `{jsonl.name}` (dados pessoais ja mascarados). Eventos tecnicos em _italico_.", ""]
    for e in events:
        match e["event"]:
            case "conversation_start":
                out.append(f"_conversa {e['conversation_id']} iniciada em {e['ts']} (LLM: {e['llm']})_\n")
            case "message_in":
                # texto do lead numa linha so: quebra de linha nao pode forjar uma linha "**Agente**:"
                lead_text = " / ".join(x.strip() for x in e["text"].splitlines() if x.strip())
                out.append(f"**Lead** ({e['type']}, {e['message_id']}): {lead_text}\n")
            case "message_out":
                out.append("**Agente**: " + e["text"].replace("\n", "  \n") + "\n")
            case "quote_attempt":
                out.append(f"_cotacao {e['quote_id']} tentativa {e['attempt']} (timeout {e['timeout_s']} s): "
                           f"status {e['status']} em {e['latency_ms']} ms_\n")
            case "quote_result":
                out.append(f"_cotacao {e['quote_id']}: {e['result']} apos {e['attempts']} tentativa(s)_\n")
            case "guard_blocked":
                out.append("_guarda de preco bloqueou uma resposta do LLM_\n")
            case "handoff":
                out.append(f"_handoff: {e['reason']} ({e['detail']})_\n")
            case "conversation_end":
                out.append(f"_fim: status {e['status']}, handoff {e['handoff_reason']}, cotacoes {e['quote_ids']}_\n")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agente AutoSeguro no terminal")
    ap.add_argument("--script", type=Path, help="roteiro com uma mensagem do lead por linha")
    ap.add_argument("--name", default=None, help="nome do log em logs/ (default: run-<timestamp>)")
    a = ap.parse_args(argv)
    # console do Windows (cp1252) quebra em acento/emoji vindo do LLM
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    name = a.name or f"run-{dt.datetime.now():%Y%m%d-%H%M%S}"
    if not _SAFE_NAME.fullmatch(name):
        print("erro: --name aceita so letras, numeros, _ e - (o log fica sempre em logs/)", file=sys.stderr)
        return 2
    if a.script and not a.script.is_file():
        print(f"erro: roteiro nao encontrado: {a.script}", file=sys.stderr)
        return 2
    try:
        llm = build_provider()
    except ConfigError as e:
        print(f"erro de configuracao: {e}", file=sys.stderr)
        return 2

    log = Path("logs") / f"{name}.jsonl"
    tracer = JsonlTracer(log)  # so escreve no primeiro evento; montar o Agent nao emite nada
    quote_url = os.getenv("QUOTE_URL", "http://localhost:8000")
    try:
        agent = Agent(llm, QuoteClient(quote_url, tracer), tracer)
    except httpx.HTTPError as e:
        print(f"erro: API de cotacao indisponivel em {quote_url} ({type(e).__name__}); suba o quote-service",
              file=sys.stderr)
        return 3  # o log anterior com o mesmo nome fica intacto
    log.unlink(missing_ok=True)

    conv = agent.start()
    lines = _script_lines(a.script) if a.script else _interactive()
    for line in lines:
        if a.script:
            print(f"Lead> {line}")
        for reply in agent.handle(conv, parse_line(line)):
            print(f"Agente> {reply}\n")
    agent.end(conv)

    md = log.with_suffix(".md")
    md.write_text(render_transcript(log), encoding="utf-8")
    print(f"[status {conv.status}] log: {log}  transcricao: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
