# Carrinhos B2B

A tela `/b2b-carts` consome `GET /api/v1/analytics/b2b-carts` com a autenticação
normal do BI e os filtros analíticos existentes. `abandonmentHours` aceita 1–8760
(padrão 24). A listagem tem `page`, `page_size` (até 100) e `stage`.

## Fonte e cobertura

Pedidos V2 documenta `status_b2b`: null = não utiliza B2B, 1 = em aberto,
2 = concluído. O Adaptor existente já usa V2 e preserva esses campos em `data`;
o BI conserva o payload em `orders.raw`. Não é necessária uma rota Mercos inventada.

Fonte: https://docs.mercos.com/reference/v2pedidos

Ainda falta validar na conta real se todos os carrinhos aparecem desde a primeira
adição, se o ID é mantido ao finalizar e como esvaziamentos/exclusões são expostos.
A metadata permanece parcial. Ausência de registros não comprova zero carrinhos.

## Definições

- Universo: registros atuais com `status_b2b` 1 ou 2, filtrados por
  `source_created_at`, nunca pela emissão. Registros sem criação só entram em
  períodos sem limites de datas e são contabilizados na qualidade global.
- Convertido: B2B 2 e status de venda válido. B2B concluído ainda como orçamento
  aparece separado; não equivale a pedido, faturamento ou pagamento.
- Possível abandono: B2B 1, status orçamento e última alteração do registro com
  idade maior ou igual ao prazo. Não é uma confirmação de inatividade do cliente.
  Sem última alteração, permanece em aberto sem classificação de abandono.
- Cancelamentos têm precedência. Combinações inconsistentes ficam em "a verificar".
- Em aberto total inclui possíveis abandonos. Os demais grupos da partição são
  exclusivos. Conversão = convertidos / todos os identificados (inclui cancelados).
- Valores são os totais atuais dos registros inteiros, inclusive com filtro de
  produto. Diferença = valor de possíveis abandonos menos valor convertido.
  Não representa receita perdida nem a mudança de valor de um mesmo carrinho.
- Datas e status representam a situação atual da coorte criada no período,
  não o número de eventos de conversão ocorridos naquele período.

## Histórico e carga

`alembic upgrade head` cria somente `b2b_cart_observations`. A sincronização passa
a guardar versões observadas de status, total e atualização; reprocessamento
da mesma versão não duplica observações. `observed_at` é a hora da coleta;
o backfill não inventa eventos anteriores nem datas de conversão.

Com as variáveis existentes do backend configuradas:

```powershell
python -m alembic upgrade head
python scripts/load_b2b_carts.py
```

Esse comando carrega observações a partir dos payloads já armazenados. Para
reler todos os pedidos do Adaptor antes do backfill:

```powershell
python scripts/load_b2b_carts.py --sync --full
```

O comando exige `MERCOS_ADAPTOR_URL` e `MERCOS_ADAPTOR_API_KEY`, usa as leases,
paginação e tratamento de limite existentes. Carga parcial/429 retorna código 2;
retome com `--sync` (sem `--full`) para usar o cursor salvo, respeitando os limites
do Mercos. Não altera pedidos na origem.

Validação operacional: escolher um carrinho conhecido, conferir retorno em aberto,
finalizá-lo pelo fluxo normal da empresa e comparar ID, status_b2b, status e total.
A primeira carga oferece apenas o estado atual, sem reconstruir transições antigas.
