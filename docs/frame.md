# FDE-1 — frame

Contexto: desafio técnico (DESAFIO.md). O "produto" é o agente de atendimento da
AutoSeguro fictícia. O enunciado já fixa o escopo; este frame decide o recorte mínimo
que cumpre cada critério de avaliação, e corta o resto.

## Quem tem o problema, e como sei

- Time de vendas que atende leads por WhatsApp (enunciado).
- Medido no `dataset/conversations.parquet` (2.500 conversas, 26.470 mensagens; script
  em scratchpad, vira `scripts/build_silver.py`):
  - desfecho: em_negociacao 30,3%, ganho 28,5%, perdido 21,5%, sem_resposta 19,7%;
  - **30,0% dos leads seriam recusados pelas regras da API** (idade > 75: 11,2%;
    veículo > 20 anos em 2026: 21,2%) — e mesmo assim **203 dessas conversas terminam
    "ganho"**: o vendedor cotou o que a regra recusa;
  - preços citados pelos vendedores vêm de uma lista fixa (129,90 … 389,90), sem relação
    com a regra de cálculo; o Premium é descrito com as coberturas do Essencial;
  - 56,8% das conversas têm mídia sem transcrição (774 documentos, 550 imagens, 465 áudios);
  - 51,8% das conversas têm objeção (preço, franquia, concorrente, "preciso pensar").

## O que fazem hoje

Vendedor humano pergunta modelo/ano, CPF, idade e CEP, e cota "de cabeça". O dado acima
mostra o custo: preço inconsistente e venda fora da regra de aceitação.

## Métrica (uma)

**% de conversas que terminam com cotação correta entregue ou recusa explicada, sem
humano** (resolvidas pelo agente), com a restrição dura de **0 preços fora da API**.
Medida nos eventos `conversation_end` / `handoff` / `quote_result` do log JSONL.
Estimativa (raciocínio, não medido): com 20% de falha + 10% de lentidão e 3 tentativas,
p(cotação indisponível) = 0,30³ ≈ 2,7% — o resto do handoff vem de objeção, mídia e
fechamento.

## Custo

- Build: o prazo do desafio (~3 dias), uma pessoa.
- Por conversa: ~6–12 chamadas de LLM. Groq free tier: limites por modelo, ~30 req/min
  (fonte: guias públicos 2026, divergentes; não checado no console). Custo R$ 0 no demo.
- Suporte: handoff com motivo tipado; nenhum fluxo cria fila nova além da do vendedor.

## Preço / plano

Não muda preço nem plano: o agente só lê `/planos` e cota via `/quote`.

## O que quebra se der certo

- Volume: limite do free tier do LLM (trocar provedor = env var, por desenho).
- Legado instável: retry multiplica a carga na `/quote` (máx. 3×).
- Mídia: mais da metade das conversas manda arquivo/áudio que o agente não lê.

## Recorte mínimo (o que entra)

1. Conversa no terminal simulando WhatsApp (modo interativo e modo roteiro p/ log).
2. Qualifica: modelo/ano, idade, CEP, plano, data de início.
3. Cota via `/quote` com tratamento de falha por classe de erro.
4. Decide: resolve (cotação entregue, recusa explicada, dúvida respondida) ou handoff
   com motivo explícito (indisponível, pedido de humano, mídia repetida, objeção de preço,
   quer fechar — emissão exige pagamento/CPF, fora do agente —, erro de payload, LLM fora).
5. Rastreio JSONL com ids e status; PII mascarada antes do LLM e do log.
6. Silver mascarado do dataset + estatísticas acima no README.

## Versão completa (cortada de propósito)

Integração WhatsApp real, persistência de estado, UI, circuit breaker, emissão de apólice,
fila de handoff, avaliação automática em lote com o dataset, multi-idioma.

## Já existe no código?

Não: repo só tem o mock da API e o dataset.

## Recomendação

**Build now**, no recorte acima. Ticket: FDE-1 (já criado, critérios em `.workflow/tasks/FDE-1.json`).
