"""A redacao dos ai-logs vai para um repo publico: cada caminho de vazamento ja visto vira teste.
Usa identidades ficticias (monkeypatch), nunca as do autor."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "redact_ai_logs", Path(__file__).resolve().parent.parent / "scripts" / "redact_ai_logs.py")
assert _SPEC and _SPEC.loader
r = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(r)

EMAILS = {"fulano.tal@empresaxyz.com.br", "fulanotal@proton.me"}


@pytest.fixture(autouse=True)
def fake_identity(monkeypatch):
    monkeypatch.setattr(r, "_git_emails", lambda: set(EMAILS))
    monkeypatch.setattr(r, "_username", lambda: "fulaninho")
    monkeypatch.setattr(r, "_account_name", lambda: "perfil-ficticio")


def _redact(obj):
    return json.dumps(r._walk(obj, r._identity_patterns()), ensure_ascii=False)


def test_plain_and_regex_escaped_emails_are_redacted():
    """Regressao: saida de depuracao trazia o e-mail escapado (fulanotal\\@proton\\.me) e passava."""
    text = (r"autor fulano.tal@empresaxyz.com.br e fulanotal\@proton\.me e fulano\\.tal@empresaxyz\\.com\\.br")
    out = _redact({"t": text})
    assert "fulano" not in out and "empresaxyz" not in out and out.count("[EMAIL-AUTOR]") == 3


def test_dict_keys_and_attached_private_files_are_redacted():
    out = _redact({"answers": {"Seu e-mail fulanotal@proton.me?": "sim"},
                   "files": [{"path": r"C:\Dev\Tools\claude-profiles\x\CLAUDE.md", "content": "regras pessoais"}]})
    assert "fulanotal" not in out and "regras pessoais" not in out


def test_company_label_and_username_after_escape_are_redacted():
    """Regressao: '\\nempresa' e 'users_usuario' passavam pelo \\b."""
    out = _redact({"t": r"linha\nempresaxyz e usuario\nfulaninho em C:\\Users\\fulaninho\\x e users_fulaninho"})
    assert "empresaxyz" not in out and "fulaninho" not in out


def test_keys_are_redacted():
    assert "gsk_" not in _redact({"t": "key gsk_ABCDEFGHIJKLMNOPQRSTUV123 e Bearer abcdefghijklmnopqrstuv"})


def test_check_detects_leaks_with_its_own_detector(tmp_path, monkeypatch):
    """--check nao pode depender so da regex da redacao (foi assim que deu 0 com vazamento)."""
    monkeypatch.setattr(r, "ROOT", tmp_path)
    logs = tmp_path / "ai-logs"
    logs.mkdir()
    (logs / "a.jsonl").write_text('{"t": "outro\\\\@desconhecido\\\\.com"}\n', encoding="utf-8")
    assert r.check() == 1
    (logs / "a.jsonl").write_text('{"t": "ana.silva12@yahoo.com.br @pytest.fixture pkg@18.3.1"}\n', encoding="utf-8")
    assert r.check() == 0


def test_claude_profile_path_and_account_name_are_redacted():
    out = _redact({"t": r"arquivo em C:\Dev\Tools\claude-profiles\perfil-ficticio\projects\x.jsonl, perfil-ficticio"})
    assert "claude-profiles" not in out and "perfil-ficticio" not in out
