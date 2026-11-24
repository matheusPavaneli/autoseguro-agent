"""Mascaramento de dados pessoais.

Usado em dois pontos de saida:
  1. antes de mandar texto ao LLM (provedor externo nao recebe CPF/e-mail/telefone/placa/CEP);
  2. antes de gravar qualquer texto no log de rastreio (agent/trace.py).

O CEP e o unico dado pessoal que a cotacao precisa: o codigo o extrai do texto cru
(extract_cep) e guarda no estado da conversa; o LLM so ve "[CEP]".

Os padroes casam pelo VALOR, nao pelo rotulo ("cpf", "Cpf", "CEP" tanto faz), e sao
ancorados para nao pegar pedaco de numero maior. A ordem importa: CPF e telefone
antes de CEP, porque todos sao sequencias de digitos.

Lacuna conhecida: nome proprio digitado pelo lead ("sou o Joao Silva") nao e
detectado; ver README.
"""
from __future__ import annotations

import re

# quantificadores limitados e inicio ancorado: sem isso o e-mail e quadratico num token longo (ReDoS)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_CPF = re.compile(r"(?<!\d)\d{3}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?\d{2}(?!\d)")
# celular (9 + 8 digitos) e fixo (2-5 + 7 digitos). Com DDD o separador e opcional; sem DDD
# ("98765-4321", "3456.7890") ele e obrigatorio, senao um CEP sem hifen ("21040100") viraria telefone.
_PHONE = re.compile(r"(?<![\d+])(?:(?:\+?55\s?)?\(?\d{2}\)?[\s.-]?(?:9\s?\d{4}|[2-5]\d{3})[-.\s]?\d{4}"
                    # sem DDD: separador obrigatorio, e "2022-2023" (par de anos do veiculo) nao e telefone
                    r"|(?!(?:19|20)\d\d[-.\s](?:19|20)\d\d(?!\d))(?:9\s?\d{4}|[2-5]\d{3})[-.\s]\d{4}"
                    r"|9\d{8})(?!\d)")  # celular de 9 digitos colado, sem DDD
_CEP = re.compile(r"(?<!\d)(\d{2})\.?(\d{3})[-\s]?(\d{3})(?!\d)")
_MAX_LEN = 10_000  # WhatsApp limita a 4.096; acima disso o texto e cortado antes de mascarar
# placa antiga (AAA9999) e Mercosul (AAA9A99); "gol2004"/"fox-2010" (modelo + ano) nao e placa...
_PLATE = re.compile(r"(?<![A-Za-z0-9])(?![A-Za-z]{3}-?(?:19|20)\d{2}(?![A-Za-z0-9]))"
                    r"[A-Za-z]{3}-?\d[A-Za-z0-9]\d{2}(?![A-Za-z0-9])")
# ...a menos que venha depois da palavra "placa": "placa KLM-2015" e placa
# ate 3 palavras entre "placa" e o valor ("a placa do carro e ABC2015")
_PLATE_LABELLED = re.compile(r"(?<=placa)((?:\s+[^\s\d]+){0,3}?\s*:?\s*)[A-Za-z]{3}-?\d[A-Za-z0-9]\d{2}(?![A-Za-z0-9])",
                             re.IGNORECASE)

# cartao (13 a 19 digitos, em grupos ou corrido) e RG (12.345.678-9, ou 7-10 digitos depois de "rg"):
# antes de CPF e telefone, senao viram mascaramento parcial que esconde o vazamento
_CARD = re.compile(r"(?<![\d+])(?:\d{4}[ .-]?){3}\d{1,7}(?!\d)")  # "+55119..." e telefone com DDI
_RG = re.compile(r"(?<!\d)\d{1,2}\.\d{3}\.\d{3}-[\dXx](?![\dXx])|(?<=\brg)(?:\s*(?:e|é|:|n[ºo°]?)?\s*)[\dXx.-]{7,13}",
                 re.IGNORECASE)

_ORDERED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", _EMAIL),
    ("[CARTAO]", _CARD),
    ("[RG]", _RG),
    ("[CPF]", _CPF),
    ("[TELEFONE]", _PHONE),
    ("[CEP]", _CEP),
    ("[PLACA]", _PLATE),
)

# Detector AMPLO e independente das regex acima, para os gates (--check): um verificador que usa a
# mesma regex do mascaramento nunca acha o que ela deixa passar. Pega qualquer sequencia de 8+ digitos
# (com ou sem separador), qualquer e-mail e qualquer placa, inclusive com cara de ano.
_BROAD = (
    ("digitos", re.compile(r"(?<!\d)\d(?:[\s.()-]?\d){7,}(?!\d)")),
    ("email", re.compile(r"\S+@\S+\.\S+")),
    ("placa", re.compile(r"(?<![A-Za-z])[A-Z]{3}-?\d[A-Z0-9]\d{2}(?![A-Za-z0-9])")),
)


def mask(text: str) -> str:
    text = _PLATE_LABELLED.sub(lambda m: m.group(1) + "[PLACA]", text[:_MAX_LEN])
    for token, pattern in _ORDERED:
        text = pattern.sub(token, text)
    return text


def find_pii_broad(text: str) -> list[str]:
    """Tipos de PII que o detector amplo ainda ve (gates). Pode dar falso positivo; nunca usa as regex
    do mascaramento."""
    return [label for label, pattern in _BROAD if pattern.search(text)]


def find_pii(text: str) -> list[str]:
    """Rotulos dos tipos de PII que ainda aparecem no texto (usado pelos gates --check)."""
    return [token for token, pattern in _ORDERED if pattern.search(text)]


# para EXTRAIR (nao para mascarar) o CEP e mais estrito: "12345678-9" e RG, nao CEP
_CEP_STRICT = re.compile(r"(?<!\d)(\d{2})\.?(\d{3})[-\s]?(\d{3})(?!-?\d)")
_CEP_LABEL = re.compile(r"\bcep\b", re.IGNORECASE)
# 8 digitos sem separador que formam data DDMMAAAA ("15122026") nao sao CEP
_DATE_8 = re.compile(r"(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])(19|20)\d\d")


def cep_candidates(text: str, *, alone_ok: bool = False) -> list[str]:
    """CEPs distintos do texto, normalizados como 00000-000, na ordem em que aparecem. E-mail, CPF
    e telefone saem antes, para que 8 digitos de dentro deles nunca virem CEP. Com alone_ok, uma
    mensagem que e SO os 8 digitos (resposta a "qual o CEP?") tambem vale."""
    cleaned = _PHONE.sub(" ", _CPF.sub(" ", _EMAIL.sub(" ", text[:_MAX_LEN])))
    alone = alone_ok and bool(re.fullmatch(r"\s*\d{5}-?\d{3}\s*[.!]?\s*", text))
    found = [f"{m.group(1)}{m.group(2)}-{m.group(3)}" for m in _CEP_STRICT.finditer(cleaned)
             if not _DATE_8.fullmatch(m.group(0)) and (alone or _looks_like_cep(cleaned, m))]
    return list(dict.fromkeys(found))


def _looks_like_cep(text: str, m: re.Match[str]) -> bool:
    """So conta como CEP se estiver formatado (07310-100, 07.310-100, 07310 100) ou com "cep" logo antes:
    8 digitos soltos podem ser RG, telefone fixo ou protocolo, e trocar o CEP muda o agravo de regiao."""
    formatted = any(ch in m.group(0) for ch in "-. ")
    return formatted or bool(_CEP_LABEL.search(text[max(0, m.start() - 15):m.start()]))


def extract_cep(text: str) -> str | None:
    """O CEP da mensagem quando ha exatamente um; com dois ou mais ("moro no X, o carro dorme no Y")
    devolve None, porque escolher errado tira o agravo de regiao e cota barato demais."""
    found = cep_candidates(text)
    return found[0] if len(found) == 1 else None
