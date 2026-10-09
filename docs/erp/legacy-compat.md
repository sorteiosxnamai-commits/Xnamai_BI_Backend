# Compatibilidade com BI, CRM e Análise Varejo

Comparação feita **antes e depois** da implementação, sobre o contrato OpenAPI publicado
(`openapi.json` do backend, 58 operações legadas em 58 caminhos) e sobre o banco:

| Verificação | Resultado |
|---|---|
| Operações legadas antes → depois | 58 → 58, nenhuma removida |
| Operações legadas (método+caminho) alteradas | 0 |
| Schemas legados alterados | 0 |
| Operações novas | 90 (46 GET, 37 POST, 4 PATCH, 2 PUT, 1 DELETE), todas sob `/api/v1/erp` |
| Tabelas legadas criadas/alteradas/removidas pela migração | 0 (`20261007_01` só cria `erp_*`) |
| Colunas legadas alteradas | 0 |
| Importação/sync/operações do ERP escrevem em tabelas legadas | não (teste compara o conteúdo de todas as tabelas legadas antes/depois) |
| Código `app/erp` importa `app.models`, `app.sync`, `app.adaptor` ou o `Base` legado | não (teste estático) |
| Suíte legada | 98 testes antes e depois, todos passando |
| Login do BI (`/api/v1/auth/*`) | inalterado; o ERP tem autenticação própria em `/api/v1/erp/auth/*` e token de tipo distinto (um não vale no outro) |

Mudanças em arquivos compartilhados, todas aditivas:

- `app/main.py`: `include_router(erp_router)` e `PATCH`/`PUT` em `allow_methods` do CORS. Nenhuma rota
  legada define PATCH/PUT, então o CORS não amplia acesso a módulos existentes.
- `alembic/env.py`: o metadata ERP entra em `target_metadata` junto com o legado.
- Nenhuma alteração em `sync.py`, `adaptor.py`, scheduler, cálculos de BI/CRM/varejo.

Frontend: `App.tsx` ganhou um ramo `/erp` antes do fallback `BiApp`; `HomeGate.tsx` ganhou o quarto
cartão (somente com `VITE_ERP_ENABLED=true`); `crm.css` ganhou regras da grade de quatro cartões;
`AuthProvider`, rotas, menus, filtros e caches do BI não foram tocados. `src/test/setup.ts` agora
limpa o DOM entre testes (sem `globals`, o cleanup automático do Testing Library não rodava).

## Verificação da transição (painel operacional)

- OpenAPI comparado com o `HEAD`: **58 operações legadas idênticas** (0 alteradas, 0 removidas); 26 operações
  novas, todas sob `/api/v1/erp`. Entre as rotas ERP já existentes, 3 mudaram: `GET /sales-orders`
  (parâmetros/filtros novos, aditivo) e `POST /sales-orders/{id}/cancel` e `/billings`, que antes só
  respondiam 409 e agora aceitam corpo (cancelamento) ou seguem 409 `capability_disabled` (faturamento).
- Migração `20261009_01` só cria/derruba as 7 tabelas novas; `20261007_01` permanece intacta.
- Rotas do ERP anterior (`/erp/clientes`, `/erp/produtos` etc.) preservadas em `ErpApp.tsx`.
