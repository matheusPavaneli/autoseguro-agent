"""QA: propriedades com random (stdlib) de seed fixa. Os dados pessoais saem das PROPRIAS funcoes de
scripts/generate_dataset.py (cpf, telefone, cep, placa, email), entao o formato e exatamente o do dataset.
Sem hypothesis: random com seed fixa ja da reprodutibilidade, e uma dependencia nova nao se justifica."""
from __future__ import annotations

import importlib.util
import random
import re

from agent.core import brl
from agent.pii import extract_cep, find_pii, mask
from agent.price_guard import has_money

from .qa_support import ROOT

N = 1500

_spec = importlib.util.spec_from_file_location("qa_gen", ROOT / "scripts/generate_dataset.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _pii(rng: random.Random) -> dict[str, str]:
    gen.random.seed(rng.random())  # o gerador usa o modulo random global
    nome, sobre = rng.choice(gen.NOMES), rng.choice(gen.SOBRENOMES)
    return {"cpf": gen.cpf(), "tel": gen.telefone(), "cep": gen.cep(), "placa": gen.placa(),
            "email": gen.email(nome, sobre)}


def _variants(p: dict[str, str], rng: random.Random) -> dict[str, str]:
    """Formato do dataset ou a versao so com digitos / sem hifen (como lead digita)."""
    out = dict(p)
    if rng.random() < 0.3:
        out["cpf"] = _digits(p["cpf"])
    if rng.random() < 0.3:
        out["cep"] = _digits(p["cep"])
    if rng.random() < 0.3:
        out["tel"] = "+" + _digits(p["tel"]) if rng.random() < 0.5 else _digits(p["tel"])[2:]
    if rng.random() < 0.3:
        out["placa"] = out["placa"].lower()
    return out


def _message(p: dict[str, str], rng: random.Random, include=("cpf", "tel", "cep", "placa", "email")) -> str:
    bits = [f"tenho {rng.randint(18, 82)} anos", f"{rng.choice(['Onix', 'Corolla', 'HB20'])} {rng.randint(2001, 2027)}"]
    labels = {"cpf": "CPF", "tel": "whats", "cep": "CEP", "placa": "placa", "email": "email"}
    bits += [f"{labels[k]} {p[k]}" for k in include]
    rng.shuffle(bits)
    sep = rng.choice([", ", " ", "; ", "\n", " e "])
    text = sep.join(bits)
    return text.capitalize() if rng.random() < 0.5 else text  # o gerador usa .capitalize()


def test_mask_never_leaves_generated_pii_and_is_idempotent():
    rng = random.Random(20261123)
    for _ in range(N):
        p = _variants(_pii(rng), rng)
        text = _message(p, rng)
        out = mask(text)
        for k, v in p.items():
            assert v.lower() not in out.lower(), (k, v, text, out)
        assert find_pii(out) == [], (text, out)
        assert mask(out) == out
        assert re.search(r"tenho \d{2} anos", out, re.IGNORECASE), "idade foi mascarada"


def test_mask_keeps_model_year_and_age():
    rng = random.Random(7)
    for _ in range(300):
        idade, ano = rng.randint(18, 99), rng.randint(1990, 2027)
        text = f"tenho {idade} anos, Gol {ano}, CPF {gen.cpf()}"
        out = mask(text)
        assert f"tenho {idade} anos" in out and f"Gol {ano}" in out


def test_extract_cep_never_returns_digits_from_cpf_or_phone():
    rng = random.Random(99)
    for _ in range(N):
        p = _variants(_pii(rng), rng)
        text = _message(p, rng, include=("cpf", "tel", "email", "placa"))
        assert extract_cep(text) is None, (text, extract_cep(text))
        with_cep = _message(p, rng)
        got = extract_cep(with_cep)
        assert got is not None and _digits(got) == _digits(p["cep"]), (with_cep, got)


def _amount_formats(v: float) -> list[str]:
    i = int(v)
    return [brl(v), brl(v).replace(" ", ""), f"{v:.2f}", f"{v:.2f}".replace(".", ","), f"R$ {i}", f"R${i}",
            f"{i} reais", f"{brl(v)} por mes", f"fica uns {v:.2f}".replace(".", ",")]


def test_has_money_catches_any_formatted_brl_amount():
    rng = random.Random(42)
    for _ in range(N):
        v = round(rng.uniform(0.01, 99_999.99), 2)
        for s in _amount_formats(v):
            assert has_money(f"O valor e {s}."), s
        if v >= 1000:
            assert has_money(f"franquia de {brl(v)[3:].split(',')[0]}")  # 4.500 / 12.345 sem centavos


def test_has_money_does_not_flag_ages_years_days_or_masked_tokens():
    rng = random.Random(5)
    for _ in range(N):
        s = (f"Voce tem {rng.randint(18, 99)} anos, carro {rng.randint(1990, 2027)}, inicio "
             f"{rng.randint(2026, 2030)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}, carencia de "
             f"{rng.randint(1, 90)} dias, CEP [CEP], CPF [CPF].")
        assert not has_money(s), s


# regressao QA 2026-11-23 (era xfail, corrigido): integer price with a price word but without R$/reais passes the guar
def test_has_money_catches_integer_amounts_in_price_context():
    rng = random.Random(3)
    misses = [f"a mensalidade fica {rng.randint(90, 900)} por mes" for _ in range(50)]
    assert all(has_money(s) for s in misses)
