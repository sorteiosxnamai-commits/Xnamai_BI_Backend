# Xnamai BI Backend

Backend analítico independente que consome o `Mercos_Adaptor`, persiste pedidos completos e todos os itens, e disponibiliza métricas para o frontend.

## Executar

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

No Render selecione **Docker**, configure as variáveis do `.env.example` e use `/health` como Health Check.

## Primeira carga

Após o deploy, execute `POST /api/v1/sync/all?full=true` com o header `X-API-Key`. Depois use `full=false` nas sincronizações incrementais. O Swagger fica em `/docs`.

## Segurança

O navegador não deve receber a chave do `Mercos_Adaptor`. Somente este backend a utiliza. Para produção, o ideal é substituir `BI_API_KEY` por autenticação de usuários com JWT/SSO antes de liberar o painel fora da rede da empresa.
"# Xnamai_BI_Backend" 
