"""Criterio de passar para o humano: explicito, em codigo, um motivo por caso.

O agente RESOLVE sozinho: qualificar, cotar, explicar a cotacao (coberturas, franquia,
carencia, pro-rata), explicar uma recusa por regra (idade > 75, veiculo > 20 anos) e
tirar duvidas sobre os planos.

O agente PASSA para o humano quando:
  - nao consegue entregar a cotacao com seguranca (API fora, payload nosso errado,
    LLM fora, loop de ferramentas sem fim, resposta insegura repetida);
  - o lead pede um humano;
  - o lead insiste em conteudo que o agente nao le (2a midia seguida sem texto);
  - a conversa entra no que so o vendedor pode fazer: fechar (emissao exige
    pagamento e documentos) ou negociar preco (desconto/ajuste e decisao comercial);
  - o LLM, para o que as regras nao cobrem: pedido de humano com outras palavras, fechamento
    ou objecao ditos de outro jeito, assunto fora de cotacao (ferramenta encaminhar_humano).

Fechamento e objecao so contam DEPOIS de uma cotacao entregue, venham da regra ou do LLM:
antes disso, "achei caro" ainda e conversa de qualificacao.
"""
from __future__ import annotations

import re
from enum import StrEnum


class HandoffReason(StrEnum):
    QUOTE_UNAVAILABLE = "quote_unavailable"
    INVALID_REQUEST = "invalid_request"
    LLM_UNAVAILABLE = "llm_unavailable"
    TOOL_LOOP_EXHAUSTED = "tool_loop_exhausted"
    UNSAFE_REPLY = "unsafe_reply"
    LEAD_ASKED_HUMAN = "lead_asked_human"
    MEDIA_UNREADABLE = "media_unreadable"
    WANTS_TO_CLOSE = "wants_to_close"
    PRICE_OBJECTION = "price_objection"
    OUT_OF_SCOPE = "out_of_scope"


_TECH = ("Estou com uma instabilidade no sistema de cotacao e nao quero te passar um valor errado. "
         "Ja encaminhei seus dados para um consultor, que vai te responder por aqui em breve.")

HANDOFF_MESSAGES: dict[HandoffReason, str] = {
    HandoffReason.QUOTE_UNAVAILABLE: _TECH,
    HandoffReason.INVALID_REQUEST: _TECH,
    HandoffReason.LLM_UNAVAILABLE: _TECH,
    HandoffReason.TOOL_LOOP_EXHAUSTED: _TECH,
    HandoffReason.UNSAFE_REPLY: _TECH,
    HandoffReason.LEAD_ASKED_HUMAN: "Claro! Vou te passar para um consultor, ele continua por aqui mesmo.",
    HandoffReason.MEDIA_UNREADABLE: ("Nao consigo abrir arquivos e audios por aqui. Vou te passar para um consultor, "
                                     "que consegue ver o que voce mandou."),
    HandoffReason.WANTS_TO_CLOSE: ("Otimo! Para emitir a apolice um consultor confirma seus documentos e a forma de "
                                   "pagamento. Ja passei a sua cotacao para ele, que continua por aqui."),
    HandoffReason.PRICE_OBJECTION: ("Entendo. Ajuste de valor ou de franquia e com um consultor: ja passei a sua "
                                    "cotacao para ele ver o que da para fazer."),
    HandoffReason.OUT_OF_SCOPE: "Esse assunto e com um consultor. Ja te encaminhei, ele continua por aqui.",
}

AFTER_HANDOFF_MESSAGE = "Um consultor ja esta com a sua conversa e te responde por aqui em breve."
ASK_TEXT_MESSAGE = ("Nao consigo abrir arquivos, fotos ou audios por aqui. Pode me escrever a informacao em texto? "
                    "Para cotar preciso de: modelo e ano do carro, sua idade, o CEP onde o carro dorme e quando "
                    "quer comecar.")

# Regras deterministicas de ALTA PRECISAO: so o que da para afirmar sem contexto. O que e ambiguo
# (concorrente, "vou ver com alguem", comparacao de planos) fica com o LLM (ferramenta encaminhar_humano),
# e o codigo ainda barra fechamento/objecao vindos do LLM antes de haver cotacao (core.py).

# pedido de humano dirigido a empresa ("quero falar com um atendente", "me passa pra um consultor")
_WHO = r"(um |uma |o |a )?(atendente|humano|consultor|vendedor|pessoa|algu[eé]m)"
_HUMAN = re.compile(rf"\b((quero|queria|prefiro|posso|da pra|dá pra) falar com {_WHO}|"
                    rf"me (passa|transfere|encaminha) (pra|para) {_WHO}|"
                    r"quero (um |uma )?(atendente|humano|consultor|vendedor)|"
                    r"atendimento humano|pessoa de verdade|falar com (um )?humano)\b", re.IGNORECASE)
# intencao explicita de fechar, em qualquer tamanho de mensagem
_CLOSE = re.compile(r"\b(pode emitir|quero contratar|vou contratar|quero fechar|vou fechar|pode fechar|vamos fechar|"
                    r"fechar negocio|fechar negócio|manda o boleto)\b", re.IGNORECASE)
# nao e fechar com a gente: "fechar com a porto", "contratar o premium em vez desse, cota pra mim?"
# nao e fechar com a gente: "fechar com a porto", "contratar o premium em vez desse", "cota o premium"
# ("fechar com voces" e "fechar essa cotacao" sao fechamento, sim)
_NOT_OUR_CLOSE = re.compile(r"\b((fechar|contratar) com (?!voc[eê]s|vcs|vc\b|a autoseguro|voces)|"
                            r"em vez|ao inves|ao invés|outro plano|cota(r)? (o|a|outro|um|uma|pra)\b|cotar)",
                            re.IGNORECASE)
# resposta "sim" ao seguimento fixo do codigo ("Quer seguir com esse plano ou prefere que eu cote outro?")
_YES_TO_FOLLOWUP = re.compile(r"^\s*(sim|s|isso|quero|quero sim|quero seguir|pode seguir|sigo|quero esse|vou querer|"
                              r"esse mesmo|pode ser|bora|vamos)\b[\s,.!]*"
                              r"((quero )?(sim|mesmo|esse|seguir)|por favor|pf)?[\s.!]*$",
                              re.IGNORECASE)
# "fechado", "fechou", "aceito" sozinhos so valem em mensagem curta ("fechado!")
_CLOSE_SHORT = re.compile(r"\b(fechado|fechou|fecha|aceito|bora|vamos nessa|contrato)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(n[aã]o|nem|nunca|antes de|pensar)\b", re.IGNORECASE)
# objecao forte; pergunta comparativa ("fica muito mais caro?") nao conta
_PRICE = re.compile(r"\b((achei|ta|tá|esta|está|ficou|muito|bem|meio) (muito |bem |meio )?(caro|salgad[oa]|puxad[oa])|"
                    r"n[aã]o cabe no (meu )?bolso|fora do (meu )?or[cç]amento|tem (algum )?desconto|"
                    r"(vi|achei|encontrei) mais barato|franquia (ta |tá |esta |está |ficou )?(muito )?alta)\b",
                    re.IGNORECASE)


def _negated(text: str, start: int) -> bool:
    """Negacao nas ate 3 palavras antes do termo, dentro da mesma oracao: em "nao achei caro,
    quero fechar" o "nao" nega "caro", nao "quero fechar"."""
    clause = re.split(r"[,.;!?]", text[:start])[-1]
    return bool(_NEGATION.search(" ".join(clause.split()[-3:])))


def _first_unnegated(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _negated(text, m.start()) for m in pattern.finditer(text))


def _is_question_about(text: str, start: int) -> bool:
    """O termo esta numa pergunta ("e o premium, fica muito mais caro?"): a oracao do termo termina em '?'.
    A oracao acaba tambem em virgula, ':' ou ' mas ', para "Achei caro, tem desconto?" ainda ser objecao."""
    end = re.search(r"[.!?,;:]|\smas\s", text[start:])
    return bool(end and end.group() == "?")


def _wants_to_close(text: str) -> bool:
    if _NOT_OUR_CLOSE.search(text):
        return False  # fechar com concorrente ou pedir outra cotacao: o LLM conduz
    if any(not _negated(text, m.start()) and not _is_question_about(text, m.start()) for m in _CLOSE.finditer(text)):
        return True
    return len(text.split()) <= 3 and any(not _negated(text, m.start()) and not _is_question_about(text, m.start())
                                          for m in _CLOSE_SHORT.finditer(text))


def _price_objection(text: str) -> bool:
    return any(not _negated(text, m.start()) and not _is_question_about(text, m.start())
               for m in _PRICE.finditer(text))


def detect_inbound(text: str, *, media: bool, previous_was_media: bool, quote_delivered: bool,
                   followup_asked: bool = False) -> HandoffReason | None:
    """Regras deterministicas aplicadas ANTES do LLM. None = segue a conversa. followup_asked: a ultima
    mensagem do agente foi a pergunta fixa depois da cotacao, entao um "sim" curto e fechamento."""
    if media:
        return HandoffReason.MEDIA_UNREADABLE if previous_was_media else None
    if _first_unnegated(_HUMAN, text):
        return HandoffReason.LEAD_ASKED_HUMAN
    if quote_delivered and (_wants_to_close(text) or (followup_asked and _YES_TO_FOLLOWUP.match(text))):
        return HandoffReason.WANTS_TO_CLOSE
    if quote_delivered and _price_objection(text):
        return HandoffReason.PRICE_OBJECTION
    return None
