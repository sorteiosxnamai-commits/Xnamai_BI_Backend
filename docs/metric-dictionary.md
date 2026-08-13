# Dicionário de métricas

- **Pedido**: registro de pedido recebido da Mercos, independentemente do
  status.
- **Venda válida**: pedido cujo status normalizado pertence a `2` ou `pedido`.
- **Cancelamento**: pedido cujo status pertence a `0`, `5`, `cancelled` ou
  `cancelado`. A regra canônica está em `app/domain/order_status.py`.
- **Faturamento bruto**: total líquido acrescido dos descontos calculados pela
  diferença entre `preco_tabela` e `preco_liquido` históricos de cada item.
  Pedidos sem preços completos continuam usando `orders.total` e são
  identificados pela cobertura parcial.
- **Faturamento líquido**: soma de `net_total`; usa `orders.total` quando o
  líquido não existe na fonte.
- **Ticket médio**: faturamento líquido dividido pela quantidade de vendas
  válidas.
- **Desconto total**: soma das diferenças positivas entre preço de tabela e
  preço líquido, multiplicadas pela quantidade; usa o campo legado `discount`
  somente quando essa derivação não está disponível.
- **Taxa de cancelamento**: cancelamentos divididos por todos os pedidos nos
  mesmos filtros.
- **Compradores únicos**: clientes distintos com venda válida.
- **Novo comprador**: cliente cuja primeira venda válida ocorreu no período.
- **Comprador recorrente**: cliente comprado no período cuja primeira venda
  válida ocorreu antes dele.
- **Quantidade de itens**: soma de linhas de item registradas no cabeçalho.
- **SKUs**: soma dos produtos distintos por pedido.
- **Curva ABC**: participação acumulada no faturamento: A até 80%, B até 95%,
  C acima de 95%.
- **RFM**: notas 1–5 de recência, frequência e valor monetário entre os
  clientes contidos nos filtros.
- **Velocidade média**: quantidade vendida dividida pelos dias do período.
- **Cobertura estimada**: estoque atual dividido pela velocidade média.
  É indisponível para período `all` ou produto sem venda.
- **Valor de estoque**: estoque atual multiplicado pelo preço de tabela. Não é
  custo, CMV, margem ou lucro.

Todos os valores monetários persistidos usam `Numeric`/`Decimal`. A API pode
serializá-los como números JSON, mas nunca calcula totais globais no navegador.

## Reconciliação SQL

O faturamento líquido sem filtros adicionais deve fechar com:

```sql
SELECT COALESCE(SUM(COALESCE(net_total, total)), 0) AS net_revenue
FROM orders
WHERE LOWER(TRIM(status)) IN ('2', 'pedido', 'order')
  AND issued_at >= :date_from_utc
  AND issued_at < :date_to_exclusive_utc;
```

As datas recebidas pelo BI são convertidas de `America/Sao_Paulo` para UTC. O
limite final é exclusivo. Para reconciliar itens:

```sql
SELECT COALESCE(SUM(oi.total), 0) AS item_revenue
FROM order_items oi
JOIN orders o ON o.mercos_id = oi.order_mercos_id
WHERE LOWER(TRIM(o.status)) IN ('2', 'pedido', 'order')
  AND o.issued_at >= :date_from_utc
  AND o.issued_at < :date_to_exclusive_utc;
```

A diferença entre cabeçalhos e itens permanece explícita no relatório
`GET /api/v1/data-quality`; ela não é ocultada nem ajustada no navegador.
