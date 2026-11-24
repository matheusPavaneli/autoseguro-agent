# ai-logs: conversas com IA usadas no desafio

Ferramenta: **Claude Code** (CLI). Uma sessão principal e os subagentes que ela abriu:

| Arquivo | O que é |
|---|---|
| [claude-code/session.jsonl](claude-code/session.jsonl) | sessão principal: leitura do desafio, decisões, frame, plano, implementação, demos e correções |
| [claude-code/subagents/critic-plan.jsonl](claude-code/subagents/critic-plan.jsonl) | crítico adversarial do plano (14 achados; os aceitos estão em [docs/plano-sdd.md](../docs/plano-sdd.md)) |
| [claude-code/subagents/qa-specialist.jsonl](claude-code/subagents/qa-specialist.jsonl) | especialista de QA: testes de todos os tipos e avaliação com o LLM real (13 bugs) |
| [claude-code/subagents/code-review-1.jsonl](claude-code/subagents/code-review-1.jsonl) | code-review nativo, 1ª passada (15 achados) |
| [claude-code/subagents/code-review-2.jsonl](claude-code/subagents/code-review-2.jsonl) | code-review nativo, 2ª passada (15 achados; levou de volta ao plano) |
| [claude-code/subagents/fable-final.jsonl](claude-code/subagents/fable-final.jsonl) | avaliação final independente da estrutura inteira (veredito: enviar depois de pequenas correções) |
| [claude-code/subagents/code-review-3.jsonl](claude-code/subagents/code-review-3.jsonl) | code-review nativo do novo ciclo, 1ª passada (15 achados) |
| [claude-code/subagents/code-review-4.jsonl](claude-code/subagents/code-review-4.jsonl) | code-review nativo do novo ciclo, 2ª passada (15 achados; aplicado o teto decidido pelo dono) |

Formato: o `.jsonl` cru do Claude Code (`~/.claude/projects/<slug>/`), uma linha JSON por evento. São cerca de
10 MB. Para ler a sequência sem abrir tudo, comece pelo destino de cada achado em [docs/review.md](../docs/review.md)
e pela seção "Processo e uso de IA" do [README](../README.md).

## Linha do tempo (2026-11-23)

1. Leitura do desafio e medição do dataset. Decisões do dono: LLM grátis e trocável, SOLID.
2. Primeiro rascunho feito fora do pipeline. O dono mandou recomeçar pelo `workbench`.
3. Triage, frame e plano auditado. Um crítico Opus achou 14 pontos no plano.
4. Implementação e primeira execução real, que mostrou 4 problemas que os testes com fake não pegavam.
5. QA de todos os tipos: 13 bugs, todos corrigidos com teste.
6. Code-review `xhigh`, duas passadas: 30 achados. Na 2ª, pela regra, voltou ao plano.
7. Avaliação final independente da estrutura e o novo ciclo de code-review (mais 2 passadas, 30 achados).
   O dono decidiu o teto: corrige regressão real; caso de borda de heurística vira limitação documentada.
8. Avaliação com o LLM real sobre o código final: 7 de 9.

## O que foi redigido

Exportado com [`scripts/redact_ai_logs.py`](../scripts/redact_ai_logs.py) (o teste está em
`tests/test_redact_ai_logs.py`), que troca:
- e-mails do autor por `[EMAIL-AUTOR]`, inclusive na forma escapada que aparece em saída de depuração;
- empresa e domínio corporativo por `[EMPRESA]` e `[DOMINIO-EMPRESA]`;
- usuário e home do Windows por `<usuario>` e `~`;
- keys de API por `[KEY-REDIGIDA]`. A key do Groq nunca foi impressa;
- instruções globais e memória pessoais do autor, que o Claude Code injeta em cada turno, por um marcador.

O nome do autor foi mantido, porque já é público como autor dos commits. O `--check` usa um detector próprio, que
não é a regex da redação, e dá 0 ocorrências. Os CPFs, CEPs e outros dados que aparecem nas conversas são os
sintéticos do dataset e dos roteiros de demo.
