"""Rastreio: um evento por linha (JSONL), sempre com conversation_id.

Mensagens levam message_id; cada cotacao leva quote_id e cada tentativa HTTP vira
um evento proprio (attempt, status, latency_ms). Todo texto passa por pii.mask
antes de ser gravado, em qualquer profundidade do evento.

Eventos: conversation_start, message_in, message_out, llm_call, tool_call,
quote_attempt, quote_result, guard_blocked, handoff, conversation_end.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any, Protocol

from .pii import mask


class Tracer(Protocol):
    def emit(self, event: str, **fields: Any) -> None: ...


class JsonlTracer:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"), "event": event,
                  **{k: v if _is_id(k) else _masked(v) for k, v in fields.items()}}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _is_id(key: str) -> bool:
    """Ids sao gerados pelo sistema (hex aleatorio), nunca dado do lead; mascarar quebraria o
    rastreio quando o hex tem 8 digitos seguidos (parece CEP)."""
    return key.endswith(("_id", "_ids")) or key == "reply_to"


def _masked(v: Any) -> Any:
    if isinstance(v, str):
        return mask(v)
    if isinstance(v, dict):
        return {k: _masked(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_masked(x) for x in v]
    return v
