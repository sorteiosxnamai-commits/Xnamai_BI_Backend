# Dicionário de métricas

- **Pedido**: registro de pedido recebido da Mercos, independentemente do
  status.
- **Venda válida**: pedido cujo status normalizado pertence a `2` ou `pedido`.
- **Cancelamento**: pedido cujo status pertence a `0`, `5`, `cancelled` ou
  `cancelado`. A regra canônica está em `app/domain/order_status.py`.
- **Faturamento bruto**: soma de `quantidade × preço de tabela atual` dos itens
  válidos. Itens excluídos, pedidos sem itens e preços sentinela de R$ 1.000,00
  não entram no cálculo.
- **Faturamento líquido**: segue a mesma base de preço de tabela atual por regra
  comercial. O valor histórico original permanece no banco para auditoria, mas
  não compõe o faturamento analítico.
- **Ticket médio**: faturamento líquido dividido pela quantidade de vendas
  válidas.
- **Desconto total**: no faturamento analítico fica zerado, porque a regra
  comercial vigente usa o preço de tabela atual. O desconto histórico do
  pedido permanece no banco para auditoria.
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

O faturamento analítico sem filtros adicionais deve fechar com:

```sql
SELECT COALESCE(SUM(oi.quantity * p.list_price), 0) AS net_revenue
FROM order_items oi
JOIN orders o ON o.mercos_id = oi.order_mercos_id
JOIN products p ON p.mercos_id = oi.product_mercos_id
WHERE LOWER(TRIM(o.status)) IN ('2', 'pedido', 'order')
  AND COALESCE(oi.excluded, false) = false
  AND p.list_price <> 1000
  AND o.issued_at >= :date_from_utc
  AND o.issued_at < :date_to_exclusive_utc;
```

As datas recebidas pelo BI são convertidas de `America/Sao_Paulo` para UTC. O
limite final é exclusivo. O valor histórico do cabeçalho (`orders.total`)
permanece no relatório `GET /api/v1/data-quality` para auditoria; ele não
compõe mais o faturamento das telas.
