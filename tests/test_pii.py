from __future__ import annotations

import pytest

from agent.pii import extract_cep, find_pii, mask

# formatos exatos de scripts/generate_dataset.py (linhas 41-68 e 106-115)
DATASET_MSG = "Cpf 529.982.247-25, tenho 35 anos, cep 07310-100"
CONTACT_MSG = "meu email é ana.silva12@yahoo.com.br e o whats é esse mesmo +55 11 91234-5678"
PLATE_MSG = "a placa é ABC1D23 se precisar"


def test_every_pii_type_is_masked_and_age_and_year_survive():
    out = mask(f"{DATASET_MSG}. {CONTACT_MSG}. {PLATE_MSG}. Toyota Corolla 2008")
    for raw in ["529.982.247-25", "07310-100", "ana.silva12@yahoo.com.br", "91234-5678", "ABC1D23"]:
        assert raw not in out
    for token in ["[CPF]", "[CEP]", "[EMAIL]", "[TELEFONE]", "[PLACA]"]:
        assert token in out
    assert "35 anos" in out and "Corolla 2008" in out
    assert find_pii(out) == []


def test_unformatted_and_lowercase_variants_are_masked():
    out = mask("cpf 52998224725 cep 07310100 placa abc1d23")
    assert find_pii(out) == []
    assert "52998224725" not in out and "07310100" not in out and "abc1d23" not in out


def test_extract_cep_ignores_digits_of_cpf_and_phone():
    assert extract_cep(DATASET_MSG) == "07310-100"
    assert extract_cep("CPF 529.982.247-25 e whats +55 11 91234-5678, cep 21040100") == "21040-100"
    assert extract_cep(CONTACT_MSG) is None
    assert extract_cep("cpf 52998224725") is None


def test_ambiguous_ceps_and_rg_are_not_taken_as_the_quote_cep():
    """Regressao do code-review: com dois CEPs o primeiro vencia; RG 12345678-9 virava CEP."""
    assert extract_cep("moro no 01310-100 mas o carro dorme no 07123-000") is None
    assert extract_cep("RG 12345678-9") is None
    assert extract_cep("cep 07310100, repito 07310-100") == "07310-100"  # o mesmo CEP duas vezes nao e ambiguo


def test_model_plus_year_is_not_a_plate():
    """Regressao do code-review: 'gol2004' virava [PLACA] e o LLM perdia o ano."""
    assert mask("meu carro e um gol2004 ou fox-2010") == "meu carro e um gol2004 ou fox-2010"
    assert mask("placa abc1d23 e ABC-1234") == "placa [PLACA] e [PLACA]"


@pytest.mark.parametrize("raw,token", [("98765-4321", "[TELEFONE]"), ("11 98765.4321", "[TELEFONE]"),
                                       ("3456-7890", "[TELEFONE]"), ("07310 100", "[CEP]")])
def test_second_review_formats_are_masked(raw, token):
    """Regressao da 2a passada: celular sem DDD, com ponto, fixo sem DDD e CEP com espaco iam crus."""
    assert mask(f"dado {raw} fim") == f"dado {token} fim"


def test_labelled_year_like_plate_is_masked_and_cep_without_hyphen_survives():
    assert mask("placa KLM-2015") == "placa [PLACA]"
    assert extract_cep("cep 21040100") == "21040-100"
    assert extract_cep("quero comecar 15122026") is None  # data DDMMAAAA nao e CEP


def test_broad_detector_is_independent_of_the_mask():
    from agent.pii import find_pii_broad
    assert find_pii_broad("cel 98765 4321") and find_pii_broad("placa KLM2015")
    assert find_pii_broad("lead_conv_00001 [CEP] [CPF] Corolla 2022") == []


@pytest.mark.parametrize("text", ["Onix 2022-2023", "Corolla 2022 2023", "carro 2019.2020"])
def test_vehicle_year_pairs_are_not_phones(text):
    """Regressao do code-review do novo ciclo: par de anos virava [TELEFONE] e o LLM nunca via o ano."""
    assert mask(text) == text


def test_nine_digit_mobile_and_plate_after_words_are_masked():
    assert mask("meu cel 987654321") == "meu cel [TELEFONE]"
    assert mask("a placa do carro e ABC2015") == "a placa do carro e [PLACA]"


def test_cep_needs_format_or_label():
    """8 digitos soltos (fixo, RG) nao viram CEP; correcao formatada sem a palavra 'cep' vira."""
    assert extract_cep("meu telefone fixo e 34567890") is None
    assert extract_cep("o cep ja te passei, meu RG e 12345678") is None
    assert extract_cep("na verdade o carro dorme na casa da minha mae, 07123-000") == "07123-000"


def test_rg_and_card_are_masked_and_phone_with_ddi_is_still_a_phone():
    assert "12.345.678" not in mask("rg 12.345.678-9") and "123456789" not in mask("meu rg e 123456789")
    assert mask("cartao 4111 1111 1111 1111") == "cartao [CARTAO]"
    assert mask("+5511912345678") == "[TELEFONE]"


def test_bare_cep_answer_is_taken_only_when_the_message_is_just_the_cep():
    from agent.pii import cep_candidates
    assert cep_candidates("07310100", alone_ok=True) == ["07310-100"]
    assert cep_candidates("meu fixo 34567890", alone_ok=True) == []
