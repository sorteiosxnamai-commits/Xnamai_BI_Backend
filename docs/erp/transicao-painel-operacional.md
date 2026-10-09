# Transição para o painel operacional de pedidos

Plano de origem: `XnamaiBI-ERP-plano-implementacao.md` (09/10/2026). Este documento é o registro vivo da
transição: baseline, matriz de reaproveitamento, mapa de rotas e plano de preview/rollback. Escopo:
somente `Xnamai_BI_Backend` e `Xnamai_BI_Frontend`. O MercosAdaptor foi apenas lido; nada nele foi alterado.
Nenhum commit, push, merge ou deploy foi feito com base no plano.

## 1. Baseline (antes das alterações)

| Item | Valor medido em 09/10/2026 |
|---|---|
| Backend, HEAD | `ee3e035` (árvore limpa) |
| Frontend, HEAD | `73b705d` (árvore limpa) |
| MercosAdaptor, HEAD | `12a549c` (somente leitura) |
| Revisão Alembic (head) | `20261007_01_erp_foundation` (aplicada em produção; **não regenerar**) |
| Rotas ERP no OpenAPI | 91 operações (47 GET, 37 POST, 4 PATCH, 2 PUT, 1 DELETE) |
| Rotas legadas (BI/CRM/Varejo) | 58 operações |
| Testes backend (SQLite) | 293 passed, 12 skipped (os pulados exigem PostgreSQL) |
| Frontend | `tsc -b` limpo; `biome lint` 0 erros (16 avisos já existentes); `vitest` 26 arquivos / 107 testes passando; `vite build` ok |
| Observação de estabilidade | `src/api/client.test.ts` (legado) estoura o tempo de 5 s quando roda em paralelo com outra carga pesada da máquina; sozinho passa. Pré-existente, fora do escopo |

**Versão editada x ZIPs do plano:** os ZIPs não estão disponíveis ao executor. O checkout atual contém
os commits de sincronização, lote, 401/403 e `limite_credito` posteriores à base dos ZIPs. A comparação foi
feita por commit, não por diff de arquivos.

## 2. Matriz de reaproveitamento

| Necessidade do plano | Já existe e é reutilizado | Novo |
|---|---|---|
| Lista e detalhe de pedidos | `GET /sales-orders`, `/sales-orders/{id}`, `ServerList`, `OrdersPage`, `OrderDetailPage` | busca ampliada, filtros, resumo operacional |
| Estados separados | `commercial_status`, `billing_status`, `fulfillment_status`, `payment_status` | estado operacional local explícito |
| Financeiro | `models/local.py`, `services/finance.py`, `routers/finance.py`, `FinancePage` | vínculo operacional com pedidos, métricas separadas |
| Estorno | `reverse_*` no serviço financeiro (conditional UPDATE, uma vez) | solicitação/aprovação de reembolso |
| Operação externa | `outbox`, `unknown`, reconciliação, `OperationTracker` | DTOs de cancelamento e faturamento |
| Autoria e auditoria | `audit()`, `ErpAuditEvent`, `idempotency` | eventos de frete/fiscal/reembolso |
| Permissões | `require(...)`, papéis em `auth.py` | permissões novas por domínio |
| Capacidades | `capabilities.py`, tela de Capacidades | itens de frete/fiscal/Pix com motivo |
| Layout | `ErpApp.tsx`, `erp.css`, `ui.tsx`, `StatePanel` | menu de 7 itens, cabeçalho, tema claro |
| Sessão expirada | `ERP_AUTH_EXPIRED`, aviso no login | cobertura para as novas telas |

## 3. Mapa de rotas

### Navegação (frontend)

| Menu | URL | Origem |
|---|---|---|
| Visão Geral | `/erp` | existente, adaptada |
| Pedidos | `/erp/pedidos` | existente, reestruturada |
| Nota Fiscal | `/erp/notas-fiscais` | **nova** |
| Frete | `/erp/frete` | **nova** |
| Financeiro | `/erp/financeiro` | existente |
| Reembolsos | `/erp/reembolsos` | **nova** |
| Configurações | `/erp/configuracoes` | **nova** (agrupa as abaixo) |

URLs preservadas dentro de Configurações: `/erp/integracoes`, `/erp/capacidades`, `/erp/admin`,
`/erp/clientes`, `/erp/produtos`, `/erp/cadastros`, `/erp/compras`, `/erp/estoque`, `/erp/externos`,
`/erp/conta` (Minha conta passa ao menu do usuário).

### API (backend, sob `/api/v1/erp`)

| Rota | Situação |
|---|---|
| `GET /sales-orders` | existente; parâmetros preservados, busca ampliada, filtros novos |
| `GET /operational-summary` | **proposta**, criada |
| `GET/POST /sales-orders/{id}/shipping-quotes`, `POST /shipping-quotes/{id}/select` | **propostas** |
| `GET/POST /sales-orders/{id}/invoice-drafts`, `GET/PATCH /invoice-drafts/{id}` | **propostas** |
| `GET/POST /refund-requests` e ações de aprovação/rejeição | **propostas** |

## 4. Preview local e retorno à interface anterior

- **Preview:** `VITE_ERP_ENABLED=true` e `ERP_ENABLED=true` em banco PostgreSQL **isolado** de desenvolvimento
  (nunca o de produção), com dados sintéticos. A interface nova substitui o layout do ERP na mesma rota
  `/erp`; o código do layout anterior permanece nos módulos de Configurações.
- **Retorno:** voltar à versão anterior do aplicativo (frontend e backend). As migrações da transição são
  **aditivas** (tabelas novas, sem alterar nem remover as existentes); o retorno de versão do aplicativo
  não exige desfazer o schema e não toca em dados, jobs nem checkpoints.

## 5. Matriz final: implementado, local e pendente externo

| Área | Implementado e testado | Só local (não é fato externo) | Pendente externo |
|---|---|---|---|
| Pedidos | lista com filtros/resumo/histórico, detalhe operacional | estados operacionais derivados | — |
| Cancelamento | consumidor Mercos via Adaptor, reconciliação pelo espelho | — | sandbox, flag e escopo da chave |
| Frete | cotação manual, seleção, estado obsoleto | selecionar ≠ contratar | provedor de frete |
| Nota Fiscal | rascunho com itens por origem, percentual | rascunho ≠ NF-e | emissor fiscal |
| Financeiro | recebível por pedido, baixa, vencido | Pix sempre `unknown` | PSP/banco |
| Reembolsos | solicitação, aprovação, confirmação externa manual, reversão local integral | pedido ≠ devolução | integração bancária |
| Faturamento Mercos | DTO e rota existem | — | **não liberado** (sem GET para reconciliar) |

## 6. Arquivos alterados (somente os dois projetos)

Backend: `app/erp/{services,routers,schemas,models}/` (order_operations, shipping, invoice_drafts,
order_finance, refunds, operations), `outbox.py`, `capabilities.py`, `auth.py`, `integrations/mercos_client.py`,
`sync/rows.py`, `alembic/versions/20261009_01_erp_operational_panel.py`, `openapi.json`, `docs/erp/*`, `tests/erp/*`.
Frontend: `src/erp/` (shell, rotas, features de pedidos, fiscal, frete, financeiro, reembolsos, configurações,
schemas, testes). MercosAdaptor, XNamaiAgent, Club e NewStore **não foram modificados**.

## 7. Publicação e retorno (não executados)

1. Aplicar `alembic upgrade head` no banco de produção (migração aditiva) **antes** do backend novo.
2. Publicar backend e depois frontend (`VITE_ERP_ENABLED` e flags de escrita permanecem como estão).
3. Retorno: voltar a versão do aplicativo; `alembic downgrade 20261007_01` só é necessário se as 7 tabelas
   novas precisarem sair (descarta apenas dados dessas tabelas).
