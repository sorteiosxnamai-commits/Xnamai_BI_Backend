# Mercos Adaptor × ERP (Prompt 1) — estado

**Executado** em 08/10/2026 no repositório vizinho `MercosAdaptor` (sem commit/push/deploy). Detalhes,
matriz antes → depois e relatório de compatibilidade: `MercosAdaptor/docs/erp-extensions.md`.

## Entregue

| Requisito | Estado |
|---|---|
| Registro de capacidades + `GET /v1/capabilities` (aditivo; `/v1/resources` intacto) | feito |
| Leituras novas: títulos, pagamentos, formas de pagamento, promoções, comissões (filtros em allowlist; comissões por `ultimo_id`/`lastId`) | feito, **desligadas** até validar no sandbox |
| Escritas novas com DTO: produtos (POST/PUT), ajuste de estoque (saldo absoluto), cancelar pedido, faturamento (POST/PUT, sem GET), formas de pagamento | feito; produtos e cancelar **ligadas** (caminho confirmado), as demais desligadas |
| Escrita sem retry, `MeusPedidosID` preservado, nenhum ID fabricado | preservado e testado nas rotas novas |
| IDs de caminho validados, sem proxy aberto | feito (única mudança em rota legada: ID restrito a `[0-9A-Za-z_-]`) |
| Cota única da conta: gate FIFO + deadline compartilhado; Redis opcional fail-closed | feito; Redis testado só com `fakeredis` |
| Chave ERP com escopos, separada da legada (a legada **não** ganha as rotas novas) | feito |
| Matriz de lacunas | em `GET /v1/capabilities` (`notImplemented`) e no doc do Adaptor |
| Testes | 60 no Adaptor (23 originais intactos + 37 novos) + 8 ponta a ponta ERP × Adaptor real |

## Como o ERP usa

- ERP → Adaptor com `ERP_ADAPTOR_API_KEY` (= `MERCOS_ERP_API_KEY` do Adaptor). O Adaptor precisa de
  `MERCOS_ERP_SCOPES` com pelo menos `read`; escopos de escrita só os que forem liberados.
- `POST /api/v1/erp/integration/discover` consulta `GET /v1/capabilities` e atualiza, na matriz do ERP,
  o que o Adaptor **declara** (build + extensão ligada + escopo da chave). Isso não prova acesso da conta
  Mercos: `accountAccess` só muda com 200/403 reais.
- Capacidade que o Adaptor já oferece, mas o ERP ainda não liga (títulos, pagamentos, comissões,
  promoções, produto, estoque), aparece como "falta a ligação no ERP". **Cancelar pedido** já tem consumidor
  no ERP (`POST /v1/orders/{id}/cancel`, sem corpo), desligado por padrão (flag de pedidos, chave de escrita
  com escopo e extensão ligada) e confirmado só quando o espelho devolve o pedido cancelado. A ligação
  (mapeamento, serviço, tela, permissão) segue como próximo trabalho e depende do payload real do sandbox.

## Ainda não confirmado no Mercos

Caminho/paginação de pagamentos, promoções, formas de pagamento, ajuste de estoque, faturamento e
filtros de títulos. As rotas existem e estão testadas com transporte simulado, mas só serão ligadas
(`MERCOS_ENABLED_EXTENSIONS`) depois da validação no sandbox.

## Limites conhecidos

- Cota: fila FIFO sem reserva por consumidor; uma carga completa do ERP concorre com o BI.
- Só duas chaves (legada e ERP).
- Redis real não foi exercitado.

## Painel operacional de pedidos: dependências externas (sem contornos)

| Função | Situação | O que falta, especificamente |
|---|---|---|
| Cancelar pedido | consumidor no ERP, desligado | `ERP_WRITE_ORDERS`, chave ERP com escopo `write:order-cancel`, extensão ligada no Adaptor, **validação em sandbox Mercos** (nenhuma chamada real foi feita) |
| Faturamento (Mercos) | **não liberado** | Adaptor sem leitura (GET) de faturamento: sem como reconciliar o resultado. Não é emissão fiscal |
| Títulos / pagamentos Mercos | indisponível | extensões desligadas e payload real não validado |
| Frete automático | indisponível | provedor/contrato de frete (credenciais, tabela, API). Hoje só cotação **manual** local; selecionar não contrata |
| Nota fiscal (NF-e) | indisponível | emissor fiscal homologado (certificado, CNPJ emitente, regras tributárias). Rascunho local não é NF-e; emitir responde 409 `issuer_unavailable` |
| Pix / cobrança | indisponível | PSP/banco (credenciais, webhook de confirmação). Pagamento Pix fica sempre `unknown` |
| Reembolso efetivo | manual | integração bancária/PSP. O ERP só registra solicitação, aprovação e confirmação externa manual |
