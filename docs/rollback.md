# Plano de rollback

1. Interrompa sincronizações e confirme que não há `sync_runs.status=running`.
2. Preserve o arquivo exportado e o relatório de qualidade do incidente.
3. Reverta frontend, backend e adaptador para as imagens anteriores, nessa
   ordem, sem executar uma carga completa.
4. Para rollback de schema, restaure preferencialmente o backup realizado antes
   do deploy.
5. `alembic downgrade -1` pode remover a última estrutura, mas conversões
   `Numeric → Float` perdem as garantias decimais; use apenas em homologação ou
   com backup confirmado.
6. Não remova `mercos_item_id` enquanto uma versão nova da sincronização estiver
   ativa.
7. Após restaurar, execute uma sincronização incremental. O cursor só avança
   depois do commit de cabeçalho e itens; não altere cursores manualmente.
8. Compare faturamento e cobertura com a consulta/relatório anterior antes de
   liberar o frontend.

As rotas legadas foram preservadas durante a migração. Elas permitem rollback
do frontend sem exigir rollback imediato da API analítica nova.
