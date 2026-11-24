# FDE-1: review-diff (ciclo depois da volta ao plano)

Preset solo-saas. Diff: 53 arquivos, todos novos. Zonas críticas: `user-data` (agent/pii.py), `secrets`
(.env.example), `auth` e `billing`. `auth` e `billing` são heurística de nome sobre ai-logs/*.jsonl; o conteúdo
foi conferido por `redact_ai_logs.py --check` (detector próprio) e por uma contagem independente de usuário,
empresa, e-mail escapado e key, que deu 0. Gates mecânicos: nenhum segredo, nenhum erro engolido.

## Achados deste ciclo

- medium (corrigido): `scripts/redact_ai_logs.py` não tinha teste. É zona `user-data` e foi o código que vazou duas
  vezes. Adicionado `tests/test_redact_ai_logs.py` com os 5 caminhos de vazamento já vistos (e-mail escapado, chave
  de dict, arquivo privado anexado, rótulo depois de escape, key). Provado: com a lógica antiga 2 dos 5 falham.
- A lista "source changed with no test changed alongside" é heurística de nome. Conferido: handoff, price_guard,
  prompts, quote_client, openai_compat e llm/base estão cobertos por `test_qa_units`, `test_price_guard`,
  `test_quote_client`, `test_qa_resilience`, `test_factory` e `test_core`. `qa_live.py` é a própria ferramenta de
  QA, sem teste por desenho. `build_silver.py` é verificado pelo `--check` no `verify`.

Nenhum outro achado. Os 30 pontos das duas passadas do code-review nativo estão corrigidos (lista abaixo).

## Dependências (gate "no new dependency")

- `pyproject.toml:7` — `"httpx>=0.27",`. Única dependência de runtime, mantida pelo autor do repo. A stdlib
  (`urllib`) não tem timeout por chamada nem transport injetável para teste sem rede, que são a base dos testes de
  falha da /quote e do LLM. O SDK `openai` foi recusado (crítico do plano, item 13).
- `pyproject.toml:16` — `"pandas>=2.2",` (e pyarrow). Só no grupo opcional `silver`: é para ler o
  `conversations.parquet` do desafio, que o próprio gerador escreve com pandas e pyarrow. O agente não depende deles.
- pytest e ruff, no grupo `dev`, são o runner e o linter do repo. Não havia nenhum antes.

As linhas de `name`, `version`, `description`, `requires-python` e `target-version` são falsos positivos do gate.

## Histórico

**review-diff do 1º ciclo, 3 achados, todos corrigidos com teste:**
- hífen U+2011 em data lida como preço;
- ids numa lista (`quote_ids`) eram mascarados;
- pro-rata parcial derrubava `format_quote`.

**Code-review nativo `xhigh`, 1ª passada, 15 achados, todos corrigidos:**
1. guarda deixava passar frases de preço sem R$ — virou lista de permitidos;
2. de dois CEPs, o 1º vencia — agora pergunta;
3. 200 parcial derrubava `format_quote`;
4. `DecodingError` escapava;
5. objeção sem negação e com falsos positivos;
6. 429/408 da `/quote` não eram repetidos;
7. pareamento no `qa_live`;
8. `Retry-After` negativo ou `nan`;
9. mensagem segura citava cotação inexistente;
10. "janeiro de 2027" lido como preço;
11. "gol2004" mascarado como placa;
12. README dizia 8/9 no live (era 6/9);
13. porta do `qa_live`;
14. nome do autor nos logs: mantido por decisão do dono, porque já é público nos commits;
15. negação longe demais do termo.

**Code-review nativo `xhigh`, 2ª passada, 15 achados (11 importantes). Pela rubrica, volta ao plano. Todos
corrigidos:**
1. ai-logs vazavam e-mail escapado, empresa e usuário, e o `--check` dava 0;
2. mês sem fronteira de palavra;
3. CEP sobrescrito por 8 dígitos soltos;
4. testes E2E dependentes da data real;
5. formatos de telefone, placa e CEP;
6. LLM encaminhava objeção ou fechamento sem cotação;
7. palavras de fechamento amplas demais;
8. pedido de humano sem negação;
9. ano solto e "R339" passavam pela guarda;
10. contagens pequenas eram bloqueadas;
11. concorrente detectado por palavra solta;
12. gates usavam a mesma regex do mascaramento;
13. `--name` apagava o log com a API fora;
14. contador de bloqueios não zerava no turno da cotação;
15. `.workflow/` ignorado pelo git (artefatos copiados para `docs/`).

**Na demo real depois das correções:** "(ex.: hoje, amanhã, 15)" era bloqueado. Corrigido, com o texto real como
teste.

**Novo ciclo, 1ª passada, 15 achados (8 importantes). Todos corrigidos com teste:**
- guarda: número por extenso, verbo flexionado, plano + número, dia 1-31 sem contexto de data;
- par de anos virando telefone;
- celular de 9 dígitos sem separador;
- CEP por 8 dígitos soltos ou não substituído na correção;
- fechamento em pergunta e "fechar com <concorrente>";
- cotação com 0, NaN ou bool;
- 400 `tool_use_failed`;
- caminho do perfil nos ai-logs;
- porta do `qa_live`;
- `docs/` ausente;
- CEP ambíguo sem como resolver (a dica do prompt agora pede para redigitar).

**Novo ciclo, 2ª passada, 15 achados (7 importantes).** Pelo teto decidido pelo dono em 2026-11-23, regressão real
é corrigida e caso de borda de heurística vira limitação, sem nova volta ao plano. Todos os 15 foram corrigidos:
1. o LLM não sabia o que foi cotado — o resultado da ferramenta agora leva coberturas, carência e pro-rata, sem preço,
   e o prompt ganhou a regra de carência;
2. CEP digitado sozinho sem hífen;
3. o resumo de confirmação era bloqueado ("plano Essencial, HB20 2021");
4. injeção com eco de número do lead ("10 mensal") — o eco agora só vale para ano e número de modelo;
5. "fechar com vocês" e "essa cotação" eram vetados;
6. "Achei caro, tem desconto?" — a oração agora termina na vírgula;
7. RG e cartão iam crus;
8. ano solto em "cerca de 2050";
9. pergunta curta ("fecha?") fechava;
10. "sim" ao seguimento fixo — agora fecha;
11. encaminhar depois de recusa caía em loop;
12. `OverflowError` com `inf`;
13. nome da conta do perfil, que agora vem de `CLAUDE_CONFIG_DIR` e nunca fica escrito no código;
14. franquia 0 válida;
15. limites de desempenho com folga, mantendo o de ReDoS em 0,15 s (medido 2 ms; quadrático 360 ms).
