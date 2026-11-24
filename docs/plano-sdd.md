# FDE-1 — implementation spec

**Preset** `solo-saas` · **Persona** security-reviewer

## Objective

Construir o agente de atendimento da AutoSeguro (DESAFIO.md): conversa com o lead, qualifica, cota via POST /quote tratando a instabilidade por classe de erro, e decide resolver ou encaminhar ao humano por regra explicita em codigo. LLM atras de interface (DIP) para trocar provedor sem tocar no agente; PII mascarada antes do LLM e do log; rastreio JSONL com ids e status.

## Critical zones

- **auth** — ai-logs/claude-code/session.jsonl
- **billing** — ai-logs/claude-code/subagents/critic-plan.jsonl
- **secrets** — .env.example
- **user-data** — agent/pii.py

## Evidence

- A /quote falha de proposito em 20% das chamadas por default  
  `quote-service/app/main.py:23` — `FAILURE_RATE = float(os.getenv("QUOTE_FAILURE_RATE", "0.20"))`
- As falhas sao 500, 502 ou 503 (transitorias, cabem retry)  
  `quote-service/app/main.py:55` — `kind = _rng.choice(["500", "502", "503"])`
- Lentidao simulada dura 8 s por default, entao o timeout por tentativa precisa ser menor  
  `quote-service/app/main.py:25` — `SLOW_SECONDS = float(os.getenv("QUOTE_SLOW_SECONDS", "8"))`
- Na lentidao a API dorme e depois responde normalmente  
  `quote-service/app/main.py:60` — `time.sleep(SLOW_SECONDS)  # simula timeout / lentidao`
- Recusa de regra de negocio volta 422 com motivo: resposta final, nao se repete  
  `quote-service/app/main.py:66` — `return JSONResponse(status_code=422, content={"error": "cotacao_recusada", "motivo": e.motivo})`
- Payload invalido volta 400: erro nosso, repetir nao muda o resultado  
  `quote-service/app/main.py:68` — `return JSONResponse(status_code=400, content={"error": "payload_invalido", "detalhe": str(e)})`
- Sem CEP a API nao aplica o agravo de regiao, entao cotar sem CEP subestima o preco  
  `quote-service/app/quote_logic.py:41` — `if not cep:`
- So os 2 primeiros digitos do CEP importam para a regra  
  `quote-service/app/quote_logic.py:43` — `pref = cep.replace("-", "").strip()[:2]`
- Inicio fora do dia 1 gera primeiro pagamento pro-rata, que o agente precisa informar  
  `quote-service/app/quote_logic.py:99` — `if inicio.day != 1:`
- Idade da API usa a data de hoje, entao veiculo velho e recusado conforme o ano corrente  
  `quote-service/app/quote_logic.py:62` — `hoje = dt.date.today()`
- Lead acima de 75 anos e recusado  
  `quote-service/data/plans.json:33` — `{ "idade_min": 76, "idade_max": 200, "recusar": true, "motivo": "Idade acima do limite de aceitacao (75 anos)." }`
- Veiculo com mais de 20 anos e recusado  
  `quote-service/data/plans.json:39` — `{ "anos_min": 21, "anos_max": 200, "recusar": true, "motivo": "Veiculo com mais de 20 anos nao e aceito." }`
- Roubo e furto tem carencia de 30 dias, que o agente precisa informar  
  `quote-service/data/plans.json:49` — `"dias": 30`
- Requisito: nao travar nem inventar preco quando a infraestrutura falha  
  `DESAFIO.md:24` — `3. Não trava nem inventa preço quando a infraestrutura falha.`
- Requisito de entrega: log de uma execucao completa com cotacao saindo  
  `DESAFIO.md:74` — `4. **Log de uma execução completa** (uma conversa do início ao fim, com a cotação saindo).`
- Criterios de avaliacao: falha da /quote, handoff explicito, rastreio com id e status, dados sensiveis  
  `DESAFIO.md:120` — `- **Dá pra rastrear o que aconteceu?** (cada mensagem/cotação, com id e status.)`
- O gerador espera PII mascarada numa camada Silver  
  `scripts/generate_dataset.py:10` — `mas inventados -- o candidato deve mascara-los na camada Silver.`
- Precos do dataset sao sorteados e a descricao de cobertura e fixa: nao servem de exemplo de preco ao LLM  
  `scripts/generate_dataset.py:127` — `add("vendedor", vend, f"Show! Pelo perfil consigo o plano {plano} por R$ {preco},90/mes. Cobre colisao, roubo e furto. Te interessa?")`
- Nao existe codigo de agente no repo ainda (greenfield)  
  no match for `def handle(` in `.`
- FastAPI valida o payload antes da rota: idade fora de 0..200 ou ano fora de 1950..2100 tambem voltam 422, sem o campo motivo  
  `quote-service/app/main.py:34` — `idade: int = Field(..., ge=0, le=200)`
- Idade do veiculo = ano corrente - ano informado; ano-modelo maior que o ano corrente cai fora das faixas e e recusado  
  `quote-service/app/quote_logic.py:31` — `anos = hoje.year - ano_veiculo`

## Files touched

- `pyproject.toml` (add) — projeto uv do agente: httpx (unica dep de runtime); dev: pytest, ruff; grupo silver: pandas, pyarrow
- `uv.lock` (add) — lockfile commitado (gerado pelo uv, nao escrito a mao)
- `.gitignore` (edit) — ignorar data/silver/ (derivado); !.env.example para o exemplo ser versionado
- `.env.example` (add) — variaveis LLM_PROVIDER/LLM_API_KEY/LLM_MODEL/QUOTE_URL sem valor real
- `README.md` (add) — como rodar, decisoes e porques, criterio de handoff, numeros medidos do dataset
- `agent/__init__.py` (add) — pacote
- `agent/llm/__init__.py` (add) — exporta o contrato
- `agent/llm/base.py` (add) — contrato neutro: ChatMessage, ToolSpec, ToolCall, LLMReply, LLMProvider (Protocol), LLMError
- `agent/llm/openai_compat.py` (add) — unico adapter: POST /chat/completions via httpx para qualquer API compativel (Groq, Ollama, OpenAI); sem SDK
- `agent/llm/factory.py` (add) — unico ponto que escolhe o provedor a partir do ambiente
- `agent/pii.py` (add) — mascara CPF/email/telefone/CEP/placa; extrai CEP por codigo
- `agent/trace.py` (add) — Tracer (Protocol) e JsonlTracer; todo texto mascarado
- `agent/quote_client.py` (add) — QuoteGateway (Protocol) e QuoteClient com timeout, retry so em transitorio, resultado tipado
- `agent/handoff.py` (add) — HandoffReason (enum), mensagem fixa por motivo, deteccao deterministica de pedido de humano e de midia
- `agent/price_guard.py` (add) — bloqueia qualquer numero com cara de dinheiro no texto do LLM (R$, 1.234, 12,34, 12.34): o preco so sai pelo texto que o codigo monta da cotacao
- `agent/prompts.py` (add) — system prompt (planos lidos de GET /planos, data de hoje) e ToolSpecs cotar/encaminhar_humano
- `agent/core.py` (add) — Agent: estado da conversa, loop LLM-ferramentas, aplica handoff e guarda de preco, emite eventos
- `agent/cli.py` (add) — terminal simulando WhatsApp: interativo e --script; grava logs/<nome>.jsonl e gera logs/<nome>.md a partir dele
- `scripts/build_silver.py` (add) — dataset -> silver mascarado (data/silver/) + estatisticas; --check falha se sobrar PII
- `scripts/demo_feliz.txt` (add) — roteiro do lead: caminho feliz ate a cotacao e o fechamento
- `tests/__init__.py` (add) — pacote de testes
- `tests/test_quote_client.py` (add) — classificacao de erro e retry via httpx.MockTransport
- `tests/test_pii.py` (add) — cada tipo mascarado; idade e ano preservados; CEP extraido sem confundir com CPF
- `tests/test_price_guard.py` (add) — valor da cotacao passa; valor inventado e bloqueado
- `tests/test_core.py` (add) — fluxos do agente com fakes: cotacao ok, indisponivel, recusa, midia, pedido de humano, LLM fora, sem CEP
- `logs/demo_feliz.jsonl` (add) — entregavel: rastreio da execucao completa real (gerado pela CLI)
- `logs/demo_feliz.md` (add) — entregavel: transcricao legivel da mesma execucao
- `ai-logs/README.md` (add) — entregavel: indice das sessoes de IA exportadas e o que foi redigido
- `tests/conftest.py` (add) — FakeProvider roteirizado e fabrica de QuoteClient com httpx.MockTransport; sem rede
- `tests/test_trace.py` (add) — AC4/AC5: campos obrigatorios por evento e nenhum CPF/CEP cru no JSONL
- `tests/test_factory.py` (add) — AC6: groq/ollama/openai resolvem base_url e modelo; key ausente falha claro
- `scripts/demo_indisponivel.txt` (add) — roteiro rodado com QUOTE_FAILURE_RATE=1: termina em handoff QUOTE_UNAVAILABLE
- `logs/demo_indisponivel.jsonl` (add) — rastreio da API fora (gerado pela CLI)
- `logs/demo_indisponivel.md` (add) — transcricao da API fora
- `scripts/redact_ai_logs.py` (add) — copia as sessoes (principal + subagentes) para ai-logs/ redigindo e-mail, caminho de usuario, keys (gsk_/sk-) e o CLAUDE.md global; --check sai 1 se sobrar algo
- `ai-logs/claude-code/session.jsonl` (add) — entregavel: sessao principal redigida (subagentes entram pelo mesmo script; lista corrigida no plano quando os nomes existirem)
- `ai-logs/claude-code/subagents/critic-plan.jsonl` (add) — entregavel: sessao do subagente critico do plano, redigida
- `ai-logs/claude-code/subagents/qa-specialist.jsonl` (add) — entregavel: sessao do subagente de QA, redigida
- `tests/qa_support.py` (add) — QA: helpers compartilhados (sobe quote-service real em porta 8001-8008, fakes de LLM) para os testes novos
- `tests/test_qa_units.py` (add) — QA: lacunas de unidade em handoff, price_guard, pii, format_quote/brl, prompts, factory e traducao do openai_compat
- `tests/test_qa_contract_integration.py` (add) — QA: contrato do payload contra QuoteRequest real e integracao QuoteClient/Agent com o quote-service real (falha 1, lento 1, mix com seed, 422)
- `tests/test_qa_business_rules.py` (add) — QA: regras de negocio ponta a ponta (faixas etarias, idade do veiculo, CEP alto risco, pro-rata, carencia) via API real
- `tests/test_qa_fuzz.py` (add) — QA: propriedades com random de seed fixa para mask, has_money e extract_cep
- `tests/test_qa_security.py` (add) — QA: prompt injection (lead e tool result), PII em log/transcricao, vazamento de key, abuso de argumento da CLI
- `tests/test_qa_resilience.py` (add) — QA: caos do LLM e da API (timeout, 429, 500, JSON ruim, argumentos invalidos, ferramenta desconhecida, resposta vazia, loop)
- `tests/test_qa_cli_e2e.py` (add) — QA: conversa ponta a ponta por agent.cli.main com LLM fake, transcricao .md/.jsonl, status e motivo de handoff
- `tests/test_qa_perf.py` (add) — QA: orcamento de latencia no pior caso e concorrencia do tracer
- `tests/test_qa_mutation.py` (add) — QA: mutacoes de alto valor aplicadas em copia do fonte, prova de que algum teste falha por mutacao
- `scripts/qa_live.py` (add) — QA: avaliacao ao vivo com Groq real e quote-service real, cenarios fixos e amostra do dataset
- `logs/qa_live.json` (add) — QA: resultado mascarado da avaliacao ao vivo (scripts/qa_live.py)
- `docs/frame.md` (add) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `docs/plano-sdd.md` (add) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `docs/review.md` (add) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `docs/evidence.md` (add) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `ai-logs/claude-code/subagents/code-review-1.jsonl` (add) — entregavel: sessao do code-review nativo, redigida
- `ai-logs/claude-code/subagents/code-review-2.jsonl` (add) — entregavel: sessao do code-review nativo, redigida
- `tests/test_redact_ai_logs.py` (add) — redacao dos ai-logs (zona user-data) nao tinha teste; vazou duas vezes

## Amendments

Added after the plan passed its audit:

- `ai-logs/claude-code/subagents/critic-plan.jsonl` (2026-11-23T22:51:24Z) — entregavel: sessao do subagente critico do plano, redigida
- `ai-logs/claude-code/subagents/qa-specialist.jsonl` (2026-11-23T22:51:27Z) — entregavel: sessao do subagente de QA, redigida
- `tests/qa_support.py` (2026-11-23T22:55:39Z) — QA: helpers compartilhados (sobe quote-service real em porta 8001-8008, fakes de LLM) para os testes novos
- `tests/test_qa_units.py` (2026-11-23T22:55:40Z) — QA: lacunas de unidade em handoff, price_guard, pii, format_quote/brl, prompts, factory e traducao do openai_compat
- `tests/test_qa_contract_integration.py` (2026-11-23T22:55:41Z) — QA: contrato do payload contra QuoteRequest real e integracao QuoteClient/Agent com o quote-service real (falha 1, lento 1, mix com seed, 422)
- `tests/test_qa_business_rules.py` (2026-11-23T22:55:42Z) — QA: regras de negocio ponta a ponta (faixas etarias, idade do veiculo, CEP alto risco, pro-rata, carencia) via API real
- `tests/test_qa_fuzz.py` (2026-11-23T22:55:43Z) — QA: propriedades com random de seed fixa para mask, has_money e extract_cep
- `tests/test_qa_security.py` (2026-11-23T22:55:44Z) — QA: prompt injection (lead e tool result), PII em log/transcricao, vazamento de key, abuso de argumento da CLI
- `tests/test_qa_resilience.py` (2026-11-23T22:55:45Z) — QA: caos do LLM e da API (timeout, 429, 500, JSON ruim, argumentos invalidos, ferramenta desconhecida, resposta vazia, loop)
- `tests/test_qa_cli_e2e.py` (2026-11-23T22:55:47Z) — QA: conversa ponta a ponta por agent.cli.main com LLM fake, transcricao .md/.jsonl, status e motivo de handoff
- `tests/test_qa_perf.py` (2026-11-23T22:55:48Z) — QA: orcamento de latencia no pior caso e concorrencia do tracer
- `tests/test_qa_mutation.py` (2026-11-23T22:55:49Z) — QA: mutacoes de alto valor aplicadas em copia do fonte, prova de que algum teste falha por mutacao
- `scripts/qa_live.py` (2026-11-23T22:55:50Z) — QA: avaliacao ao vivo com Groq real e quote-service real, cenarios fixos e amostra do dataset
- `logs/qa_live.json` (2026-11-23T22:55:51Z) — QA: resultado mascarado da avaliacao ao vivo (scripts/qa_live.py)
- `docs/frame.md` (2026-11-24T00:12:51Z) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `docs/plano-sdd.md` (2026-11-24T00:12:52Z) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `docs/review.md` (2026-11-24T00:12:54Z) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `docs/evidence.md` (2026-11-24T00:12:55Z) — artefato do pipeline publicado (o .workflow e ignorado pelo git)
- `ai-logs/claude-code/subagents/code-review-1.jsonl` (2026-11-24T00:14:24Z) — entregavel: sessao do code-review nativo, redigida
- `ai-logs/claude-code/subagents/code-review-2.jsonl` (2026-11-24T00:14:25Z) — entregavel: sessao do code-review nativo, redigida
- `tests/test_redact_ai_logs.py` (2026-11-24T00:25:30Z) — redacao dos ai-logs (zona user-data) nao tinha teste; vazou duas vezes

## Steps

1. Criar pyproject.toml (python >=3.12; runtime so httpx; dev pytest e ruff; grupo silver pandas e pyarrow) e gerar uv.lock com uv lock — `pyproject.toml`
2. Contrato do LLM em tipos neutros e Protocol; adapter OpenAI-compat sobre httpx; fabrica por env (groq default, ollama, openai). core.py importa so agent.llm.base — `agent/llm/base.py`
3. pii.mask (ordem: email, CPF, telefone, CEP, placa; padroes pelo valor, sem depender do rotulo, case-insensitive, ancorados sem digito vizinho); extract_cep remove CPF e telefone antes — `agent/pii.py`
4. Tracer Protocol; JsonlTracer append com lock; todo str passa por mask; eventos: message_in, message_out, llm_call, tool_call, quote_attempt, quote_result, handoff, guard_blocked, conversation_end; campos de id (*_id, reply_to) nao passam pelo mask: id hexadecimal com 8 digitos seguidos virava [CEP] (bug visto na 1a execucao real) — `agent/trace.py`
5. QuoteClient: tentativas 1 e 2 com timeout 3 s, a 3a com 9 s (recupera a resposta lenta de 8 s); backoff 0,5 s * 2^(n-1) + jitter; 200 Ok; 422 com error=cotacao_recusada Refused; qualquer outro 4xx (inclui 422 de validacao do FastAPI) Invalid; 5xx/timeout/conexao repete e depois Unavailable; um quote_id por cotacao, um evento por tentativa — `agent/quote_client.py`
6. handoff: motivos QUOTE_UNAVAILABLE, INVALID_REQUEST, LLM_UNAVAILABLE, TOOL_LOOP_EXHAUSTED, LEAD_ASKED_HUMAN, MEDIA_UNREADABLE (2a midia seguida sem texto), WANTS_TO_CLOSE e PRICE_OBJECTION (palavra-chave, so depois de cotacao entregue), UNSAFE_REPLY (2o bloqueio seguido do guard), OUT_OF_SCOPE (so via ferramenta do LLM); cada um com mensagem fixa — `agent/handoff.py`
7. price_guard: qualquer numero com cara de dinheiro no texto do LLM bloqueia a resposta (troca por mensagem segura e emite guard_blocked) — `agent/price_guard.py`
8. prompts: system prompt com planos de GET /planos (sem preco base), data de hoje, regras de conduta (pedir modelo/ano, idade, CEP, plano, data de inicio; informar carencia e pro-rata; nunca citar preco sem ferramenta); ToolSpecs cotar(plano_id, idade, veiculo_ano, data_inicio) e encaminhar_humano(motivo enum) — `agent/prompts.py`
9. core.Agent.handle: ja em handoff responde fixo sem LLM; regras deterministicas de handoff antes do LLM; extrai CEP do texto cru para o estado; LLM recebe so texto mascarado; loop de ferramentas (max 4 rodadas, senao TOOL_LOOP_EXHAUSTED); cotar sem CEP ou com data invalida devolve erro ao LLM; ano maior que o corrente vira o corrente (ano-modelo); QuoteOk: o CODIGO monta e envia o texto da cotacao (plano, premio, franquia, coberturas, carencia, pro-rata) e o LLM so recebe a confirmacao de envio; Refused: motivo ao LLM; Unavailable/Invalid/LLMError: handoff com mensagem fixa; resposta final passa no price_guard; ajuste da 1a execucao real (2026-11-23): apos QuoteOk o codigo envia a pergunta de seguimento fixa e encerra o turno sem nova chamada ao LLM (gpt-oss degenerou em texto invisivel); caracteres zero-width removidos de todo texto do LLM — `agent/core.py`
10. cli: python -m agent.cli [--script arquivo] [--name demo]; linhas que comecam com [audio]/[documento]/[imagem] viram midia; grava logs/<nome>.jsonl e logs/<nome>.md — `agent/cli.py`
11. build_silver: le o parquet, mascara message_body, troca sender_name do lead e o primeiro nome dele no texto (limite de palavra) por lead_<conversation_id>; nome do vendedor fica (funcionario, nao titular do dado); grava data/silver/conversations.parquet; imprime estatisticas; --check sai 1 se pii.mask ainda achar algo — `scripts/build_silver.py`
12. Testes com fakes; sem rede — `tests/test_core.py`
13. Subir quote-service (uv run uvicorn, porta 8000) com QUOTE_SEED que force ao menos 1 falha antes do 200 e rodar demo_feliz; reiniciar com QUOTE_FAILURE_RATE=1 e rodar demo_indisponivel; commitar logs gerados — `logs/demo_feliz.jsonl`
14. README com decisoes (chamada abandonada ocupa 1 thread do servidor ate 8 s; nome do lead digitado ao vivo nao e mascarado, lacuna conhecida; logs datados porque faixas de idade do veiculo mudam em 1o de janeiro); ai-logs via scripts/redact_ai_logs.py, gate --check com zero ocorrencias — `README.md`
15. Correcoes dos achados do QA (2026-11-23, 35 xfail estritos): price_guard pega inteiro em contexto de preco, decimal de 1 casa, 'R $', conto/pila; pii com regex de e-mail limitado (ReDoS) e formatos fixo, celular '9 8765', CEP com ponto, CPF com espacos; openai_compat recusa arguments nao-objeto, redige a key no erro e repete 1x em 429/5xx/timeout; quote_client trata 200 malformado como transitorio; core valida faixas antes da API, repete a rodada em resposta vazia; handoff com guarda de negacao e fechamento curto, mais palavras de objecao e 'ta'; cli valida --name, erro claro com /planos fora, nao apaga log com roteiro inexistente, transcricao sem quebra de linha forjavel; prompt proibe citar cobertura fora da lista. Remover os xfail que passam; test_single_429 passa a afirmar o retry — `agent/core.py`
16. Correcoes do code-review nativo xhigh (15 achados, todos reproduzidos): price_guard passa a allowlist (remove numeros legitimos e bloqueia qualquer numero restante que o lead nao digitou) + denylist sem a isencao 'em' e com franquia/por/mensal/BRL; SAFE_MESSAGE distinta sem cotacao; CEP so com um unico candidato na mensagem e sem pegar RG; _valid_quote valida carencia/coberturas e core formata antes de registrar; quote_client trata RequestError e 408/425/429 como transitorios; Retry-After limitado e finito; negacao local (3 palavras) em fechamento e objecao, objecao sem 'orcamento'/'mais barato' solto/banco sem oferta; placa nao pega modelo+ano; qa_live pareia cotacoes com payloads filtrados e checa porta; README com 6/9 do live — `agent/price_guard.py`
17. VOLTA AO PLANO (2a passada do code-review xhigh com 11 achados importantes, 2026-11-23). Decisoes: (a) redacao dos ai-logs com padroes tolerantes a escape (\. e \@), rotulo da empresa, usuario do Windows e provedor do e-mail; --check com detector generico independente; (b) handoff deterministico so com regras de alta precisao (midia repetida; pedido de humano dirigido a empresa e com negacao; fechamento com frase forte; objecao com termo forte e sem pergunta comparativa); concorrente e casos ambiguos ficam com o LLM; code recusa quer_fechar/objecao_preco do LLM antes de haver cotacao; (c) guarda: ano so permitido se o lead digitou ou e o ano atual/proximo, meses com fronteira de palavra, contagem pequena + substantivo permitida, modelo exige 2+ letras; (d) CEP nao sobrescreve o guardado sem rotulo 'cep' e nao aceita data DDMMAAAA; mascara aceita CEP com espaco, celular sem DDD e com ponto, placa com cara de ano depois de 'placa'; (e) detector amplo independente em pii para os gates; (f) cli apaga o log so depois de subir o Agent; guard_strikes zera no turno da cotacao; testes e2e da CLI com data injetada; (g) artefatos do plano copiados para docs/ (o .workflow e ignorado) — `agent/handoff.py`
18. Code-review xhigh do novo ciclo (15 achados, 8 importantes, todos reproduzidos): guarda pega numero por extenso, radicais de verbo de preco (fic*, cust*, sa*), nome de plano + numero (mesmo ecoado do lead), dia 1-31 so em contexto de data; pii nao mascara par de anos como telefone, mascara celular de 9 digitos sem separador e placa com ate 3 palavras depois de 'placa'; CEP so entra (1a vez ou correcao) se formatado ou com 'cep' logo antes; fechamento ignora pergunta, 'fechar com' e pedido de outra cotacao; _valid_quote exige numero finito > 0 e nao bool; openai_compat repete 1x o 400 tool_use_failed; redacao tira o caminho do perfil e o bloco de memoria; qa_live usa porta propria no pior caso — `agent/price_guard.py`
19. 2a passada do code-review xhigh do novo ciclo (15 achados), com o teto decidido pelo dono em 2026-11-23 (regressao real corrige; caso de borda de heuristica vira limitacao, sem novo volta ao plano). Corrigido: resultado da ferramenta cotar leva coberturas/carencia/pro-rata sem preco e prompt com regra de carencia; CEP so com 8 digitos quando a mensagem e so o CEP; guarda sem 'por' e 'reais' soltos, plano+numero sem ano/modelo/contagem, unidades 'mensal'/'no mes'/'% de desconto', eco do lead so de numero de modelo ou ano; ano em contexto de veiculo so com nome capitalizado ou ano/modelo/carro; fechamento com 'voces', clausula termina em virgula/mas, pergunta curta nao fecha, 'sim' ao seguimento fixo fecha; encaminhar apos recusa passa; OverflowError; RG e cartao mascarados; franquia >= 0; nome da conta do perfil redigido; limites de desempenho com folga — `agent/core.py`

## Tests

- **unit** `tests/test_quote_client.py` — 503, 503, 200 resulta em QuoteOk com attempts=3 e tres eventos quote_attempt com o mesmo quote_id
- **unit** `tests/test_quote_client.py` — timeout em todas as tentativas resulta em QuoteUnavailable com attempts=3, sem excecao vazando
- **unit** `tests/test_quote_client.py` — 422 com error=cotacao_recusada resulta em QuoteRefused com o motivo da API apos exatamente 1 tentativa
- **unit** `tests/test_quote_client.py` — 400, e 422 de validacao sem error=cotacao_recusada, resultam em QuoteInvalid apos exatamente 1 tentativa
- **unit** `tests/test_quote_client.py` — ConnectError e repetido; a 3a tentativa usa timeout maior que a lentidao simulada
- **unit** `tests/test_pii.py` — CPF, email, telefone, CEP e placa do formato do dataset somem do texto; '35 anos' e 'Corolla 2008' ficam intactos
- **unit** `tests/test_pii.py` — extract_cep acha o CEP e nao confunde com digitos de CPF nem de telefone +55 11 9XXXX-XXXX na mesma frase
- **unit** `tests/test_price_guard.py` — texto do LLM com R$ 262,38, 262,38, 262.38 ou 4.500 e bloqueado; texto com 35 anos e Corolla 2022 passa
- **unit** `tests/test_core.py` — cotacao indisponivel termina em handoff QUOTE_UNAVAILABLE, a resposta nao contem R$ e o LLM nao e chamado depois
- **unit** `tests/test_core.py` — 422 chega ao LLM como recusa e a conversa nao vira handoff
- **unit** `tests/test_core.py` — cotar sem CEP no estado nao chama a API e devolve erro de CEP ao LLM
- **unit** `tests/test_core.py` — o texto enviado ao provedor nunca contem o CPF nem o CEP crus que o lead digitou
- **unit** `tests/test_core.py` — LLMError vira handoff LLM_UNAVAILABLE com mensagem fixa, sem excecao para o chamador
- **unit** `tests/test_core.py` — duas midias seguidas sem texto geram handoff MEDIA_UNREADABLE; 'quero falar com um atendente' gera LEAD_ASKED_HUMAN
- **unit** `tests/test_core.py` — resposta do LLM com preco inventado e trocada pela mensagem segura e gera evento guard_blocked
- **unit** `tests/test_core.py` — apos QuoteOk o texto enviado ao lead traz premio, franquia, carencia e pro-rata exatamente como a API devolveu
- **unit** `tests/test_core.py` — fechado depois da cotacao gera WANTS_TO_CLOSE; achei caro gera PRICE_OBJECTION; LLM que so chama ferramenta 4 vezes gera TOOL_LOOP_EXHAUSTED
- **unit** `tests/test_core.py` — ano 2027 e enviado a API como o ano corrente
- **unit** `tests/test_trace.py` — todo evento tem conversation_id; message_in/out tem message_id; quote_attempt tem quote_id, attempt, status e latency_ms; o arquivo nao contem o CPF nem o CEP crus do teste
- **unit** `tests/test_factory.py` — LLM_PROVIDER groq/ollama/openai resolvem base_url e modelo default; groq sem LLM_API_KEY sai com erro claro
- **regression** `tests/test_trace.py` — um message_id/quote_id com 8 digitos seguidos chega intacto ao JSONL
- **unit** `tests/test_core.py` — apos QuoteOk a 2a mensagem e a pergunta de seguimento fixa e o LLM nao e chamado de novo no turno; texto do LLM com zero-width sai limpo

## Verification

```
uv run pytest -q
```
```
uv run ruff check agent tests scripts
```
```
uv run --group silver python scripts/build_silver.py --check
```
```
uv run python scripts/redact_ai_logs.py --check
```

## Rollback

Projeto novo, sem deploy, sem dado gravado fora do repo: reverter o commit ou apagar o branch FDE-1. O quote-service e o dataset originais nao sao alterados.

## Product

- **metric** — % de conversas resolvidas sem humano (cotacao entregue ou recusa explicada), com 0 precos fora da API; medida nos eventos quote_result, handoff e conversation_end do log
- **who asked** — Enunciado do desafio (DESAFIO.md) e o dono; dado medido em .workflow/FDE-1/frame.md
- **cost** — R$ 0 no demo (Groq free tier); ~6 a 12 chamadas de LLM por conversa; ate 3 chamadas a /quote por cotacao
- **pricing impact** — nenhum: o agente so le /planos e cota via /quote

## Open questions

- Resolvido 2026-11-23: llama-3.3-70b-versatile nao existe mais no Groq (404 model_not_found na 1a execucao); default trocado para openai/gpt-oss-120b, listado em GET /models da conta.
- Limites do Groq free tier: fontes publicas divergem (30 RPM, 1K a 14,4K RPD por modelo); nao checado no console.
- Avaliacao final pelo Fable: dono liberou como excecao pontual em 2026-11-23 (so esta avaliacao).
