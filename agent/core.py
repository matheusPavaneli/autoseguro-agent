"""O agente: estado da conversa, loop LLM <-> ferramentas, handoff e guarda de preco.

Divisao de responsabilidade:
  - o LLM conversa e extrai dados do texto livre (modelo/ano, idade, plano, data);
  - o CODIGO decide: quando passar para o humano (agent/handoff.py), o que vai a API,
    e o texto da cotacao que o lead recebe (format_quote). O LLM nunca escreve preco.

Depende so de abstracoes: LLMProvider (agent/llm/base.py), QuoteGateway e Tracer.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .handoff import AFTER_HANDOFF_MESSAGE, ASK_TEXT_MESSAGE, HANDOFF_MESSAGES, HandoffReason, detect_inbound
from .llm.base import ChatMessage, LLMError, LLMProvider, ToolCall
from .pii import cep_candidates, mask
from .price_guard import SAFE_MESSAGE, SAFE_MESSAGE_NO_QUOTE, has_money, numbers_in
from .quote_client import QuoteGateway, QuoteInvalid, QuoteOk, QuoteRefused, QuoteUnavailable
from .trace import Tracer

_NEEDS_QUOTE = {HandoffReason.WANTS_TO_CLOSE, HandoffReason.PRICE_OBJECTION}
_LLM_REASON_MAP = {
    "pedido_do_lead": HandoffReason.LEAD_ASKED_HUMAN,
    "quer_fechar": HandoffReason.WANTS_TO_CLOSE,
    "objecao_preco": HandoffReason.PRICE_OBJECTION,
    "fora_do_escopo": HandoffReason.OUT_OF_SCOPE,
}

QUOTE_FOLLOWUP = "Quer seguir com esse plano ou prefere que eu cote outro?"
_INVISIBLE = re.compile(r"[​-‏⁠﻿]")


@dataclass(frozen=True)
class InboundMessage:
    text: str
    type: str = "text"  # text | image | audio | document


@dataclass
class Conversation:
    id: str
    history: list[ChatMessage] = field(default_factory=list)  # so texto mascarado
    cep: str | None = None
    cep_ambiguous: bool = False  # ultima mensagem trouxe mais de um CEP: o LLM pergunta qual
    lead_numbers: frozenset[str] = frozenset()  # numeros que o lead digitou (texto mascarado)
    quotes: list[QuoteOk] = field(default_factory=list)
    handoff: HandoffReason | None = None
    previous_was_media: bool = False
    refusals: list[str] = field(default_factory=list)  # quote_ids recusados por regra
    guard_strikes: int = 0

    @property
    def status(self) -> str:
        """resolvida = cotacao entregue ou recusa explicada sem humano."""
        if self.handoff:
            return "handoff"
        return "resolvida" if self.quotes or self.refusals else "aberta"


class _Handoff(Exception):
    def __init__(self, reason: HandoffReason, detail: str = "") -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.detail = detail


class Agent:
    def __init__(self, llm: LLMProvider, quotes: QuoteGateway, tracer: Tracer, *,
                 today: Callable[[], dt.date] = dt.date.today, max_tool_rounds: int = 4) -> None:
        self._llm = llm
        self._quotes = quotes
        self._tracer = tracer
        self._today = today
        self._max_rounds = max_tool_rounds
        self._plans = quotes.plans()
        self._tools = prompts.tools(self._plans)

    # ---------- API publica ----------

    def start(self) -> Conversation:
        conv = Conversation(id=f"c_{uuid.uuid4().hex[:12]}")
        self._tracer.emit("conversation_start", conversation_id=conv.id, llm=self._llm.name)
        return conv

    def end(self, conv: Conversation) -> None:
        self._tracer.emit("conversation_end", conversation_id=conv.id, status=conv.status,
                          handoff_reason=conv.handoff.value if conv.handoff else None,
                          quote_ids=[q.quote_id for q in conv.quotes])

    def handle(self, conv: Conversation, msg: InboundMessage) -> list[str]:
        """Processa uma mensagem do lead e devolve as mensagens a enviar, em ordem."""
        message_id = f"m_{uuid.uuid4().hex[:12]}"
        self._tracer.emit("message_in", conversation_id=conv.id, message_id=message_id,
                          type=msg.type, text=msg.text)
        out: list[str] = []
        try:
            self._process(conv, msg, message_id, out)
        except _Handoff as h:
            conv.handoff = h.reason
            self._tracer.emit("handoff", conversation_id=conv.id, message_id=message_id,
                              reason=h.reason.value, detail=h.detail)
            out.append(HANDOFF_MESSAGES[h.reason])
        for text in out:
            self._tracer.emit("message_out", conversation_id=conv.id, message_id=f"m_{uuid.uuid4().hex[:12]}",
                              reply_to=message_id, text=text)
        return out

    # ---------- passos ----------

    def _process(self, conv: Conversation, msg: InboundMessage, message_id: str, out: list[str]) -> None:
        if conv.handoff:
            out.append(AFTER_HANDOFF_MESSAGE)
            return
        media = msg.type != "text"
        last_out = next((m.content for m in reversed(conv.history) if m.role == "assistant" and m.content), None)
        reason = detect_inbound(msg.text, media=media, previous_was_media=conv.previous_was_media,
                                quote_delivered=bool(conv.quotes), followup_asked=last_out == QUOTE_FOLLOWUP)
        conv.previous_was_media = media
        if reason:
            raise _Handoff(reason, "regra deterministica")
        if media:
            conv.history.append(ChatMessage("user", f"[lead enviou {msg.type}, sem texto]"))
            conv.history.append(ChatMessage("assistant", ASK_TEXT_MESSAGE))
            out.append(ASK_TEXT_MESSAGE)
            return

        # mensagem que e SO o CEP (resposta a "qual o CEP?") vale mesmo sem hifen
        ceps = cep_candidates(msg.text, alone_ok=True)
        if len(ceps) == 1:
            # cep_candidates so aceita CEP formatado ou com "cep" logo antes: uma correcao "07123-000" troca,
            # 8 digitos soltos (RG, telefone fixo, protocolo) nao
            conv.cep, conv.cep_ambiguous = ceps[0], False
        elif len(ceps) > 1:
            # escolher errado tira o agravo de regiao (cota barato demais): nao escolhe, pergunta
            conv.cep, conv.cep_ambiguous = None, True
        masked = mask(msg.text)
        conv.lead_numbers = conv.lead_numbers | numbers_in([masked])
        conv.history.append(ChatMessage("user", masked))

        for _ in range(self._max_rounds):
            reply = self._call_llm(conv, message_id)
            if not reply.tool_calls:
                if not _INVISIBLE.sub("", reply.text or "").strip():
                    continue  # resposta vazia: tenta outra rodada (conta no limite de rodadas)
                out.append(self._guard(conv, message_id, reply.text or ""))
                return
            conv.history.append(ChatMessage("assistant", reply.text, tool_calls=reply.tool_calls))
            quotes_before = len(conv.quotes)
            for call in reply.tool_calls:
                result = self._run_tool(conv, call, message_id, out)
                conv.history.append(ChatMessage("tool", json.dumps(result, ensure_ascii=False), tool_call_id=call.id))
            if len(conv.quotes) > quotes_before:
                # cotacao entregue pelo codigo: o seguimento tambem e fixo, sem nova chamada ao LLM
                conv.guard_strikes = 0  # turno terminou sem bloqueio: dois bloqueios so contam se seguidos
                conv.history.append(ChatMessage("assistant", QUOTE_FOLLOWUP))
                out.append(QUOTE_FOLLOWUP)
                return
        raise _Handoff(HandoffReason.TOOL_LOOP_EXHAUSTED, f"{self._max_rounds} rodadas sem resposta final")

    def _call_llm(self, conv: Conversation, message_id: str):
        cep_status = "sim" if conv.cep else ("ambiguo" if conv.cep_ambiguous else "nao")
        system = prompts.system_prompt(self._plans, self._today(), cep_status=cep_status)
        messages = [ChatMessage("system", system), *conv.history]
        t0 = time.perf_counter()
        try:
            reply = self._llm.complete(messages, self._tools)
        except LLMError as e:
            self._tracer.emit("llm_call", conversation_id=conv.id, message_id=message_id, status="error",
                              latency_ms=round((time.perf_counter() - t0) * 1000), error=str(e)[:300])
            raise _Handoff(HandoffReason.LLM_UNAVAILABLE, str(e)[:300]) from e
        self._tracer.emit("llm_call", conversation_id=conv.id, message_id=message_id, status="ok",
                          latency_ms=round((time.perf_counter() - t0) * 1000),
                          tool_calls=[c.name for c in reply.tool_calls])
        return reply

    def _guard(self, conv: Conversation, message_id: str, text: str) -> str:
        text = _INVISIBLE.sub("", text).strip()
        year = self._today().year
        if has_money(text, conv.lead_numbers | {str(year), str(year + 1)}):  # ano de inicio da vigencia
            conv.guard_strikes += 1
            self._tracer.emit("guard_blocked", conversation_id=conv.id, message_id=message_id,
                              strikes=conv.guard_strikes, text=text)
            if conv.guard_strikes >= 2:
                raise _Handoff(HandoffReason.UNSAFE_REPLY, "LLM escreveu valor monetario duas vezes seguidas")
            text = SAFE_MESSAGE if conv.quotes else SAFE_MESSAGE_NO_QUOTE
        else:
            conv.guard_strikes = 0
        conv.history.append(ChatMessage("assistant", text))
        return text

    def _run_tool(self, conv: Conversation, call: ToolCall, message_id: str, out: list[str]) -> dict[str, Any]:
        self._tracer.emit("tool_call", conversation_id=conv.id, message_id=message_id, tool=call.name,
                          arguments=call.arguments)
        if call.name == "encaminhar_humano":
            reason = _LLM_REASON_MAP.get(str(call.arguments.get("motivo")), HandoffReason.OUT_OF_SCOPE)
            if reason in _NEEDS_QUOTE and not conv.quotes and not conv.refusals:  # recusado: nao ha o que recotar
                # mesma regra do handoff deterministico: sem cotacao, "caro"/"quero fechar" ainda e qualificacao
                return {"erro": "sem_cotacao_ainda",
                        "instrucao": "ainda nao ha cotacao; continue qualificando e cote antes de encaminhar"}
            raise _Handoff(reason, "decisao do LLM")
        if call.name == "cotar":
            return self._quote(conv, call.arguments, message_id, out)
        return {"erro": f"ferramenta desconhecida: {call.name}"}

    def _quote(self, conv: Conversation, args: dict[str, Any], message_id: str, out: list[str]) -> dict[str, Any]:
        today = self._today()
        plano = str(args.get("plano_id", "")).lower()
        if plano not in prompts.plan_ids(self._plans):
            return {"erro": "plano_invalido", "opcoes": prompts.plan_ids(self._plans)}
        try:
            idade = int(args["idade"])
            ano = int(args["veiculo_ano"])
            inicio = dt.date.fromisoformat(str(args["data_inicio"]))
        except (KeyError, TypeError, ValueError, OverflowError):  # OverflowError: int(inf) de '1e400'
            return {"erro": "dados_incompletos_ou_invalidos",
                    "instrucao": "confirme idade, ano do veiculo (4 digitos) e data de inicio AAAA-MM-DD"}
        if inicio < today:
            return {"erro": "data_inicio_no_passado", "hoje": today.isoformat()}
        # faixas do schema da API (main.py): fora delas a API devolve 422 de validacao, que nao e
        # instabilidade; o erro volta ao LLM para ele confirmar o dado com o lead
        if not 0 <= idade <= 120:
            return {"erro": "idade_invalida", "instrucao": "confirme a idade do lead em anos"}
        if not 1950 <= ano <= today.year + 1:
            return {"erro": "ano_do_veiculo_invalido", "instrucao": "confirme o ano do veiculo com 4 digitos"}
        if not conv.cep:
            return {"erro": "falta_cep", "instrucao": "peca o CEP de onde o carro dorme"}
        # ano-modelo pode ser o ano seguinte; a API calcula idade = ano corrente - ano (fica negativa)
        ano_api = min(ano, today.year)
        payload = {"plano_id": plano, "idade": idade, "veiculo_ano": ano_api, "cep": conv.cep,
                   "data_inicio": inicio.isoformat()}

        result = self._quotes.quote(payload, conversation_id=conv.id)
        self._tracer.emit("quote_result", conversation_id=conv.id, message_id=message_id,
                          quote_id=result.quote_id, result=type(result).__name__, attempts=result.attempts,
                          premio_mensal=result.data.get("premio_mensal") if isinstance(result, QuoteOk) else None)
        match result:
            case QuoteOk():
                text = format_quote(result.data)  # formata ANTES de registrar: falha aqui nao deixa cotacao fantasma
                conv.quotes.append(result)
                out.append(text)
                # o LLM precisa saber O QUE foi cotado para responder duvidas depois, mas nunca os valores
                car = result.data.get("carencia") or {}
                return {"status": "cotacao_ja_enviada_ao_lead", "plano": result.data.get("plano_nome"),
                        "coberturas": result.data.get("coberturas"),
                        "carencia": {"coberturas": car.get("coberturas", []), "dias": car.get("dias")},
                        "primeiro_pagamento_proporcional": "primeiro_pagamento_pro_rata" in result.data,
                        "instrucao": "Nao repita valores; para valor, aponte a cotacao enviada."}
            case QuoteRefused():
                conv.refusals.append(result.quote_id)
                return {"status": "recusada", "motivo": result.motivo}
            case QuoteInvalid():
                raise _Handoff(HandoffReason.INVALID_REQUEST, result.detalhe)
            case QuoteUnavailable():
                raise _Handoff(HandoffReason.QUOTE_UNAVAILABLE,
                               f"{result.attempts} tentativas, ultimo erro {result.last_error}")


def brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_quote(d: dict[str, Any]) -> str:
    """Texto da cotacao, montado so com o que a API devolveu."""
    cob = ", ".join(prompts.COVERAGE_LABELS.get(c, c) for c in d["coberturas"])
    lines = [f"Cotacao do plano {d['plano_nome']}:",
             f"- Mensalidade: {brl(d['premio_mensal'])}",
             f"- Franquia: {brl(d['franquia'])}",
             f"- Coberturas: {cob}"]
    car = d.get("carencia") or {}
    if car.get("coberturas"):
        lines.append(f"- Carencia: {', '.join(car['coberturas'])} so passam a valer {car['dias']} dias "
                     "apos o inicio da vigencia.")
    pr = d.get("primeiro_pagamento_pro_rata")
    if pr:
        lines.append(f"- Primeiro pagamento proporcional ({pr['dias_cobrados']} de {pr['dias_no_mes']} dias): "
                     f"{brl(pr['valor_primeiro_pagamento'])}; depois {brl(d['premio_mensal'])} por mes.")
    return "\n".join(lines)
