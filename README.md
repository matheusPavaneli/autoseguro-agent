# AutoSeguro: agente de atendimento e cotação

Agente que atende um lead de seguro auto pelo "WhatsApp" (terminal), qualifica, cota na API legada
instável e decide sozinho quando resolver e quando passar para um humano. Enunciado: [DESAFIO.md](DESAFIO.md).

```
lead ──► agent/cli.py ──► agent/core.py ──► LLMProvider (Groq | Ollama | OpenAI)   conversa e extrai dados
                              │
                              ├─► handoff.py      regras de passar pro humano (código, não prompt)
                              ├─► pii.py          mascara CPF/e-mail/telefone/placa/CEP antes do LLM e do log
                              ├─► quote_client.py POST /quote: timeout, retry por classe de erro
                              ├─► price_guard.py  nenhum valor em R$ sai do texto livre do LLM
                              └─► trace.py        logs/<execução>.jsonl, um evento por linha
```

## Como rodar

Requisitos: Python 3.12+ e [uv](https://docs.astral.sh/uv/). Docker é opcional.

```bash
# 1. API de cotação (terminal 1)
docker compose up --build                      # ou:
cd quote-service && uv run uvicorn app.main:app --port 8000

# 2. Configuração (key grátis em https://console.groq.com/keys)
cp .env.example .env                           # preencha LLM_API_KEY

# 3. Agente (terminal 2)
uv run --env-file .env python -m agent.cli                                              # conversa interativa
uv run --env-file .env python -m agent.cli --script scripts/demo_feliz.txt --name demo  # roteiro

# Testes e checagens (sem rede, sem LLM)
uv run pytest -q
uv run ruff check agent tests scripts
uv run --group silver python scripts/build_silver.py --check
```

Trocar o LLM: `LLM_PROVIDER=ollama` (self-hosted, sem key) ou `openai`, e opcionalmente `LLM_MODEL`
e `LLM_BASE_URL`. Nenhum código muda (ver [Decisões](#decisões-e-porquês), item 6).

## Logs de execução (entregável 4)

| Execução | O que mostra | Arquivos |
|---|---|---|
| Caminho feliz | 1ª chamada à `/quote` volta **503**, a 2ª cota; a cotação sai com pro-rata e carência; o lead fecha e vai para o humano | [logs/demo_feliz.md](logs/demo_feliz.md), [.jsonl](logs/demo_feliz.jsonl) |
| API fora | 5xx nas 3 tentativas (timeouts 3 s, 3 s, 9 s): handoff com mensagem fixa, **nenhum preço** | [logs/demo_indisponivel.md](logs/demo_indisponivel.md), [.jsonl](logs/demo_indisponivel.jsonl) |

Rodados em 2026-11-23 com `openai/gpt-oss-120b` no Groq. O caminho feliz usou `QUOTE_SEED=14`, que faz a 1ª chamada
falhar, e a API fora usou `QUOTE_FAILURE_RATE=1`. As faixas de idade do veículo dependem do ano corrente
(`quote_logic.py:31`), então os mesmos roteiros podem cotar diferente a partir de 2027-01-01.

Cotação do caminho feliz conferida à mão: Completo 209,90 × 1,00 (35 anos) × 1,00 (carro de 4 anos) × 1,30 (CEP com
prefixo 07, alto risco) = **R$ 272,87**. Início em 15/12, então o 1º pagamento é proporcional: 272,87 × 17/31 = **R$ 149,64**.

## Decisões e porquês

### 1. Falha da `/quote`: cada classe de erro tem uma resposta

| Resposta | O que o agente faz | Por quê |
|---|---|---|
| 200 | entrega a cotação | |
| 422 `cotacao_recusada` | **não repete**; explica o motivo ao lead e a conversa conta como resolvida | regra de negócio (idade > 75, carro > 20 anos): repetir dá o mesmo resultado |
| outro 4xx (inclui o 422 de validação do FastAPI, sem `motivo`) | **não repete**; handoff `invalid_request` | o payload nosso está errado; repetir não conserta e o lead não pode corrigir |
| 5xx, timeout, conexão | repete com backoff exponencial + jitter (0,5 s e 1 s) | transitório. A `/quote` é cálculo puro, então repetir não duplica nada |
| esgotou as tentativas | handoff `quote_unavailable` com mensagem fixa | **nunca** inventa preço e nunca prende o lead |

Timeouts por tentativa: **3 s, 3 s, 9 s**. A lentidão simulada dura 8 s. Cortar em 3 s e repetir é mais rápido
que esperar, e a última tentativa espera o bastante para aproveitar uma resposta lenta. Conta, com 20% de erro e
10% de lentidão por chamada:

- p(indisponível) = 0,30 × 0,30 × 0,20 = **1,8%** das cotações (com timeout fixo de 3 s seria 0,30³ = 2,7%);
- pior caso: 3 + 3 + 9 s de timeout + 1,5 s de espera ≈ **16,5 s** até o handoff.

Custo aceito: uma chamada abandonada por timeout segue ocupando um worker do servidor por até 8 s.

O que ficou de fora: circuit breaker. Com um agente e uma conversa por vez ele não muda o resultado, e com
volume é o próximo passo.

### 2. O LLM conversa, o código decide

O LLM extrai dado de texto livre ("e um Corolla 2022", "dia 15 de dezembro") e conduz a conversa. Ficam no código:

- **o texto da cotação** (`core.format_quote`): montado só com o que a API devolveu (mensalidade, franquia,
  coberturas, carência, pro-rata). O LLM recebe apenas "cotação já enviada" e o seguimento é uma frase fixa.
- **a guarda de preço** (`price_guard.py`): uma lista de permitidos. O texto livre do LLM só pode ter número com
  motivo claro: ano, data, "N anos" ou "N dias", item de lista, nome de modelo (HB20) ou um número que o lead
  digitou. Qualquer outro número, e qualquer cara de dinheiro explícita (`R$`, `262,38`, "fica em 340", "300
  reais"), troca a resposta por uma mensagem segura. Se acontecer duas vezes seguidas, vira handoff
  `unsafe_reply`. O prompt nem recebe o preço base nem a franquia dos planos.
- **o CEP**: extraído do texto cru por regex e guardado no estado. A API só usa os 2 primeiros dígitos, mas sem CEP
  ela não aplica o agravo de região (`quote_logic.py:41`) e cota **barato demais**. Por isso, sem CEP o agente não cota.
  Com dois CEPs na mesma mensagem ("moro no X, o carro dorme no Y"), ele não escolhe: pergunta qual é o do carro.
- ano-modelo maior que o ano corrente (ex.: 2027 vendido em 2026) vai como o ano corrente. Sem isso a API calcula
  idade de carro negativa e recusa um carro zero.

### 3. Critério de passar para o humano (`agent/handoff.py`)

O agente **resolve sozinho**: qualificar, cotar, explicar a cotação, explicar uma recusa por regra e tirar dúvidas
sobre coberturas.

| Motivo | Quando | Detectado por |
|---|---|---|
| `quote_unavailable` | API falhou nas 3 tentativas | código |
| `invalid_request` | 4xx que não é recusa de negócio | código |
| `llm_unavailable` | provedor fora, 429, 401, resposta inválida | código |
| `tool_loop_exhausted` | 4 rodadas de ferramenta sem resposta final | código |
| `unsafe_reply` | guarda de preço bloqueou 2 vezes seguidas | código |
| `lead_asked_human` | "quero falar com um atendente" | palavra-chave (o LLM cobre paráfrases) |
| `media_unreadable` | 2ª mídia seguida sem texto (1ª: pede para escrever) | código |
| `wants_to_close` | "fechado", "pode emitir"… **depois** de uma cotação | palavra-chave (o LLM cobre paráfrases) |
| `price_objection` | "caro", "desconto", concorrente… **depois** de uma cotação | palavra-chave (o LLM cobre paráfrases) |
| `out_of_scope` | sinistro, apólice existente, outro produto | só LLM (ferramenta `encaminhar_humano`) |

Por que fechamento e objeção vão para o humano: emitir apólice exige documentos e pagamento, e o agente não tem
esses sistemas. Desconto e ajuste de franquia são decisão comercial. No dataset, 51,8% das conversas têm objeção e
os vendedores respondem "posso ajustar a franquia", o que o agente não tem autorização para prometer.

Erro de palavra-chave cai do lado seguro: um falso positivo manda o lead para um humano, nunca para um preço errado.

### 4. Dados sensíveis

- O provedor externo de LLM **nunca recebe** CPF, e-mail, telefone, placa nem CEP crus (`pii.mask` antes de cada
  chamada; teste `test_raw_cpf_and_cep_never_reach_the_provider`). O agente também não pede CPF: não é necessário
  para cotar.
- O log mascara todo texto antes de gravar, mas não os ids gerados pelo sistema. A transcrição `.md` é gerada do
  log, então também sai mascarada.
- `scripts/build_silver.py`: camada Silver do dataset. Mascara o texto, troca o nome do lead por `lead_<conversation_id>`
  (um id que não sai do nome, então não dá para reverter por dicionário) e grava em `data/silver/`, que é ignorado pelo
  git. O `--check` falha se sobrar PII ou primeiro nome.
- `ai-logs/` passa por `scripts/redact_ai_logs.py`, que tira e-mail, caminho do usuário, keys e as instruções pessoais
  do autor. O `--check` dá 0 ocorrências.
- Lacuna conhecida: um nome digitado pelo lead ao vivo ("sou o João Silva") vai ao LLM sem máscara, porque detectar
  nome próprio de forma confiável pede um modelo de NER e está fora do escopo.

### 5. Rastreio

Cada linha de `logs/*.jsonl` é um evento com `ts`, `event` e `conversation_id`. Mensagens levam `message_id` (e as de
saída levam `reply_to`). Cada cotação tem `quote_id` e **cada tentativa HTTP** é um evento `quote_attempt` com
`attempt`, `timeout_s`, `status` e `latency_ms`. Também são registrados: `llm_call` (status e latência), `tool_call`,
`quote_result`, `guard_blocked`, `handoff` (motivo e detalhe) e `conversation_end` (status `resolvida` / `handoff` /
`aberta` e as cotações). A métrica do frame, % resolvidas sem humano com 0 preço fora da API, sai desses eventos.

### 6. Provedor de LLM trocável (inversão de dependência)

`agent/core.py` depende só de `LLMProvider` (`agent/llm/base.py`, um `Protocol` com tipos neutros). O formato
OpenAI fica isolado em `agent/llm/openai_compat.py`, e só `agent/llm/factory.py` decide qual provedor usar. Groq,
Ollama, OpenAI, OpenRouter e vLLM entram pelo mesmo adapter. Um provedor com API própria entra com um adapter novo,
e o agente não muda. A prova é que os testes rodam o agente inteiro com um `FakeProvider` roteirizado. O cliente
HTTP é `httpx`, a única dependência de runtime (sem SDK).

Por que Groq como default: é grátis, quem avalia só precisa criar uma key e há modelo grande com tool calling. Self-hosted
(Ollama) funciona com `LLM_PROVIDER=ollama`, mas exige baixar o modelo, e modelos de 7–8B erram mais em ferramentas.

### 7. O que o dataset ensinou (`uv run --group silver python scripts/build_silver.py`)

2.500 conversas, 26.470 mensagens:

- **30,0% dos leads seriam recusados pelas regras da API** (11,2% com mais de 75 anos; 21,2% com carro de mais de
  20 anos em 2026). Ainda assim, **203 dessas conversas terminaram "ganho"**: o vendedor vendeu o que a regra recusa.
- Os preços dos vendedores saem de uma lista fixa (129,90 … 389,90), e o Premium aparece com as coberturas do
  Essencial. **Por isso o dataset não é usado como few-shot**: ensinaria o modelo a inventar preço.
- 56,8% das conversas têm mídia sem transcrição (774 documentos, 550 imagens, 465 áudios). Daí a regra "pede texto
  na 1ª, humano na 2ª".
- Desfecho: 30,3% em negociação, 28,5% ganho, 21,5% perdido, 19,7% sem resposta.

## Testes e QA

`uv run pytest -q` roda sem LLM e sem rede externa. Os testes de integração sobem o `quote-service` real nas portas
8001 a 8009; num clone novo, a primeira vez precisa do `uv` baixar as dependências do serviço. A suíte cobre:
- unidade, contrato e integração com a API real (falha 100%, lentidão de 8 s recuperada na 3ª tentativa, recusas 422);
- todas as bordas das regras de negócio;
- fuzz com seed fixa sobre os formatos do gerador do dataset;
- segurança: prompt injection, PII em log e na transcrição, key em mensagem de erro, `--name` escapando de `logs/`;
- caos no LLM;
- E2E da CLI;
- desempenho, incluindo ReDoS;
- 6 mutações, todas pegas.

As revisões independentes acharam 13 bugs (QA) e 60 pontos (code-review nativo `xhigh`, 4 passadas),
todos corrigidos com teste. O destino de cada um está em [docs/review.md](docs/review.md). Os dois mais graves:
- a guarda deixava passar "fica 199 por mês" e virou lista de permitidos;
- a redação dos ai-logs vazava e-mail escapado, e o próprio `--check` não via, porque usava a mesma regex.

A avaliação com o LLM real (`scripts/qa_live.py`, código final, seed 21, 2026-11-23) está em
[logs/qa_live.json](logs/qa_live.json). Deu **7 de 9** cenários, com 0 preço inventado, 0 crash e 0 PII crua vista
pelo provedor, em 65 chamadas ao LLM. As duas falhas:
- `recusa_idade_80`: a recusa foi explicada corretamente, mas em seguida o LLM ofereceu um consultor (o prompt
  proíbe oferecer contorno) e o lead aceitou. O resultado foi seguro, mas a regra do prompt foi violada;
- `corrige_idade_apos_cotacao`: a API falhou 3 vezes seguidas por acaso (503, timeout, 502). O agente fez o handoff
  certo, mas o cenário de correção não chegou a ser exercitado.

Os 6 trechos de conversa reais do dataset rodaram sem preço inventado e sem crash.

## Limitações conhecidas

- **A guarda de preço, a detecção de handoff e o mascaramento de PII são heurísticas de regex.** Cada rodada de
  revisão achou mais casos de borda. Elas cobrem os formatos do dataset e todas as frases que as revisões levantaram,
  mas não são prova de que nenhuma frase passa. O desenho limita o estrago:
  - o LLM nunca recebe preço, então só um preço inventado poderia escapar;
  - o erro de palavra-chave cai do lado humano;
  - o detector de PII dos gates é independente do mascaramento.

  O passo seguinte seria a resposta do LLM sair só em campos estruturados (texto sem número + ação), em vez de texto
  livre filtrado.

- O LLM pode atribuir a um plano uma cobertura de outro (visto ao vivo no QA). O prompt proíbe, mas não há guarda em
  código para coberturas como há para preço. As coberturas que valem são as da cotação, montada pelo código.

- O LLM às vezes cota sem confirmar o resumo antes, apesar do prompt. O impacto é baixo, porque a cotação mostra o plano
  e o lead pode corrigir, mas uma regra de prompt não é garantia.
- O estado da conversa fica em memória: um processo atende uma conversa por vez, sem persistência nem fila de handoff.
- As palavras-chave de objeção e fechamento são em português, simples e conservadoras.
- O free tier do Groq tem limite por minuto: estourar vira handoff `llm_unavailable`, não erro.

## Processo e uso de IA

Feito com Claude Code no pipeline `workbench`: triage, frame ([docs/frame.md](docs/frame.md)), plano auditado
([docs/plano-sdd.md](docs/plano-sdd.md), com cada citação ao código conferida pela ferramenta), crítico adversarial do
plano, implementação, QA, verificação, review ([docs/review.md](docs/review.md)) e commit. As sessões, incluindo os
subagentes, estão em [ai-logs/](ai-logs/README.md).

**O que eu decidi** (está nas mensagens da sessão principal):
- recorte mínimo que cumpre os requisitos, sem nada além do necessário;
- LLM grátis e trocável sem mexer no agente (Groq por padrão, Ollama possível), com inversão de dependência;
- obrigar o uso do pipeline `workbench` (o primeiro rascunho, feito fora dele, foi descartado);
- uma rodada de QA cobrindo todos os tipos de teste;
- uma avaliação final independente da estrutura;
- a identidade dos commits;
- neste repositório, a transparência de IA que o desafio exige.

**O que a IA propôs e eu aceitei:**
- a divisão "o LLM conversa, o código decide";
- os timeouts 3/3/9 s, com a conta;
- a tabela de handoff;
- a guarda de preço;
- mascarar PII antes do LLM e do log;
- não usar o dataset como few-shot de preço, porque os preços dele são sorteados.

**Onde a IA errou e foi corrigida:**
- começou sem o pipeline;
- escolheu um modelo default do Groq que não existia mais (a 1ª execução real mostrou o 404);
- mascarou ids no log como se fossem CEP;
- a redação dos ai-logs vazou dado pessoal duas vezes, e o próprio verificador dela não via o vazamento;
- afirmou ter copiado artefatos que não existiam.

Esses erros foram pegos por execução real, pelo QA, pelo code-review e pela avaliação final, não por revisão de
texto.
