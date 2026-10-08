# Matriz de cobertura ERP × Mercos (gerada)

Gerado por `python scripts/erp_coverage.py`. Não edite à mão.

Legenda: **A** implementado no Adaptor analisado · **D** documentado pelo provedor, exige
extensão do Adaptor · **V** depende de inventário/plano da conta · **E** processo próprio do ERP.

> Cobertura completa da conta Mercos só pode ser afirmada depois de conciliar esta matriz com
> um inventário autorizado da conta (plano, recursos ativos, amostras reais). Nada aqui prova
> acesso real: `accountAccess` começa em `unknown` e só muda com evidência (200/403).

## 1. Os 12 recursos do Adaptor (leitura)

| # | Alias | Entidade Mercos | Versão | Tabela ERP | Depende de | Campos mapeados |
|---|---|---|---|---|---|---|
| 1 | `categories` | `categorias` | v1 | `erp_categories` | — | 4 (+0 listas filhas) |
| 2 | `segments` | `segmentos` | v1 | `erp_segments` | — | 2 (+0 listas filhas) |
| 3 | `order-types` | `pedidos/tipo` | v1 | `erp_order_types` | — | 2 (+0 listas filhas) |
| 4 | `payment-conditions` | `condicoes_pagamento` | v1 | `erp_payment_conditions` | — | 6 (+0 listas filhas) |
| 5 | `price-tables` | `tabelas_preco` | v1 | `erp_price_tables` | — | 7 (+0 listas filhas) |
| 6 | `carriers` | `transportadoras` | v1 | `erp_carriers` | — | 7 (+0 listas filhas) |
| 7 | `commercial-policies` | `politicas_comerciais` | v1 | `erp_commercial_policies` | — | 5 (+0 listas filhas) |
| 8 | `users` | `usuarios` | v1 | `erp_sellers` | — | 6 (+0 listas filhas) |
| 9 | `customers` | `clientes` | v1 | `erp_customers` | `segments`, `users` | 25 (+2 listas filhas) |
| 10 | `products` | `produtos` | v1 | `erp_products` | `categories` | 20 (+1 listas filhas) |
| 11 | `product-prices` | `produtos_tabela_preco` | v1 | `erp_product_prices` | `products`, `price-tables` | 3 (+0 listas filhas) |
| 12 | `orders` | `pedidos` | v2 | `erp_sales_orders` | `customers`, `products`, `users`, `payment-conditions`, `price-tables`, `carriers`, `commercial-policies`, `order-types` | 22 (+1 listas filhas) |

Cada recurso tem: importação paginada com checkpoint atômico, campos tipados, tela/detalhe,
snapshot de origem restrito e inventário de campos. Mapeamento campo a campo em
`field-mapping.md`.

## 2. Capacidades

| Chave | Capacidade | Sit. | Adaptor | Provedor | ERP implementa | Flag de escrita | Fonte | Motivo / pendência |
|---|---|---|---|---|---|---|---|---|
| `read.customers` | Leitura: Clientes | A | sim | sim | sim | — | F05 | — |
| `read.products` | Leitura: Produtos | A | sim | sim | sim | — | F06 | — |
| `read.orders` | Leitura: Pedidos e orçamentos (v2) | A | sim | sim | sim | — | F07 | — |
| `read.price-tables` | Leitura: Tabelas de preço | A | sim | sim | sim | — | F26 | — |
| `read.payment-conditions` | Leitura: Condições de pagamento | A | sim | sim | sim | — | F22 | — |
| `read.carriers` | Leitura: Transportadoras | A | sim | sim | sim | — | F23 | — |
| `read.commercial-policies` | Leitura: Políticas comerciais | A | sim | sim | sim | — | F16 | — |
| `read.categories` | Leitura: Categorias | A | sim | sim | sim | — | F06 | — |
| `read.segments` | Leitura: Segmentos | A | sim | sim | sim | — | F05 | — |
| `read.order-types` | Leitura: Tipos de pedido | A | sim | sim | sim | — | F07 | — |
| `read.product-prices` | Leitura: Preços por produto/tabela | A | sim | sim | sim | — | F17 | — |
| `read.users` | Leitura: Vendedores | A | sim | sim | sim | — | F15 | — |
| `write.customers` | Criar/editar cliente | A | sim | sim | sim | `ERP_WRITE_CUSTOMERS` | F05 | Escrita assíncrona via outbox; sem retry cego. |
| `write.orders` | Criar/editar pedido | A | sim | sim | sim | `ERP_WRITE_ORDERS` | F07 | Escrita assíncrona via outbox; v2. |
| `write.titles` | Criar/editar títulos | A | sim | sim | não | `ERP_WRITE_TITLES` | F09 | Rota existe no Adaptor, mas só habilita após leitura/reconciliação de títulos. |
| `read.titles` | Consultar títulos | D | sim | sim | não | — | F09 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. Alias titles → titulos. |
| `read.payments` | Pagamentos Mercos Pay | D | sim | sim | não | — | F10 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. Ativação da conta a verificar. |
| `write.products` | Criar/editar produtos | D | sim | sim | não | `ERP_WRITE_PRODUCTS` | F06 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. |
| `write.inventory_publish` | Publicar ajuste de estoque | D | sim | sim | não | `ERP_WRITE_INVENTORY_PUBLISH` | F08 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. Saldo é absoluto; exige autoridade definida. |
| `write.order_cancel` | Cancelar pedido | D | sim | sim | não | `ERP_WRITE_ORDERS` | F27 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. Operação dedicada documentada. |
| `write.billing` | Registrar/alterar faturamento | D | sim | sim | não | `ERP_WRITE_BILLING` | F28 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. |
| `read.commissions` | Comissões | D | sim | sim | não | — | F12 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. Chave própria `comissao_id`. |
| `read.product_images` | Imagens de produto | D | não | sim | não | — | F25 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. Leitura devolve hashes, não URLs. |
| `read.payment_methods` | Formas de pagamento | D | sim | sim | não | — | F21 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. |
| `read.promotions` | Promoções | D | sim | sim | não | — | F24 | O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta. ID pode mudar. |
| `webhooks.receive` | Receptor de webhooks Mercos | D/V | não | sim | sim | — | F13/F14 | Receptor implementado no ERP; habilitação e payloads reais da conta pendentes. |
| `scope.divisions` | Divisões/representadas | D/V | não | sim | não | — | F29 | Escopo da conta precisa ser inventariado; HTTP 200 não prova recorte. |
| `ref.tags_networks_extras` | Tags, redes, motivos de bloqueio, campos extras | V | não | não | não | — | — | Campos de referência chegam no payload; endpoints relacionados a inventariar. |
| `ref.goals_tasks_crm` | Metas, tarefas, atividades, oportunidades | V | não | não | não | — | — | Sem API validada; consta na matriz sem promessa. |
| `ref.tax_b2b_config` | Configurações tributárias e B2B | V | não | não | não | — | — | Sem API validada; consta na matriz sem promessa. |
| `local.suppliers_purchases` | Fornecedores e compras | E | não | não | sim | — | — | Processo próprio do ERP. |
| `local.inventory` | Depósitos, razão e reservas | E | não | não | sim | — | — | Processo próprio; publicação ao Mercos desabilitada. |
| `local.finance` | Contas a pagar, caixa e centros de custo | E | não | não | sim | — | — | Processo próprio; não confunde título Mercos. |
| `local.fiscal_banking` | Fiscal eletrônico, bancos, boletos, logística | E/V | não | não | não | — | — | Integrações específicas futuras; não inferir de campos ou links. |
| `legacy.bi_crm_retail` | BI, CRM e Análise Varejo atuais | Existente | não | não | sim | — | — | Permanecem acessíveis e inalterados. |

## 3. Extensões do Adaptor (Prompt 1 executado)

O `MercosAdaptor` agora oferece as rotas abaixo (ver `MercosAdaptor/docs/erp-extensions.md`),
mas as que não têm caminho/paginação confirmados vêm **desligadas** até a validação no sandbox
(`MERCOS_ENABLED_EXTENSIONS`). No ERP continuam fora de `enabled` até existir a ligação (serviço,
tela e permissão) e o payload real da conta:

- `read.titles`: alias `titles` → `titulos` (GET)
- `read.payments`: alias `payments` → `pagamentos` (GET)
- `write.products`: POST/PUT de produtos, com grades e IDs retornados
- `write.inventory_publish`: PUT de ajuste de estoque (saldo absoluto)
- `write.order_cancel`: `POST /api/v1/pedidos/cancelar/{id}`
- `write.billing`: entidade v1 `faturamento` (criar/alterar)
- `read.commissions`: alias `commissions` → `comissoes` (paginação própria, `comissao_id`)
- `read.product_images`: alias `product-images` → `imagens_produto`
- `read.payment_methods`: alias `payment-methods` → `formas_pagamento`
- `read.promotions`: alias `promotions` → `promocoes`

Já entregue no Adaptor: cota única da conta (local ou Redis, fail-closed), chave ERP com escopos
separados da chave legada, DTOs e allowlist de filtros, `GET /v1/capabilities`. Pendente lá:
imagens de produto (formato diferente) e escrita dos demais cadastros (métodos não confirmados).

