# FDE-1 — verification evidence

**Verdict:** pass

**Verified:** tree `023fc6d4b57e` on `fde459cae7ca`

## `uv run pytest -q` — pass

```
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 64%]
........................................................................ [ 86%]
..............................................                           [100%]
334 passed in 52.22s
```

## `uv run ruff check agent tests scripts` — pass

```
All checks passed!
```

## `uv run --group silver python scripts/build_silver.py --check` — pass

```
conversas: 2500
mensagens: 26470
desfecho: {'em_negociacao': 0.303, 'ganho': 0.285, 'perdido': 0.215, 'sem_resposta': 0.197}
lead_acima_75: 0.112
veiculo_mais_de_20_anos: 0.212
recusaria_pela_api: 0.3
recusaria_mas_ganho: 203
conversas_com_midia: 0.568
midia_por_tipo: {'document': 774, 'image': 550, 'audio': 465}
conversas_com_objecao: 0.518
silver: data\silver\conversations.parquet (26470 mensagens)
mensagens com PII restante: 0; com primeiro nome do lead restante: 0
```

## `uv run python scripts/redact_ai_logs.py --check` — pass

```
ai-logs: 0 arquivo(s) com dado a redigir
```
