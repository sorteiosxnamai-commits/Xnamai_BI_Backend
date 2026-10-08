# Primeira ativação em homologação (consulta e sincronização apenas)

Não siga o "Para ligar" de `status.md` em produção. A primeira ativação é em **homologação**, com
um banco PostgreSQL isolado, **escritas externas desabilitadas** e conta Mercos de sandbox.

## 0. Pré-requisitos

- PostgreSQL de homologação **isolado** (não o banco de produção). O ERP usa as mesmas tabelas do BI
  no mesmo banco apenas se você decidir assim; para a primeira rodada, use um banco novo.
- Adaptor (repositório `MercosAdaptor`, com as extensões do Prompt 1) apontando para o **sandbox**
  do Mercos: `MERCOS_BASE_URL=https://sandbox.mercos.com/api/v1`. Para esta primeira rodada configure:
  `MERCOS_ERP_API_KEY=<chave só do ERP>`, `MERCOS_ERP_SCOPES=read` (**sem** escopos de escrita),
  `MERCOS_ENABLED_EXTENSIONS=` vazio. Várias réplicas: `MERCOS_REDIS_URL` (senão, 1 instância).
- No backend ERP, `ERP_ADAPTOR_API_KEY` = a chave `MERCOS_ERP_API_KEY` do Adaptor. Com
  `MERCOS_ERP_SCOPES=read` o Adaptor recusa qualquer escrita (403), mesmo que alguém ligue uma flag
  do ERP por engano: duas travas independentes.

## 1. Configuração (backend de homologação)

```
ERP_ENABLED=true
ERP_CONNECTION_ID=xnamai-homolog
ERP_BOOTSTRAP_ADMINS=<login admin do BI de homologação>
ERP_ADAPTOR_API_KEY=<MERCOS_ERP_API_KEY do Adaptor>   # leitura; escrita barrada pelo escopo do Adaptor
ERP_WRITE_CUSTOMERS=false
ERP_WRITE_ORDERS=false
ERP_WRITE_TITLES=false
ERP_ALLOW_BI_OPERATOR_LINK=false
ERP_JWT_SECRET=<segredo próprio, longo e aleatório>
ERP_WEBHOOK_SECRET_HEX=         # vazio: receptor responde 503 até você validar a assinatura
```

`alembic upgrade head`, depois `python -m app.erp.worker` (um processo). Frontend de homologação com
`VITE_ERP_ENABLED=true`.

## 2. Operadores individuais

1. Entre em `/erp` com **"Sou administrador: entrar com o login do BI"** (só o admin de bootstrap).
2. Administração › cadastre cada pessoa (login = e-mail, papéis) e use **Criar senha**: a senha
   temporária aparece **uma única vez**. Entregue por canal seguro.
3. Cada pessoa entra pelo login do ERP e é obrigada a trocar a senha. Sessões, bloqueio por
   tentativas e auditoria são individuais. O BI continua com o login atual, inalterado.

## 3. Sondagem do sandbox (somente leitura)

Primeiro, descubra o que o Adaptor declara: `POST /api/v1/erp/integration/discover` (atualiza a matriz
de capacidades; não prova acesso da conta Mercos). Depois a sonda:

```bash
python scripts/erp_sandbox_probe.py --out docs/erp/sandbox-report.json
```

Para cada recurso imprime linhas, paginação, **nomes e tipos de campos** (nunca valores) e os
campos **ainda sem mapeamento**. Registre aqui o que divergiu do esperado e ajuste `registry.py`
(depois `python scripts/erp_field_mapping.py`).

Checklist a confirmar com dados reais:

| Item | Como confirmar | Resultado |
|---|---|---|
| Campos de cada recurso | relatório da sonda; Integrações › Inventário de campos | ☐ |
| Paginação `nextCursor` × `pageCursor` e cursor repetido | rodar carga completa de `orders` e `customers`; conferir Execuções | ☐ |
| Empate de timestamp numa página inteira | procurar execução `failed` com "Cursor repetido" | ☐ |
| Permissões por recurso (403) | Capacidades › Conta; status `forbidden` por recurso | ☐ |
| Detalhe de pedido por ID (produção pode negar) | Pedidos incompletos; capacidade `read.orders` | ☐ |
| Status de pedido (0/5, 1, 2) | comparar `commercial_status` e `kind` de amostras | ☐ |
| Grade de produto / variações / imagens | detalhe de produtos reais | ☐ |
| Exclusão externa (`excluido`) e `product-prices` | `sourceDeleted` nos registros | ☐ |
| Escopo por divisão/representada | HTTP 200 não prova recorte; conferir registros | ☐ |

## 4. Webhook (somente depois da sondagem)

1. Defina `ERP_WEBHOOK_SECRET_HEX` com o segredo do sandbox e cadastre a URL no Mercos de sandbox.
2. Dispare um evento real. Em Integrações › Webhooks confira: assinatura aceita, evento gravado
   **antes** do 2xx, `scheduled` com o recurso certo, e o formato real do corpo.
3. Confirme o nome do cabeçalho do ID de entrega. Hoje o ERP aceita `X-Delivery-Id` (não confirmado);
   sem ele deduplica pelo hash do corpo numa janela.

## 5. Só depois: escritas no sandbox

Fora do escopo desta primeira rodada. Quando for a hora: `ERP_ADAPTOR_API_KEY` do Adaptor com escopo
de escrita, `ERP_WRITE_CUSTOMERS=true` primeiro, testar timeout (nenhuma duplicata), 429, retorno sem
ID (vira `unknown`) e reconciliação; só então pedidos. Rollback imediato: voltar a flag para `false`.

## 6. Critérios para liberar para a equipe

- Suíte `tests/erp` verde em PostgreSQL (`ERP_TEST_DATABASE_URL`), incluindo concorrência e migração.
- Relatório da sonda sem campos críticos desconhecidos e checklist acima preenchido.
- Cada operador com senha individual trocada; ninguém usando conta compartilhada.
- Rollback ensaiado (`ERP_ENABLED=false`) e worker único em execução.

## Sonda real já executada (08/10/2026, **produção**, somente GET, 1 página, 3 recursos)

Executada por pedido explícito, com as credenciais em variáveis de ambiente do processo (nada gravado
em arquivo) e o Adaptor local no meio. **Atenção: o `MERCOS_BASE_URL` informado era `app.mercos.com`
(produção), não sandbox.**

| Recurso | Resultado |
|---|---|
| `segments` | **403** para este token (recurso não liberado); o ERP trata como `forbidden`, sem loop |
| `payment-conditions` | 15 linhas numa página; `pageCursor` presente, `nextCursor` ausente; campos reais: `id, nome, excluido, ultima_alteracao, valor_minimo, considerar_limite_credito, disponivel_b2b, representada_id` |
| `users` | 30 linhas; `pageCursor` presente, sem `nextCursor`; campos reais: `id, nome, email, telefone, administrador, acesso_bloqueado, excluido, ultima_alteracao` |

Consequências aplicadas no mapeamento: novos campos (`considerar_limite_credito`, `disponivel_b2b`,
`representada_id`, `telefone`, `acesso_bloqueado`) ganharam coluna; `numero_parcelas` e
`dias_primeira_parcela`, que eu havia suposto, **não existem** no payload e foram removidos.
`representada_id` indica escopo por representada/divisão: confirme o recorte nas demais entidades.
Falta validar os demais 9 recursos, paginação com `nextCursor`, escritas e webhook.

## Segunda sonda de produção (08/10/2026, 4 leituras, só GET, sem 429, sem impacto observado)

| Recurso | Resultado |
|---|---|
| `carriers` | 403 (permissão da conta) |
| `commercial-policies` | 403 (permissão da conta) |
| `categories` | 122 linhas; `pageCursor` sem `nextCursor`; campos `id, nome, excluido, ultima_alteracao, categoria_pai_id (13/122), representada_id (122/122)` |
| `price-tables` | 11 linhas; `pageCursor` sem `nextCursor`; `id, nome, tipo, excluido, ultima_alteracao, acrescimo (10/11), desconto (0/11, nulo), representada_id (11/11)` |

Observação: `desconto` nulo em 11 linhas é **campo não observado com valor**, não prova de que
não exista; não remover. `representada_id` aparece em todos os registros de três recursos
(`payment-conditions`, `categories`, `price-tables`; ver sonda anterior): o escopo existe no
payload, mas **ainda não está provado** se os IDs são únicos na conta ou por representada nem se
os filtros restringem a leitura. Enquanto isso, nenhuma chave de identidade é alterada e escritas
dependentes de escopo permanecem desligadas.
