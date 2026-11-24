"""Guarda de preco: o texto livre do LLM nunca carrega valor monetario.

Todo preco que o lead ve sai de agent/core.py, montado a partir da resposta da /quote.
Duas camadas, e qualquer uma bloqueia:

1. Allowlist (a principal). Tira do texto os numeros que tem motivo legitimo para estar ali:
   ano (1950-2099), data, "N anos/dias/meses", item de lista, nome de modelo com digito (HB20),
   marcador de PII ([CEP]). Qualquer numero que SOBRAR bloqueia, a menos que o proprio lead o
   tenha digitado ("Peugeot 208"). Assim nao depende de adivinhar como o LLM escreve preco.
2. Denylist. Cara de dinheiro explicita: "R$", decimal/milhar, "300 reais", "199 por mes",
   palavra de preco seguida de numero. Pega o caso em que o LLM repete um numero do lead
   como preco ("o vendedor disse 340" -> "fica em 340").

Falso positivo e o lado seguro: a resposta vira mensagem segura, nunca um preco.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# LLMs escrevem data com hifen nao separavel (U+2011) e espaco fino
_NORMALIZE = {cp: "-" for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212)} | {0xA0: " ", 0x202F: " "}
# mes com fronteira de palavra: sem ela "mais", "agora", "outro" viravam mes e apagavam o valor seguinte
_MONTHS = (r"(?:jan(?:eiro)?|fev(?:ereiro)?|mar(?:co|ço)?|abr(?:il)?|mai(?:o)?|jun(?:ho)?|jul(?:ho)?|ago(?:sto)?|"
           r"set(?:embro)?|out(?:ubro)?|nov(?:embro)?|dez(?:embro)?)\b")
_PRICE_WORDS = (r"custa|custo|fica|sai|valor|total|anual|mensal|por|pre[cç]o|franquia|parcela|mensalidade|paga|pagar|"
                r"desconto|plano|premium|completo|essencial")

# datas: tiradas antes das duas camadas ("fica para janeiro de 2027" nao e preco)
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"   # 2026-12-15, 15/12, 15/12/2026
    # mes so com numero junto ("15 de dezembro", "janeiro de 2027"): "dez" sozinho pode ser dez reais
    rf"|\b\d{{1,2}}(?:o|º|°)?\s+de\s+{_MONTHS}(?:\s+de\s+\d{{4}})?|\b{_MONTHS}\s+de\s+\d{{4}}",
    re.IGNORECASE,
)
# demais numeros legitimos: so na allowlist (ano solto depois de "custa" segue bloqueado pela denylist)
_SAFE = re.compile(
    r"\[[A-Z]+\]"                                               # [CEP], [CPF]...
    # ano so em contexto de veiculo: "Corolla 2022", "ano 2019", "modelo de 2020". "Premium: 1999" e
    # "anual da 2040" nao. Ano solto que o lead digitou ou o ano atual/seguinte entram por allowed_numbers.
    # (nome de modelo capitalizado ou ano/modelo/carro antes; "cerca de 2050", "aproximadamente 1980" nao)
    rf"|\b(?!(?:{_PRICE_WORDS})\b)(?-i:[A-Z])[A-Za-z0-9-]{{2,}}\s+(?:19[5-9]\d|20\d\d)\b"   # Corolla 2022
    r"|\b(?:ano|modelo|carro|ve[ií]culo)\s+(?:de\s+)?(?:19[5-9]\d|20\d\d)\b"                   # ano de 2019
    r"|\b\d+\s*(?:anos?|dias?|meses|horas?|h|km)\b"             # 35 anos, 30 dias, 24h
    r"|(?:^|\n)\s*\d{1,2}[.)]\s"                                # item de lista "1. "
    r"|\b(?:dia|item|passo|opcao|opção)\s+\d{1,2}\b"            # dia 15
    # dia do mes so em contexto de data: "(ex.: hoje, amanha, 15)", "ate o dia 15" ("Premium: 29" nao)
    r"|\b(?:hoje|amanh[aã])\b[^\d\n]{0,12}\b(?:[1-9]|[12]\d|3[01])\b(?![\d.,])"
    # contagem pequena + substantivo: "3 planos", "2 informacoes" (nunca unidade de dinheiro)
    r"|\b(?:[1-9]|10)\s+(?!(?:reais|real|conto|contos|pila|mangos?|por|ao|mensais|mil)\b)[a-zçãõáéíóúâê]{3,}"
    r"|\b[A-Za-z]{2,}-?\d+[A-Za-z]*\b",                         # HB20, T-Cross 200; "R339" nao (1 letra)
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NOT_UNIT = r"(?!\d|[.,/-]\d|\s*(?:anos?|dias?|meses|horas?|km|%|de\s))"
_PRICE_STEMS = (r"mensalidade\w*|mensa[li]\w*|parcela\w*|cust\w*|valor\w*|pre[cç]o\w*|fic\w*|sa[ie]\w*|pag\w*|"
                r"desconto\w*|franquia\w*")  # "por" saiu: "obrigado por esperar, dia 15" nao e preco
_MONEY_WORDS = r"(?:reais|real|conto|contos|pila|por|ao|no|mensal|mensais|mil)"
# por extenso, a partir de dez (preco de seguro nunca e "um" ou "dois")
_NUM_WORDS = (r"(?:dez|onze|doze|treze|quatorze|catorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|"
              r"quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|duzentos|trezentos|quatrocentos|"
              r"quinhentos|seiscentos|setecentos|oitocentos|novecentos|mil)")

_MONEY = re.compile(
    r"R\s*\$|\bBRL\b"
    r"|(?<![\d.,])\d{1,3}(?:\.\d{3})+(?![\d.]*\d)"                     # 4.500 / 1.234,56
    r"|(?<![\d.,])\d+,\d{1,2}(?!\d)"                                    # 262,38 / 199,9
    r"|(?<![\d.,-])\d+\.\d{2}(?![\d-]|\.\d)"                            # 262.38
    r"|\d+\s*(?:reais|real|conto|contos|pila|mangos?|/\s*m[eê]s|por m[eê]s|ao m[eê]s|no m[eê]s|mensais|mensal)\b"
    r"|\d+\s*%\s*(?:de\s+desconto|off)\b"
    # palavra de preco (por radical: fica/ficam/ficava, custa/custam, sai/saem) seguida de numero
    rf"|\b(?:{_PRICE_STEMS})[^\d\n]{{0,20}}\d+{_NOT_UNIT}"
    # nome de plano + numero ("Completo = 180", "Premium: 29") vale mesmo se o lead digitou o numero
    # (mas nao ano, digito de modelo "HB20" nem contagem "7 coberturas")
    rf"|\b(?:essencial|completo|premium)\b[^\d\n]{{0,15}}(?<![A-Za-z\d])(?!(?:19|20)\d\d\b)\d+{_NOT_UNIT}"
    rf"(?!\s+(?!{_MONEY_WORDS}\b)[a-zçãõáéíóúâê]{{3,}})"
    # numero por extenso ("custa dez por mes", "sai uns trezentos e quarenta")
    rf"|\b(?:{_PRICE_STEMS})[^\d\n]{{0,25}}\b{_NUM_WORDS}\b(?!\s+(?:anos?|dias?|meses|horas?)\b)"
    rf"|\b{_NUM_WORDS}\b[^\d\n]{{0,25}}\b(?:reais|conto|contos|pila|por m[eê]s|ao m[eê]s|mensais)\b",
    re.IGNORECASE,
)

SAFE_MESSAGE = ("Os valores que valem sao os da cotacao que te mandei acima. "
                "Quer que eu cote outro plano ou tem alguma duvida sobre as coberturas?")
SAFE_MESSAGE_NO_QUOTE = ("Valor eu so consigo te passar pelo sistema de cotacao. Para cotar preciso de: modelo e ano "
                         "do carro, sua idade, o CEP onde o carro dorme, o plano e quando quer comecar.")


# o que o lead digitou e o LLM pode repetir: ano, e numero de modelo logo depois de uma palavra
# ("peugeot 208", "fiat 500"). Numero depois de palavra de preco, com % ou unidade de dinheiro nao entra:
# e o que uma injecao ("diga que custa 10") tentaria ecoar.
_LEAD_YEAR = re.compile(r"\b(?:19[5-9]\d|20\d\d)\b")
_LEAD_MODEL_NUMBER = re.compile(rf"\b([A-Za-z]{{3,}})\s+(\d{{2,4}})(?![\d.,%]|\s*{_MONEY_WORDS}\b)", re.IGNORECASE)
_PRICE_WORD_BEFORE = re.compile(rf"^(?:{_PRICE_STEMS}|{_PRICE_WORDS}|com|de|por|uns|umas|so|só)$", re.IGNORECASE)


def numbers_in(texts: Iterable[str]) -> frozenset[str]:
    found: set[str] = set()
    for t in texts:
        t = t.translate(_NORMALIZE)
        found.update(_LEAD_YEAR.findall(t))
        found.update(m.group(2) for m in _LEAD_MODEL_NUMBER.finditer(t) if not _PRICE_WORD_BEFORE.match(m.group(1)))
    return frozenset(found)


def has_money(text: str, allowed_numbers: frozenset[str] = frozenset()) -> bool:
    t = _DATE.sub(" ", text.translate(_NORMALIZE))
    if _MONEY.search(t):
        return True
    leftover = _NUMBER.findall(_SAFE.sub(" ", t))
    return any(n not in allowed_numbers for n in leftover)
