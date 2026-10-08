# ERP Xnamai — execução, flags e rollback

Nada disto foi instalado ou ativado em produção. O ERP nasce **desligado**.

## Variáveis (todas aditivas; ver `.env.example`)

| Variável | Padrão | Efeito |
|---|---|---|
| `ERP_ENABLED` | `false` | Desligado: todas as rotas `/api/v1/erp/*` respondem **404 `erp_disabled`** (inclusive por URL direta e o webhook); o worker não inicia |
| `ERP_CONNECTION_ID` | `xnamai` | Identidade local da integração (não é o CompanyToken) |
| `ERP_BOOTSTRAP_ADMINS` | vazio | Logins (e-mails) de admin JWT do BI que entram como `erp_admin` **por vínculo explícito**. Sem isto e sem linha em `erp_operators`, o acesso é negado |
| `ERP_ADAPTOR_API_KEY` | vazio | Chave exclusiva do ERP no Adaptor. Sem ela o ERP lê com a chave do BI, mas **não escreve** |
| `ERP_WRITE_CUSTOMERS` / `_ORDERS` / `_TITLES` / `_PRODUCTS` / `_INVENTORY_PUBLISH` / `_BILLING` | `false` | Flags de escrita por capacidade; desligada bloqueia por URL (409 `capability_disabled`), não só no menu |
| `ERP_JWT_SECRET` | derivado do `JWT_SECRET` | Segredo dos tokens individuais do ERP. Defina um próprio |
| `ERP_ACCESS_MINUTES` / `ERP_REFRESH_DAYS` | `15` / `7` | Vida do acesso e da sessão (refresh rotativo em cookie httpOnly) |
| `ERP_ALLOW_BI_OPERATOR_LINK` | `false` | Aceita o token do BI para operadores cadastrados (contas compartilhadas não identificam pessoas). Deixe desligado |
| `ERP_WEBHOOK_SECRET_HEX` | vazio | Segredo HMAC em hexadecimal. Vazio: receptor responde 503 |
| `ERP_WEBHOOK_MAX_BYTES` | `1000000` | Corpo máximo aceito (413 acima) |
| `ERP_SYNC_OVERLAP_SECONDS` | `5` | Sobreposição de janela na carga incremental |
| `ERP_INVENTORY_AUTHORITY` | `mercos` | Só um *default*: virar fonte exige corte conciliado explícito na API |

Frontend: `VITE_ERP_ENABLED=true` mostra o cartão e libera `/erp`. O backend continua sendo a
autoridade da flag e das permissões.

## Operadores e sessões individuais

O ERP tem autenticação própria (`/api/v1/erp/auth/*`), independente do login do BI, no mesmo
repositório: senha com scrypt, bloqueio por tentativas (5, 15 min), sessão revogável, refresh
rotativo (reuso derruba a sessão), troca obrigatória da senha temporária e auditoria. Fluxo inicial:
o administrador de bootstrap entra pelo login do BI, cadastra os operadores e gera senhas temporárias.
Roteiro completo de primeira ativação (somente consulta e sincronização) em `homologation.md`.

## Subir o worker (processo separado)

```bash
python -m app.erp.worker
```

Um processo por ambiente. Ele consome jobs persistidos (claim atômico + lease), a outbox de escrita
e o inbox de webhooks, e agenda sincronização incremental dos recursos vencidos. **Não suba o worker
dentro de cada processo web.** O claim é um `UPDATE` condicional, então uma duplicata acidental não
processa o mesmo job duas vezes, mas gasta cota.

No Render, seria um *Background Worker* com o mesmo `Dockerfile`, comando
`python -m app.erp.worker` e as mesmas variáveis do serviço web. Não foi adicionado ao
`render.yaml` para não criar um serviço pago sem decisão sua.

## Migração

```bash
alembic upgrade head      # revisão 20261007_01, parte do head verificado 20260831_02
```

Somente `CREATE TABLE`/`CREATE INDEX` de tabelas `erp_*`. Nenhuma tabela legada é tocada
(`tests/erp/test_isolation_contracts.py` verifica isso). O metadata do ERP é `ErpBase`, separado do
`Base` legado: o `create_all` do lifespan **não** cria nem altera tabelas ERP.

## Rollback

1. Desligue `ERP_ENABLED` (e `VITE_ERP_ENABLED`): as rotas passam a 404 e o worker para.
   Nada do BI/CRM/varejo é afetado; esta é a reversão normal.
2. Pare o worker. Operações `unknown` permanecem registradas para reconciliação posterior.
3. `alembic downgrade 20260831_02` remove as tabelas `erp_*` **e destrói dados financeiros e de
   auditoria do ERP**. Use somente em ambiente sem dados reais.

## Webhook do Mercos

`POST /api/v1/erp/webhooks/mercos` autentica por `X-Hub-Signature-256` (HMAC-SHA256 sobre os bytes
originais). O evento é persistido **antes** do 2xx e processado fora da requisição; o processamento
agenda uma consulta incremental do recurso afetado em vez de aplicar o payload (um webhook atrasado
nunca regride uma entidade mais nova). Catálogo suportado: `pedido.gerado/faturado/cancelado`,
`cliente.cadastrado/atualizado/bloqueio_atualizado/excluido`; `pagamento.atualizado` é registrado e
ignorado com motivo (recurso ainda não sincronizado). O cabeçalho de ID de entrega (`X-Delivery-Id`)
é opcional e **não confirmado** na documentação consultada: sem ele a deduplicação usa o hash do
corpo dentro de uma janela. Confirme com um payload real antes de depender disso.

## Cota do Mercos

O ERP limita concorrência local (um pedido ao Adaptor por vez, por processo) e respeita 429 com
espera dinâmica, mas **a coordenação entre BI, ERP e agente é responsabilidade do gateway**
(ver `adaptor-requirements.md`). Até lá, uma carga completa do ERP concorre com a do BI.

## Carga e sincronização sem interface (08/10/2026)

Quem processa `erp_jobs` em produção é o **scheduler do backend** (tarefa `erp_queue` a cada 30 s,
`ERP_QUEUE_IN_SCHEDULER=true`), sem worker separado. O claim de jobs é atômico no banco e há lease com
heartbeat; um recurso nunca é sincronizado por dois processos ao mesmo tempo (`ResourceBusy`).

- **Automático** (`ERP_AUTO_SYNC=true`): a cada ciclo o scheduler enfileira, por recurso e na ordem de
  dependência, o que está vencido. Recurso nunca sincronizado = carga inicial (paginada, com checkpoint
  confirmado na mesma transação da página); depois, incremental pelo checkpoint. Reinício não refaz carga
  completa e jobs ativos equivalentes não são duplicados. Recurso negado (403) é reavaliado só a cada
  24 intervalos; 401 do Adaptor é falha de autenticação e interrompe o job sem marcar recurso como negado.
- **Comando** (mesmos serviços de fila/lease/sync/checkpoint; só lê do Mercos, só grava `erp_*`):

```
python -m app.erp.cli status                       # estado por recurso e da fila
python -m app.erp.cli run [--resource R] [--timeout S]   # executa/retoma a carga e mostra o resultado
```

- **Versão em execução:** `GET /api/v1/erp/build` (sem dados de negócio) devolve commit do deploy
  (`RENDER_GIT_COMMIT`), flags do consumidor e das escritas.
- **Progresso:** log por página (`ERP sync <recurso> run=<id> página=N: consultando o Adaptor / respondeu
  N registros em Xs; gravando / gravada (persistidos, inalterados, quarentena)`), sem payload; o checkpoint
  guarda a última atividade (`last_attempt_at`).
