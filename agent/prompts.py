"""System prompt e ferramentas expostas ao LLM.

Os planos vem de GET /planos (nada de tabela copiada no codigo). O prompt leva SO
nome e coberturas: preco base e franquia ficam fora, porque nenhum valor monetario
deve sair do LLM (agent/price_guard.py).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .llm.base import ToolSpec

COVERAGE_LABELS = {
    "colisao": "colisao", "roubo": "roubo", "furto": "furto", "terceiros": "danos a terceiros",
    "vidros": "vidros", "carro_reserva": "carro reserva", "assistencia_24h": "assistencia 24h",
}

OUT_OF_SCOPE_REASONS = {
    "pedido_do_lead": "lead pediu humano com outras palavras",
    "quer_fechar": "lead quer contratar/fechar",
    "objecao_preco": "lead reclamou do preco ou quer desconto",
    "fora_do_escopo": "assunto que nao e cotacao de seguro auto (sinistro, apolice existente, outro produto)",
}


_CEP_HINT = {"ambiguo": (" O lead mandou mais de um CEP: peca para ele digitar de novo so o CEP onde o carro dorme"
                        " (nao aceite 'o primeiro'/'o segundo': os CEPs chegam mascarados).")}


def plan_ids(plans: dict[str, Any]) -> list[str]:
    return [p["id"] for p in plans["planos"]]


def system_prompt(plans: dict[str, Any], today: dt.date, cep_status: str) -> str:
    linhas = "\n".join(
        f"- {p['nome']} (id {p['id']}): " + ", ".join(COVERAGE_LABELS.get(c, c) for c in p["coberturas"])
        for p in plans["planos"]
    )
    return f"""Voce e o assistente de vendas da AutoSeguro no WhatsApp. Atende leads que querem seguro de carro.
Hoje e {today.isoformat()}. Escreva em portugues do Brasil, mensagens curtas, tom cordial, sem emoji em excesso.

Planos (coberturas):
{linhas}

Para cotar voce precisa de: modelo e ano do carro, idade do lead, CEP onde o carro dorme, plano escolhido
e data de inicio da vigencia (converta "hoje", "amanha", "dia 15" para AAAA-MM-DD a partir de hoje).
Peca o que faltar, um ou dois itens por vez. Nao peca CPF, e-mail nem placa: nao sao necessarios para cotar.
Se o lead nao souber o plano, explique as coberturas e pergunte. Antes de cotar, confirme em uma linha
o resumo (carro/ano, idade, plano, data de inicio).

Dados pessoais chegam mascarados, ex.: [CEP], [CPF]. O sistema guarda o CEP real.
CEP ja informado nesta conversa: {cep_status}.{_CEP_HINT.get(cep_status, "")}

Regras duras:
1. NUNCA escreva valores, precos, franquias ou numeros de dinheiro. Para preco use a ferramenta cotar:
   o sistema envia a cotacao ao lead. Depois dela, nao repita valores; pergunte se quer fechar ou
   cotar outro plano. Duvidas sobre a cotacao enviada: responda so com o que o resultado da ferramenta
   cotar trouxe (coberturas, carencia, pagamento proporcional). Carencia: as coberturas listadas nela so
   valem depois do numero de dias informado, contado do inicio da vigencia.
2. Se a cotacao for recusada, explique o motivo com gentileza e nao ofereca contorno nem consultor.
3. Nao prometa desconto nem condicao. So existem os planos acima, cada um com exatamente as coberturas
   listadas: nunca atribua a um plano cobertura de outro e nunca invente plano ou variacao de plano.
4. Use encaminhar_humano quando o lead pedir um atendente, quiser fechar, reclamar do preco ou
   trouxer assunto fora de cotacao de seguro auto."""


def tools(plans: dict[str, Any]) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="cotar",
            description="Calcula a cotacao na API da AutoSeguro e envia o resultado ao lead. "
                        "Use so com todos os dados confirmados.",
            parameters={
                "type": "object",
                "properties": {
                    "plano_id": {"type": "string", "enum": plan_ids(plans)},
                    "idade": {"type": "integer", "description": "idade do lead em anos"},
                    "veiculo_ano": {"type": "integer", "description": "ano do veiculo, 4 digitos"},
                    "data_inicio": {"type": "string", "description": "inicio da vigencia, AAAA-MM-DD"},
                },
                "required": ["plano_id", "idade", "veiculo_ano", "data_inicio"],
            },
        ),
        ToolSpec(
            name="encaminhar_humano",
            description="Passa a conversa para um consultor humano.",
            parameters={
                "type": "object",
                "properties": {"motivo": {"type": "string", "enum": list(OUT_OF_SCOPE_REASONS),
                                          "description": "; ".join(f"{k}: {v}" for k, v in
                                                                   OUT_OF_SCOPE_REASONS.items())}},
                "required": ["motivo"],
            },
        ),
    ]
