# Limitações conhecidas da fonte Mercos

- Custo real, CMV, margem e lucro não foram confirmados nos payloads
  disponíveis. O BI não calcula essas métricas.
- Comissão, frete efetivamente pago e recebimento financeiro também não estão
  confirmados.
- `cost_price` não foi criado: preço de custo só deve entrar após confirmação
  em amostra real e documentação da origem.
- Datas como cancelamento e entrega são persistidas somente quando os campos
  forem confirmados; atualmente a análise usa emissão/criação/alteração.
- Heatmap de hora não deve ser exibido até confirmar que o horário de emissão é
  confiável e não foi normalizado pela integração.
- Valor de estoque usa preço de tabela, não custo.
- Cobertura e risco de ruptura são estimativas baseadas em velocidade histórica;
  não substituem previsão de demanda.
- A lista v2 de pedidos não é tratada como fonte suficiente de itens. O backend
  consulta o detalhe v2 de cada pedido novo ou alterado antes de persistir.
- Campos desconhecidos permanecem em `raw` para auditoria. Nenhum campo
  analítico é inventado a partir de ausência na fonte.
