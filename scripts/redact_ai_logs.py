"""Exporta as sessoes do Claude Code deste desafio para ai-logs/, redigindo dado pessoal e segredo.

  python scripts/redact_ai_logs.py --src <pasta-do-projeto-no-claude> --session <id>   # exporta
  python scripts/redact_ai_logs.py --check                                             # so verifica

O que sai redigido (em qualquer string de qualquer linha JSON):
  - e-mails reais do autor (lidos de `git config user.email` e `--global`, nunca escritos aqui);
  - nome de usuario e caminho do home do Windows;
  - keys de API (gsk_..., sk-..., "Bearer ...");
  - o bloco do CLAUDE.md global do autor e o contexto de conta injetados pelo Claude Code
    (instrucoes pessoais, nomes de outros projetos), trocados por um marcador.
Os dados do dataset sao sinteticos (DICIONARIO.md) e ficam como estao.

--check varre ai-logs/ e sai 1 se achar qualquer um dos itens acima.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ai-logs" / "claude-code"

_KEYS = re.compile(r"\b(gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,})\b|(Bearer\s+)[A-Za-z0-9._-]{16,}")
_PRIVATE_FILE = re.compile(r"(claude-profiles|\.claude)[\\/].*(CLAUDE|MEMORY)\.md$|[\\/]memory[\\/][^\\/]+\.md$",
                           re.IGNORECASE)
_PRIVATE_BLOCKS = [
    re.compile(r"Contents of [^\n]*CLAUDE\.md \(user's private global instructions[\s\S]*?(?=</system-reminder>|"
               r"Contents of |\Z)"),
    re.compile(r"# userEmail[\s\S]*?(?=</system-reminder>|\Z)"),
    re.compile(r"You have a persistent file-based memory at[\s\S]*?(?=\n# [A-Z]|</system-reminder>|\Z)"),
]
# pasta de perfil do Claude Code do autor (nome de conta e layout local): sai como <perfil-claude>
_PROFILE_MARK = "claude-profiles"
_PATH_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-:\\/")


def _redact_profile(text: str) -> str:
    """Troca o caminho inteiro em volta de 'claude-profiles' por <perfil-claude>. Varredura linear
    (uma regex com quantificador aninhado aqui ficou exponencial num arquivo de 2 MB)."""
    low = text.lower()
    out, i = [], 0
    while (j := low.find(_PROFILE_MARK, i)) != -1:
        start = j
        while start > i and text[start - 1] in _PATH_CHARS:
            start -= 1
        end = j + len(_PROFILE_MARK)
        while end < len(text) and text[end] in "\\/":   # separadores e o nome da conta logo depois
            end += 1
        while end < len(text) and (text[end].isalnum() or text[end] in "_.-"):
            end += 1
        out.append(text[i:start])
        out.append("<perfil-claude>")
        i = end
    out.append(text[i:])
    return "".join(out)


# titulo da 1a secao do CLAUDE.md global do autor. Montado por partes para que este arquivo, ao
# aparecer na propria sessao exportada, nao contenha a frase inteira (falso positivo no --check).
_GLOBAL_RULES_MARKER = "hard rule, " + "every answer, " + "every project"
_PLACEHOLDER = "[instrucoes pessoais e contexto de conta do autor omitidos]"


def _git_emails() -> set[str]:
    found: set[str] = set()
    for args in (["git", "config", "user.email"], ["git", "config", "--global", "user.email"]):
        try:
            out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out:
            found.add(out)
    return found


_PERSONAL_PROVIDERS = {"gmail.com", "proton.me", "protonmail.com", "outlook.com", "hotmail.com", "icloud.com"}


def _tolerant(literal: str) -> str:
    """Regex do literal aceitando barras invertidas entre os caracteres: saidas de depuracao trazem o
    e-mail escapado (nome\\.x\\@dominio) e, dentro de JSON, com as barras dobradas."""
    return r"\\{0,4}".join(re.escape(c) for c in literal)


def _username() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or Path.home().name


def _identity_patterns() -> list[tuple[re.Pattern[str], str]]:
    pats: list[tuple[re.Pattern[str], str]] = []
    for email in sorted(_git_emails()):
        pats.append((re.compile(_tolerant(email), re.IGNORECASE), "[EMAIL-AUTOR]"))
        domain = email.split("@", 1)[1]
        if domain not in _PERSONAL_PROVIDERS:
            pats.append((re.compile(_tolerant(domain), re.IGNORECASE), "[DOMINIO-EMPRESA]"))
            label = domain.split(".", 1)[0]
            if len(label) >= 4:
                # substring, sem \b: em texto escapado o nome vem colado num "\n" ("\\nempresa")
                pats.append((re.compile(re.escape(label), re.IGNORECASE), "[EMPRESA]"))
    user = _username()
    if user:
        for form in (rf"C:\\{{1,4}}Users\\{{1,4}}{re.escape(user)}", rf"/c/Users/{re.escape(user)}",
                     rf"C:/Users/{re.escape(user)}"):
            pats.append((re.compile(form, re.IGNORECASE), "~"))
        # fronteira de palavra OU logo depois de um escape (\n, \r, \t), sem pegar "privado"/"privacidade"
        pats.append((re.compile(rf"(?:(?<=\\[nrt])|(?<![A-Za-z])){re.escape(user)}(?![A-Za-z])",
                                re.IGNORECASE), "<usuario>"))
    account = _account_name()
    if account:
        pats.append((re.compile(re.escape(account), re.IGNORECASE), "<conta-claude>"))
    return pats


def _account_name() -> str:
    """Nome da pasta de perfil do Claude Code (ultima parte de CLAUDE_CONFIG_DIR), lido do ambiente e
    nunca escrito aqui. Sem a variavel, nao ha o que redigir alem do caminho."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    name = Path(cfg).name if cfg else ""
    return name if len(name) >= 4 and name.lower() not in {".claude", "claude"} else ""


def _company_labels() -> list[str]:
    labels = []
    for email in _git_emails():
        domain = email.split("@", 1)[1]
        if domain not in _PERSONAL_PROVIDERS and len(domain.split(".", 1)[0]) >= 4:
            labels.append(domain.split(".", 1)[0].lower())
    return labels


# --check independente da redacao: qualquer e-mail (mesmo escapado) cujo dominio nao seja dos dados
# sinteticos do dataset/testes conta como vazamento, venha de onde vier
_ANY_EMAIL = re.compile(r"[\w.+-]{1,64}\\{0,4}@\\{0,4}([\w-]{1,63}(?:\\{0,4}\.[\w-]{1,63}){1,6})")
_SYNTHETIC_DOMAINS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com.br", "icloud.com", "bol.com.br",
                      "anthropic.com", "example.com", "llm.test", "x.com", "domain.com", "domain.com.br",
                      "empresa.com.br"}  # os 4 ultimos: exemplos ficticios dos revisores


_TLDS = {"com", "br", "me", "net", "org", "io", "dev", "ai", "co", "edu", "gov", "info", "us", "uk", "pt"}


def _fixture_addresses() -> set[str]:
    """Enderecos ficticios do proprio teste da redacao: aparecem na sessao porque o teste foi escrito nela."""
    f = ROOT / "tests" / "test_redact_ai_logs.py"
    if not f.is_file():
        return set()
    plain = f.read_text(encoding="utf-8").replace("\\", "")  # o teste escreve alguns escapados
    return {m.group(0).lower() for m in re.finditer(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", plain)}


def _leaked_emails(text: str) -> int:
    """Conta e-mails reais: TLD conhecido (descarta @pytest.fixture, pacote@18.3.1), dominio nao sintetico
    e endereco que nao e fixture do teste da redacao."""
    fixtures = _fixture_addresses()
    n = 0
    for m in _ANY_EMAIL.finditer(text):
        domain = m.group(1).replace("\\", "").lower()
        if domain.rsplit(".", 1)[-1] not in _TLDS or domain in _SYNTHETIC_DOMAINS:
            continue
        local = re.split(r"[^\w.+-]", m.group(0).split("@")[0].replace("\\", ""))[-1].lower()
        if any(fx.endswith(f"{local}@{domain}") or fx == f"{local}@{domain}" for fx in fixtures) and local:
            continue
        n += 1
    return n


def redact_text(text: str, identity: list[tuple[re.Pattern[str], str]]) -> str:
    for block in _PRIVATE_BLOCKS:
        text = block.sub(_PLACEHOLDER + "\n", text)
    if _GLOBAL_RULES_MARKER in text:
        # sobrou trecho do CLAUDE.md global fora dos blocos conhecidos (ex.: saida de depuracao): sai inteira
        return _PLACEHOLDER
    text = _KEYS.sub(lambda m: (m.group(2) or "") + "[KEY-REDIGIDA]", text)
    text = _redact_profile(text)
    for pattern, token in identity:
        text = pattern.sub(token, text)
    return text


def _walk(v: object, identity: list[tuple[re.Pattern[str], str]]) -> object:
    if isinstance(v, str):
        return redact_text(v, identity)
    if isinstance(v, list):
        return [_walk(x, identity) for x in v]
    if isinstance(v, dict):
        path = v.get("path")
        if isinstance(path, str) and "content" in v and _PRIVATE_FILE.search(path):
            # instrucoes globais/memoria do autor anexadas como arquivo
            v = {**v, "content": _PLACEHOLDER}
        # chaves tambem: respostas de AskUserQuestion vem num dict indexado pelo texto da pergunta
        return {redact_text(k, identity): _walk(x, identity) for k, x in v.items()}
    return v


def export(src_dir: Path, session: str, names: dict[str, str]) -> list[Path]:
    identity = _identity_patterns()
    pairs = [(src_dir / f"{session}.jsonl", OUT / "session.jsonl")]
    for sub in sorted((src_dir / session / "subagents").glob("*.jsonl")):
        pairs.append((sub, OUT / "subagents" / f"{names.get(sub.stem, sub.stem)}.jsonl"))
    written = []
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for raw in src.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                lines.append(json.dumps(_walk(json.loads(raw), identity), ensure_ascii=False))
            except json.JSONDecodeError:
                lines.append(redact_text(raw, identity))
        dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(dst)
    return written


def check() -> int:
    identity = _identity_patterns()
    hits = 0
    for f in sorted((ROOT / "ai-logs").rglob("*")):
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        # nunca imprime o dado em si: so o tipo do problema
        problems = [token for p, token in identity if p.search(text)]
        account = _account_name().lower()
        if "claude-profiles" in text.lower() or (account and account in text.lower()):
            problems.append("caminho do perfil do Claude Code")
        if any(label in text.lower() for label in _company_labels()):  # substring pura, independente da regex
            problems.append("nome da empresa")
        if n := _leaked_emails(text):
            problems.append(f"{n} e-mail(s) fora dos dominios sinteticos")
        if _KEYS.search(text):
            problems.append("api-key")
        if any(b.search(text) for b in _PRIVATE_BLOCKS) or _GLOBAL_RULES_MARKER in text:
            problems.append("bloco-privado")
        if problems:
            hits += 1
            print(f"{f.relative_to(ROOT)}: {problems}")
    print(f"ai-logs: {hits} arquivo(s) com dado a redigir")
    return 1 if hits else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, help="pasta do projeto em ~/.claude/projects/<slug>")
    ap.add_argument("--session", help="id da sessao principal")
    ap.add_argument("--name", action="append", default=[], metavar="STEM=NOME",
                    help="renomeia um subagente, ex.: agent-a1b2=critic-plan")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not a.check:
        if not (a.src and a.session):
            ap.error("--src e --session sao obrigatorios para exportar")
        names = dict(n.split("=", 1) for n in a.name)
        for p in export(a.src, a.session, names):
            print(f"escrito {p.relative_to(ROOT)}")
    return check()


if __name__ == "__main__":
    sys.exit(main())
