"""Bronze -> Silver: dataset de conversas com dados pessoais mascarados, mais as estatisticas
usadas no README.

  uv run --group silver python scripts/build_silver.py           # grava data/silver/ e imprime numeros
  uv run --group silver python scripts/build_silver.py --check   # tambem sai 1 se sobrar PII no silver

Mascaramento:
  - message_body: agent.pii.mask (CPF, e-mail, telefone, CEP, placa);
  - nome do lead (sender_name e o primeiro nome citado no texto, ex. "Ola Ana") vira
    lead_<conversation_id>: um id que nao e derivado do nome, entao nao da para reverter
    por dicionario;
  - nome do vendedor fica: e o funcionario, nao o titular do dado.
data/silver/ e ignorado pelo git (derivado, regeneravel).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.pii import find_pii, find_pii_broad, mask  # noqa: E402

CURRENT_YEAR = 2026  # as faixas da API usam o ano corrente (quote_logic.py:31); dataset gerado em 2026


def to_silver(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    lead_names = (df[df.sender_role == "lead"].groupby("conversation_id").sender_name.first())

    def scrub(row: pd.Series) -> str:
        text = mask(row.message_body)
        full = lead_names.get(row.conversation_id)
        if full:
            token = f"lead_{row.conversation_id}"
            for name in (full, full.split()[0]):
                text = re.sub(rf"\b{re.escape(name)}\b", token, text)
        return text

    df["message_body"] = df.apply(scrub, axis=1)
    is_lead = df.sender_role == "lead"
    df.loc[is_lead, "sender_name"] = "lead_" + df.loc[is_lead, "conversation_id"]
    return df


def stats(df: pd.DataFrame) -> dict[str, object]:
    conv = df.drop_duplicates("conversation_id")
    ano = conv.veiculo_texto.str.extract(r"(\d{4})$")[0].astype(int)
    old_car = CURRENT_YEAR - ano > 20
    old_lead = conv.lead_idade_informada > 75
    refused = old_car | old_lead
    lead_msgs = df[df.sender_role == "lead"]
    objection = lead_msgs.message_body.str.contains(
        "salgado|franquia|mais barato|pensar|esposa|caro", case=False, regex=True)
    media_convs = df[df.message_type != "text"].conversation_id.nunique()
    return {
        "conversas": len(conv),
        "mensagens": len(df),
        "desfecho": conv.conversation_outcome.value_counts(normalize=True).round(3).to_dict(),
        "lead_acima_75": round(old_lead.mean(), 3),
        "veiculo_mais_de_20_anos": round(old_car.mean(), 3),
        "recusaria_pela_api": round(refused.mean(), 3),
        "recusaria_mas_ganho": int((refused & (conv.conversation_outcome == "ganho")).sum()),
        "conversas_com_midia": round(media_convs / len(conv), 3),
        "midia_por_tipo": df[df.message_type != "text"].message_type.value_counts().to_dict(),
        "conversas_com_objecao": round(lead_msgs[objection].conversation_id.nunique() / len(conv), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=ROOT / "dataset/conversations.parquet")
    ap.add_argument("--out", type=Path, default=ROOT / "data/silver/conversations.parquet")
    ap.add_argument("--check", action="store_true", help="sai 1 se pii.find_pii achar algo no silver")
    a = ap.parse_args()

    bronze = pd.read_parquet(a.src)
    silver = to_silver(bronze)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    silver.to_parquet(a.out, index=False)
    for k, v in stats(bronze).items():
        print(f"{k}: {v}")
    print(f"silver: {a.out.relative_to(ROOT)} ({len(silver)} mensagens)")

    # detector amplo e independente do mascaramento (senao o gate so confirma a propria regex)
    leaks = silver.message_body.map(lambda t: find_pii(t) + find_pii_broad(t))
    names_left = sum(
        bool(re.search(rf"\b{re.escape(n.split()[0])}\b", body))
        for n, body in zip(bronze.sender_name.where(bronze.sender_role == "lead").ffill(), silver.message_body,
                           strict=True)
    )
    n_leaks = int(leaks.map(bool).sum())
    print(f"mensagens com PII restante: {n_leaks}; com primeiro nome do lead restante: {names_left}")
    if a.check and (n_leaks or names_left):
        print("FALHOU: silver ainda tem dado pessoal", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
