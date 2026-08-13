# Filtros analíticos

Todos os endpoints em `/api/v1/analytics/*` usam o mesmo contrato. Listas são
enviadas repetindo o parâmetro: `sellerIds=1&sellerIds=2`.

- `period`: `7d`, `30d`, `90d`, `365d`, `ytd` ou `all`.
- `dateFrom`, `dateTo`: datas `YYYY-MM-DD`. Quando informadas, prevalecem sobre
  o início do período predefinido. O dia é interpretado em
  `America/Sao_Paulo`; a consulta ao banco usa UTC e `dateTo` é inclusiva.
- `granularity`: `day`, `week`, `month`, `quarter` ou `year`.
- `statuses`, `sellerIds`, `customerIds`, `productIds`, `categoryIds`,
  `states`, `cities`, `segmentIds`, `orderTypeIds`, `paymentConditionIds`:
  seleção múltipla.
- `minValue`, `maxValue`: limites monetários inclusivos.
- `activeOnly`: restringe cadastros dimensionais ativos.

Listagens também aceitam `page`, `page_size` (máximo 100), `search`, `sort` e
`order=asc|desc`. Os nomes de ordenação são validados por allowlist no FastAPI;
nenhum identificador recebido é interpolado em SQL.

Resposta paginada:

```json
{
  "items": [],
  "page": 1,
  "pageSize": 50,
  "totalItems": 0,
  "totalPages": 0,
  "sort": "issued_at",
  "order": "desc",
  "appliedFilters": {},
  "metadata": {
    "generatedAt": "...",
    "dataThrough": "...",
    "isPartial": false,
    "warnings": [],
    "quality": {}
  }
}
```
