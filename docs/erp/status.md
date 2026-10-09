# ERP Xnamai × Mercos — estado da implementação

Atualizado em 08/10/2026. Nada foi ativado em produção, commitado, enviado nem implantado. O ERP
nasce **desligado** (`ERP_ENABLED=false`, `VITE_ERP_ENABLED=false`). A primeira ativação deve seguir
`homologation.md` (consulta e sincronização, escritas externas desabilitadas).

## Situação dos cinco próximos passos

| # | Passo | Estado |
|---|---|---|
| 1 | Revisar o código (migração, permissões, financeiro, estoque, worker) | **Feito** (autorrevisão): ver `review-2026-10-08.md`. 9 defeitos corrigidos, riscos residuais listados. Falta revisão independente |
| 2 | Concluir o Mercos Adaptor (Prompt 1) | **Feito** em `MercosAdaptor` (sem commit/deploy): capacidades, chave ERP com escopos, cota única (local/Redis fail-closed), rotas novas com DTO, extensões não confirmadas desligadas. 60 testes no Adaptor + 8 ponta a ponta ERP × Adaptor real. Ver `adaptor-requirements.md` |
| 3 | Validar em PostgreSQL isolado | **Feito** em PostgreSQL 16.2 real (embutido, só para teste): suíte do ERP inteira, concorrência de saldo e baixa com threads, constraints `CHECK` no banco e cadeia de migrações legado+ERP (sobe, desce, sobe) |
| 4 | Testar no sandbox Mercos | **Preparado, não executado** (sem credenciais): `scripts/erp_sandbox_probe.py` (somente leitura) e checklist em `homologation.md` |
| 5 | Pendências operacionais | **Feito**: estorno de recebimento de compra; edição dos itens do pedido; **autenticação individual dentro do módulo ERP** (login próprio, sessões revogáveis, troca de senha), sem alterar o login do BI |

## O que existe

- **Backend** `app/erp`: 49 tabelas `erp_*`, sync dos 12 recursos (checkpoint atômico, quarentena,
  inventário de campos), outbox com `unknown`/conflito/reconciliação, receptor de webhook (HMAC),
  compras (com estorno de recebimento), estoque operacional (razão imutável, reservas, transferências,
  ajuste absoluto, estorno causal, autoridade por escopo), financeiro local, autenticação individual.
- **Frontend** `src/erp`: portal com quarto cartão, `/erp` com lazy loading e boundary, login próprio
  do ERP, troca obrigatória da senha temporária, Minha conta (senha e sessões), administração de
  operadores (senha temporária exibida uma vez, encerrar sessões), edição de itens de pedido,
  estorno de recebimento, e as demais telas já existentes.
- **Autenticação individual:** scrypt com sal, política mínima de senha, bloqueio após 5 falhas
  (15 min), refresh rotativo em cookie httpOnly com detecção de reuso (derruba a sessão), revogação
  imediata ao desativar o operador ou redefinir a senha, auditoria sem segredos. O login do BI não foi
  tocado; tokens ERP e BI não são intercambiáveis. O vínculo legado de operador por token do BI fica
  **desligado** (`ERP_ALLOW_BI_OPERATOR_LINK=false`); só o administrador de bootstrap entra pelo BI,
  para cadastrar os operadores.

## Verificação executada (nesta máquina)

- Backend, SQLite: `ruff` limpo; **266 testes passam, 1 pulado** (o PG-only); `openapi.json` estável.
- Backend, **PostgreSQL 16.2 real**: suíte completa verde, incluindo o teste exclusivo de migração
  (resultado na linha "PostgreSQL final" ao fim).
- Compatibilidade: 58 operações legadas antes e depois, 0 alteradas, 0 schemas legados alterados;
  90 operações novas (46 GET, 37 POST, 4 PATCH, 2 PUT, 1 DELETE), todas sob `/api/v1/erp`; contagem do OpenAPI atual em 08/10/2026, todas sob `/api/v1/erp` (`legacy-compat.md`).
- Frontend: `tsc` limpo, build ok, `biome` sem novos avisos (16 já existentes), **104 de 105 testes
  passam** na suíte inteira; o único que falha é o legado `CustomersPage`, que estoura o timeout de
  5 s em carga paralela nesta máquina e passa isolado (já falhava antes das mudanças).

## O que continua NÃO verificado

1. **Conta e payloads reais do Mercos.** Campos, paginação, permissões, respostas de escrita e
   assinatura/formato do webhook seguem inferidos. Use `homologation.md` e a sonda.
2. **Adaptor.** Rotas e cota prontas e testadas com Mercos simulado; extensões sem caminho/paginação
   confirmados seguem **desligadas** até o sandbox. O ERP ainda não *consome* títulos, pagamentos,
   comissões, promoções, produto e estoque (falta mapeamento, serviço e tela, que dependem do payload
   real). Cancelar pedido tem consumidor desligado por padrão; faturamento segue não liberado. Redis real não foi exercitado (só `fakeredis`).
3. **Produção.** Nada foi aplicado em banco real nem em serviço de produção. O worker não está no
   `render.yaml` (criaria serviço pago).
4. **Limites do PostgreSQL embutido.** É um servidor PostgreSQL 16 de verdade, mas rodou local, sem
   pooler (o BI usa o pooler do Supabase na 6543, sem prepared statements). Repita no ambiente de
   homologação com o mesmo pooler.
5. **Escopo ainda fora:** emissão fiscal, boletos, logística, divisões/representadas, cadastros
   dependentes de plano da conta, custo médio de estoque, 2FA.

## Como ligar (somente homologação)

Veja `homologation.md`. Resumo: banco isolado, `ERP_ENABLED=true`, `ERP_BOOTSTRAP_ADMINS`,
`ERP_JWT_SECRET`, **sem** `ERP_ADAPTOR_API_KEY` e com todas as `ERP_WRITE_*` em `false`;
`alembic upgrade head`; um worker; frontend com `VITE_ERP_ENABLED=true`.

PostgreSQL (suíte completa, legado + ERP, PostgreSQL 16.2) após as mudanças de lease/capacidades: **287 testes passam, 0 falhas, 0 pulados** (inclui os 3 de payloads observados) (08/10/2026).

## Estado em 08/10/2026 (rodada de revisão)

**a) Implementado e testado (PostgreSQL 16 isolado, 284 testes)**
- Migração `20261007_01` sobre estrutura legada com dados de teste: hash de todas as tabelas legadas
  idêntico após `upgrade` e após `downgrade` (rollback remove só `erp_*`). Ela não foi aplicada em
  nenhum ambiente além de bancos de teste, portanto pode ser regenerada.
- Worker: página atômica com rollback, checkpoint após crash, dois workers concorrentes (um recurso
  nunca é sincronizado por dois ao mesmo tempo), renovação do lease em sincronização longa sem
  roubo, e **worker que perdeu o lease não confirma página, checkpoint, run nem job** (`LeaseLost`,
  verificação com `FOR UPDATE` dentro da mesma transação da gravação).
- Outbox: deduplicação sob concorrência, um único envio ao Mercos com seis despachantes, recuperação
  após queda no meio do envio vai para `unknown` e nunca reenvia.
- Criação concorrente das linhas de capacidade (corrida de unicidade corrigida).
- Estorno de recebimento, edição de itens do pedido, identidade individual, mensagem
  "Resultado em verificação" sem botão de reenvio.
- Legado: 58 operações idênticas ao `HEAD` em método, caminho, parâmetros, corpo e status sem credencial.

**b) Implementado sem validação real:** escritas externas (resposta real do Mercos), webhook
(assinatura e formato), `nextCursor` com paginação real, Redis do gate de cota, extensões do Adaptor
(títulos, pagamentos, comissões, promoções, produtos, estoque, cancelamento, faturamento).

**c) Pendente:** mapear `representada_id` (categorias, tabelas de preço, condições de pagamento),
`acrescimo`/`desconto` de tabelas de preço; investigação do escopo por representada; sandbox;
**emissão fiscal e emissão de boleto NÃO estão implementadas** (o ERP registra faturamento e
título locais, que não são nota fiscal nem boleto).

**d) Indisponível por permissão da conta:** `segments`, `carriers` e `commercial-policies` (403).

**Cota compartilhada:** o gate do Adaptor serializa toda chamada que passa por ele, e o BI passa
pelo Adaptor. `LocalGate` vale para uma instância; réplicas exigem `MERCOS_REDIS_URL` (`RedisGate`,
falha fechada). O Adaptor em produção ainda roda a versão sem o gate (alterações não commitadas).

## Painel operacional de pedidos (plano de implementação, executado localmente)

Implementado **localmente**, sem commit/deploy de produção e sem escrita real no Mercos:
menu Visão Geral, Pedidos, Nota Fiscal, Frete, Financeiro, Reembolsos e Configurações; migração aditiva
`20261009_01` (7 tabelas novas); cotação de frete **manual**, rascunho fiscal local, recebível por pedido,
solicitação/aprovação de reembolso e consumidor de cancelamento. Matriz detalhada em
`transicao-painel-operacional.md`. **Isto não declara o ERP completo**: frete automático, NF-e, Pix e
devolução efetiva de dinheiro dependem de integrações externas (ver `adaptor-requirements.md`).
