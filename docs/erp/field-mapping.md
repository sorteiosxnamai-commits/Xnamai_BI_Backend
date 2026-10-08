# Mapeamento campo a campo (gerado)

Gerado por `python scripts/erp_field_mapping.py`. Não edite à mão.
Campos de origem sem coluna tipada ficam no snapshot restrito e no inventário
de campos (`erp_field_inventory`), nunca descartados em silêncio.

## `categories` → `categorias` (v1) → `erp_categories`

Categorias. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `parent_external_id` | `categoria_pai_id`, `pai_id` | ident |  |
| `represented_external_id` | `representada_id` | ident | Representada/divisão (observado em produção); atributo, não faz parte da chave. |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `segments` → `segmentos` (v1) → `erp_segments`

Segmentos. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `order-types` → `pedidos/tipo` (v1) → `erp_order_types`

Tipos de pedido. Chave externa: `id`. `pedidos/tipo` permanece v1; não segue a regra v2 de pedidos.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `payment-conditions` → `condicoes_pagamento` (v1) → `erp_payment_conditions`

Condições de pagamento. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `minimum_order_value` | `valor_minimo` | dec |  |
| `consider_credit_limit` | `considerar_limite_credito` | boolean |  |
| `available_b2b` | `disponivel_b2b` | boolean |  |
| `represented_external_id` | `representada_id` | ident | Representada/divisão da conta (observado em produção). |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `price-tables` → `tabelas_preco` (v1) → `erp_price_tables`

Tabelas de preço. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `price_type` | `tipo` | text | Livre/acréscimo/desconto; imutável após criação. |
| `percentage` | `percentual`, `acrescimo_desconto` | dec |  |
| `surcharge_percent` | `acrescimo` | dec | Observado em produção (10/11 linhas). |
| `discount_percent` | `desconto` | dec | Existe no payload; nulo em todas as 11 linhas da amostra. |
| `represented_external_id` | `representada_id` | ident | Representada/divisão (observado em produção); atributo, não faz parte da chave. |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `carriers` → `transportadoras` (v1) → `erp_carriers`

Transportadoras. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `document` | `cnpj`, `cpf` | text |  |
| `phone` | `telefone` | text |  |
| `email` | `email` | text |  |
| `city` | `cidade` | text |  |
| `state` | `estado` | text |  |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `commercial-policies` → `politicas_comerciais` (v1) → `erp_commercial_policies`

Políticas comerciais. Chave externa: `id`. Identidade por slug + conta; IDs anteriores em id_history.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `descricao` | _catalog_name |  |
| `slug` | `slug` | text |  |
| `valid_from` | `data_inicio`, `vigencia_inicio` | day |  |
| `valid_to` | `data_fim`, `vigencia_fim` | day |  |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `users` → `usuarios` (v1) → `erp_sellers`

Vendedores. Chave externa: `id`. Vendedor externo; não é operador de login do ERP.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `nome`, `email` | derivado |  |
| `email` | `email` | text |  |
| `phone` | `telefone` | text |  |
| `is_admin` | `administrador`, `admin` | boolean |  |
| `access_blocked` | `acesso_bloqueado` | boolean |  |
| `active` | `ativo` | boolean |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `customers` → `clientes` (v1) → `erp_customers`

Clientes. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `name` | `razao_social`, `nome` | derivado |  |
| `trade_name` | `nome_fantasia` | text |  |
| `person_type` | `tipo` | text |  |
| `document` | `cnpj`, `cpf` | derivado | Documento alfanumérico preservado; sem remoção de caracteres. |
| `state_registration` | `inscricao_estadual` | text |  |
| `suframa` | `suframa` | text |  |
| `street` | `rua` | text |  |
| `number` | `numero` | text |  |
| `complement` | `complemento` | text |  |
| `district` | `bairro` | text |  |
| `zip_code` | `cep` | text |  |
| `city` | `cidade` | text |  |
| `state` | `estado` | text |  |
| `email` | `emails`, `email` | _first_email | Primeiro e-mail; demais em extras.emails. |
| `phone` | `telefone` | text |  |
| `mobile` | `celular` | text |  |
| `segment_external_id` | `segmento_id` | ident |  |
| `seller_external_id` | `vendedor_id`, `usuario_id` | ident |  |
| `blocked` | `bloqueado` | boolean |  |
| `block_reason` | `motivo_bloqueio` | text |  |
| `credit_limit` | `limite_credito` | dec |  |
| `active` | `ativo` | boolean |  |
| `notes` | `observacao`, `observacoes` | text |  |
| `extras` | `extras`, `emails`, `telefones`, `tags` | _customer_extras | Campos extras, e-mails/telefones adicionais e tags. |
| `source_created_at` | `data_criacao` | instant |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

Lista filha `contatos` → `erp_customer_contacts`:

| Coluna ERP | Campo de origem | Tipo |
|---|---|---|
| `external_id` | `id` | ident |
| `name` | `nome` | text |
| `role` | `cargo` | text |
| `email` | `emails`, `email` | _first_email |
| `phone` | `telefone` | text |
| `mobile` | `celular` | text |

Lista filha `enderecos, enderecos_adicionais` → `erp_customer_addresses`:

| Coluna ERP | Campo de origem | Tipo |
|---|---|---|
| `external_id` | `id` | ident |
| `kind` | `tipo` | text |
| `street` | `rua` | text |
| `number` | `numero` | text |
| `complement` | `complemento` | text |
| `district` | `bairro` | text |
| `zip_code` | `cep` | text |
| `city` | `cidade` | text |
| `state` | `estado` | text |

## `products` → `produtos` (v1) → `erp_products`

Produtos. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `code` | `codigo` | ident |  |
| `name` | `nome` | derivado |  |
| `unit` | `unidade` | text |  |
| `category_external_id` | `categoria_id` | ident |  |
| `kind` | `variacoes`, `variantes`, `grade_cores`, `grade_tamanhos` | _product_kind | Heurística de agregador de grade; validar com payload real da conta. |
| `list_price` | `preco_tabela` | dec |  |
| `minimum_price` | `preco_minimo` | dec |  |
| `external_stock` | `saldo_estoque` | dec |  |
| `commission_percent` | `comissao` | dec |  |
| `ipi_percent` | `ipi` | dec |  |
| `ncm` | `codigo_ncm`, `ncm` | text |  |
| `multiple` | `multiplo` | dec |  |
| `gross_weight` | `peso_bruto` | dec |  |
| `width` | `largura` | dec |  |
| `height` | `altura` | dec |  |
| `length` | `comprimento` | dec |  |
| `active` | `ativo` | boolean |  |
| `image_hashes` | `imagens`, `imagens_hash`, `hashes_imagens` | _product_images | Somente hashes SHA-512; nenhum link é fabricado a partir do hash. |
| `notes` | `observacoes` | text |  |
| `source_created_at` | `data_criacao` | instant |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

Lista filha `variacoes, variantes` → `erp_product_variants`:

| Coluna ERP | Campo de origem | Tipo |
|---|---|---|
| `external_id` | `id` | ident |
| `code` | `codigo` | ident |
| `name` | `nome` | text |
| `attributes` | `atributos`, `grade` | json_value |
| `external_stock` | `saldo_estoque` | dec |
| `price` | `preco_tabela`, `preco` | dec |

## `product-prices` → `produtos_tabela_preco` (v1) → `erp_product_prices`

Preços por produto/tabela. Chave externa: `id`. Chave externa composta; exclusão é sinalizada, não aplicada como delete genérico.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `product_external_id` | `produto_id` | ident |  |
| `price_table_external_id` | `tabela_preco_id` | ident |  |
| `price` | `preco` | dec |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

## `orders` → `pedidos` (v2) → `erp_sales_orders`

Pedidos e orçamentos. Chave externa: `id`.

| Coluna ERP | Campo(s) de origem | Tipo | Observação |
|---|---|---|---|
| `number` | `numero` | ident |  |
| `kind` | `status`, `situacao` | _order_kind |  |
| `commercial_status` | `status`, `situacao` | _order_status_text | Valor bruto da origem; separado de faturamento/atendimento/pagamento. |
| `billing_status` | `status_faturamento`, `faturamento` | text |  |
| `customer_external_id` | `cliente_id` | ident |  |
| `seller_external_id` | `criador_id`, `usuario_id`, `vendedor_id` | ident |  |
| `order_type_external_id` | `tipo_pedido_id` | ident |  |
| `payment_condition_external_id` | `condicao_pagamento_id` | ident |  |
| `price_table_external_id` | `tabela_preco_id` | ident |  |
| `carrier_external_id` | `transportadora_id` | ident |  |
| `commercial_policy_external_id` | `politica_comercial_id` | ident |  |
| `issued_at` | `data_emissao`, `data_criacao`, `ultima_alteracao` | _order_issue_instant |  |
| `issue_date` | `data_emissao` | day | Data sem hora permanece data. |
| `expected_delivery_date` | `data_entrega`, `previsao_entrega` | day |  |
| `gross_total` | `total_bruto`, `valor_bruto` | convert |  |
| `discount_total` | `valor_desconto`, `desconto_valor`, `desconto` | convert |  |
| `freight_total` | `valor_frete`, `frete` | convert |  |
| `net_total` | `total_liquido`, `valor_liquido`, `total` | convert |  |
| `notes` | `observacoes` | text |  |
| `shipping_address` | `endereco_entrega` | _address_dict |  |
| `extras` | `extras` | json_value |  |
| `source_created_at` | `data_criacao` | instant |  |
| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |
| `source_deleted` | `excluido` | boolean | Exclusão externa conserva histórico |

Lista filha `itens, items` → `erp_sales_order_items`:

| Coluna ERP | Campo de origem | Tipo |
|---|---|---|
| `external_id` | `id`, `item_id`, `pedido_item_id` | ident |
| `product_external_id` | `produto_id` | ident |
| `code` | `produto_codigo`, `codigo` | ident |
| `name` | `produto_nome`, `nome`, `descricao` | text |
| `quantity` | `quantidade` | derivado |
| `list_unit_price` | `preco_tabela` | dec |
| `unit_price` | `preco_liquido`, `preco_unitario`, `preco` | dec |
| `discount` | `desconto`, `desconto_de_cupom` | dec |
| `total` | `subtotal`, `total`, `quantidade` | _item_total |
| `excluded` | `excluido` | derivado |
| `notes` | `observacoes` | text |

