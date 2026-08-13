# Relatório de qualidade da base

O código da auditoria, o contrato e a tela estão implementados, mas este
checkout não possui acesso configurado ao PostgreSQL de produção. Portanto,
nenhum percentual real é registrado neste documento para evitar apresentar
amostra local ou valor inventado como dado da empresa.

Para gerar o artefato real:

```bash
DATABASE_URL="postgresql+psycopg://..." \
python scripts/audit_data.py --raw-fields --output data-quality-report.json
```

O resultado inclui:

- totais das cinco tabelas centrais;
- intervalo de emissão;
- cobertura de cliente, vendedor, produto, itens e status;
- pedidos sem itens e divergência cabeçalho × itens;
- zeros, duplicidades e JSONs vazios;
- cursores e última sincronização;
- inventário seguro dos nomes e tipos dos campos `raw`.

Critério operacional: ranking de produto só pode ser rotulado como confiável
quando `coverage.ordersWithItemsPct >= 95`. O frontend e a metadata analítica
exibem aviso de parcialidade abaixo desse limite.
