# Deploy

## Ordem

1. Deploy do `Mercos_Adaptor`.
2. Configure e teste `GET /v1/orders/{mercos_id}`; pedidos usam Mercos v2.
3. Faça backup do PostgreSQL.
4. Deploy do backend. O container executa `alembic upgrade head` antes do
   Uvicorn.
5. Configure as credenciais de login.
6. Deploy do frontend somente com `VITE_BI_API_URL`.
7. Execute uma sincronização incremental e confira `/api/v1/data-quality`.

## Backend

Variáveis obrigatórias:

- `DATABASE_URL`
- `MERCOS_ADAPTOR_URL`
- `MERCOS_ADAPTOR_API_KEY`
- `BI_API_KEY` (somente integrações servidor-servidor legadas)
- `JWT_SECRET`
- `AUTH_ADMIN_USERNAME`, `AUTH_ADMIN_PASSWORD`
- `AUTH_VIEWER_USERNAME`, `AUTH_VIEWER_PASSWORD`
- A versão atual opera com autenticação desabilitada: qualquer pessoa com a
  URL pode consultar dados, sincronizar e exportar como administrador.
- `CORS_ORIGINS`
- `AUTH_COOKIE_SECURE=true`
- `AUTH_COOKIE_SAMESITE=none` quando frontend e API estiverem em domínios
  diferentes.

O CORS deve listar somente os domínios reais do frontend e permitir
credenciais. Nunca registrar tokens ou senhas.

## Frontend

- `VITE_BI_API_URL`

Não existe `VITE_BI_API_KEY`; segredos Vite são incorporados ao bundle e não
oferecem proteção.

## Verificação

```bash
python -m pytest -q
python -m ruff check app tests scripts alembic
python scripts/export_openapi.py
python scripts/audit_data.py --raw-fields --output data-quality-report.json
```

No frontend:

```bash
npm ci
npm test
npm run lint
npm run build
```

No `Mercos_Adaptor`:

```bash
python -m pytest -q
python -m ruff check app tests
```

O relatório real exige `DATABASE_URL` apontando para o PostgreSQL do BI. O
inventário de `raw` retorna apenas nomes, ocorrências e tipos, nunca valores.
