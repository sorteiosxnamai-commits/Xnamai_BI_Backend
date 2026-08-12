# Xnamai BI Backend

Backend analítico que consome o `Mercos_Adaptor`, persiste pedidos/itens e serve o frontend.

## Executar

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

No Render use **Docker**, variáveis do `.env.example` e health check `/health`.

## Produção (Render)

Obrigatório:
- `DATABASE_URL` — pooler Supabase porta 6543 (sem `?pgbouncer` / `&supa=`)
- `MERCOS_ADAPTOR_URL=https://mercosadaptor.onrender.com`
- `MERCOS_ADAPTOR_API_KEY` — mesma chave do Adaptor
- `BI_API_KEY` — chave que o frontend envia em `X-API-Key`
- `CORS_ORIGINS` — URL do front (ex.: `https://xnamai-bi-frontend.onrender.com,http://localhost:5173`)

## Primeira carga

```bash
curl -X POST "https://xnamai-bi-backend.onrender.com/api/v1/sync/all?full=true" \
  -H "X-API-Key: SUA_BI_API_KEY"
```

Depois o scheduler roda pedidos a cada `SYNC_ORDERS_MINUTES` e catálogo a cada `SYNC_CATALOG_HOURS`.

Swagger: `/docs`.
