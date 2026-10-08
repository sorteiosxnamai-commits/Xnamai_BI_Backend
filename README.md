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
- `CORS_ORIGINS` — URLs do front (ex.: `https://xnamai-bi-frontend.vercel.app,http://localhost:5173`)

## Primeira carga

```bash
curl -X POST "https://xnamai-bi-backend.onrender.com/api/v1/sync/all?full=true" \
  -H "X-API-Key: SUA_BI_API_KEY"
```

Depois o scheduler roda pedidos a cada `SYNC_ORDERS_MINUTES` e, em rodízio,
um recurso de catálogo a cada `SYNC_CATALOG_MINUTES`. Respostas 429 liberam a
execução imediatamente para uma nova tentativa automática no próximo ciclo.

Swagger: `/docs`.

## ERP Xnamai (módulo isolado, desligado por padrão)

O ERP vive em `app/erp`, sob `/api/v1/erp`, com tabelas `erp_*` e metadata próprio. Ele sincroniza
com o Mercos pelo Adaptor, sem tocar nas tabelas nem no scheduler do BI. Ligue com `ERP_ENABLED=true`
e suba o worker com `python -m app.erp.worker`. Veja `docs/erp/`: `status.md` (o que foi feito e o
que não foi verificado), `deployment.md`, `coverage.md`, `field-mapping.md`,
`adaptor-requirements.md` e `legacy-compat.md`.
