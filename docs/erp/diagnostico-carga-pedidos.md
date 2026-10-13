# Diagnóstico da carga de Pedidos (13/10/2026)

Fonte: leitura da produção pela interface do ERP (Integrações, Visão Geral, Pedidos), sem criar nada, mais
leitura do código. **Não houve acesso ao banco, aos logs da Render nem ao Adaptor.** Cada item abaixo diz se é
*observado*, *lido no código* ou *inferido*.

## 1. O que foi observado em produção

| Observação | Valor |
|---|---|
| Execuções de Pedidos (topo da lista) | 09/10 17:12 `interrupted` (19 págs., 380 recebidos, **0 gravados, 380 sem mudança**); 13/10 09:47 `interrupted` (50 págs., 1000 recebidos, 0 gravados, 1000 sem mudança); 13/10 11:15 `running` (19 págs., 380 recebidos, 0 gravados) |
| Mensagem das interrompidas | “Processo reiniciado durante a execução; retomada pelo checkpoint” |
| Modo de todas | `full` (as dos 12 recursos aparecem em dois lotes: 09:36–09:47 e 11:04–11:15) |
| “Alterações até” (checkpoint) de Pedidos | 25/01/2020 às 11:17 → 18/05/2020 às 11:25 → 29/05/2020 às ~11:27 |
| Pedidos locais | 5.080, o mais recente de 23/09/2020 08:37 |
| Sem itens / sem cliente / pendentes | 442 / 76 / 445 |
| Referências pendentes | 76 `customerId`, 1.186 `paymentConditionId`, 5.080 `carrierId` |
| Jobs | succeeded 12 · failed 7 · processing 1 · queued 12 |

## 2. Causa raiz da releitura (lido no código, confirmado por teste)

1. `sync_resource` ignorava o checkpoint quando `mode == "full"` (`cursor = None`): **toda carga completa
   recomeçava na primeira página**, mesmo depois de interrompida. A mensagem “retomada pelo checkpoint” só era
   verdadeira para o modo incremental.
2. `_persist_page` gravava o cursor de cada página em `checkpoint.cursor` **sem checar a ordem**: ao reler
   páginas antigas, o “Alterações até” **regredia** (por isso 2020‑01‑25 apareceu com pedidos até setembro/2020
   já gravados).
3. Resultado medido nas execuções vistas: `gravados = 0` e `sem mudança = recebidos` em todas — **100% do que
   essas execuções leram já estava gravado**. Elas nunca passaram da janela inicial (380–1000 registros) antes de
   serem interrompidas, então nunca avançaram.

Isto **não é a sobreposição intencional**: a sobreposição (`ERP_SYNC_OVERLAP_SECONDS`, 5 s) só recua o cursor
em cinco segundos ao retomar, relendo no máximo uma página. Reler 380–1000 registros do início é recomeço.

Quem disparou o modo `full`: o agendamento automático só enfileira `incremental`; o `full` vem de
“Sincronizar tudo” com “Carga completa” (ou o botão do recurso com a caixa marcada). Os dois lotes de 12 recursos
indicam duas cargas completas manuais (*inferido*: a tela não mostra quem pediu).

## 3. Correção aplicada (somente backend ERP; nada apagado)

- Carga completa interrompida (`interrupted`, `failed`, `partial`, `continuing`, `waiting_rate_limit` ou execução órfã
  `running`) **continua do maior cursor confirmado entre as execuções inacabadas desde a última carga completa concluída**, com a sobreposição de 5 s.
  Uma carga completa pedida depois de uma concluída continua começando do início (pedido explícito).
- O cursor do checkpoint **nunca regride**; o progresso de cada execução fica em `cursor_after`.
- O log registra “carga completa retomada do último cursor confirmado …” e `resumedFrom` aparece no resultado.
- Testes: `tests/erp/test_sync_resume.py` (6 casos; 4 deles falham se a correção for desligada).

**Efeito esperado na primeira retomada depois da publicação:** o checkpoint do recurso está regredido
(≈ 29/05/2020), mas a retomada usa o **maior cursor confirmado entre as execuções inacabadas** (cada execução só
grava `cursor_after` depois de confirmar a página). Se alguma execução anterior confirmou até setembro/2020, a
retomada começa lá; se nenhuma confirmou, começa do maior cursor registrado e relê uma vez o trecho
intermediário (já gravado). Não se pula para data recente, nada é apagado e nenhum checkpoint é reiniciado.
*Não foi possível ler os `cursor_after` de produção nesta rodada; a aba “Diagnóstico de pedidos” os mostra.*

## 4. Causa dos reinícios

| Reinício | Evidência | Status |
|---|---|---|
| 09/10 17:12 | O último push do backend foi às 16:59 (`de1d53b`); o deploy da Render reinicia o processo poucos minutos depois | **Provável**, por coincidência de horário |
| 13/10 09:47 | Nenhum commit/push do backend em 13/10 (o push do tema foi só do frontend) | **Não confirmada.** Candidatos sem prova: reinício da instância, alteração de variável de ambiente (reinicia o serviço), falta de memória. Precisa de **Render › Events e Logs do serviço do backend, 13/10 09:40–09:55 (BRT)** |

O diagnóstico novo (Integrações › *Diagnóstico de pedidos*) mostra reinícios registrados e sinaliza
`job_reclaimed_after_lease_expiry` quando o mesmo job é tomado mais de uma vez (lease vencido ou processo morto).

## 5. A carga longa impede outros recursos?

**Sim, por desenho (lido no código).** A fila é drenada pelo agendador do BI a cada 30 s com
`max_instances=1`; `drain_once` executa **um job por vez e só volta quando ele termina**. Enquanto o job de
pedidos roda:

- jobs de outros recursos ficam `queued` (os 12 enfileirados vistos são coerentes com isso);
- a **caixa de saída de escrita e a caixa de entrada de webhooks também esperam** (hoje inertes: escritas
  desligadas e webhook sem segredo, mas voltam a importar quando forem ligados);
- a hidratação de itens só é enfileirada quando um job de pedidos **termina ou é interrompido**; com a carga
  nunca terminando, ela não roda (explica os 442 sem itens). Ela também processa 50 pedidos por execução.

Não foi criado segundo sincronizador. **Recomendação (não implementada):** fatiar a carga completa por tempo ou
páginas e devolver o job à fila, para intercalar outros recursos, a hidratação e a caixa de saída.

Os 7 jobs `failed` são coerentes (*inferido*) com jobs “todos” que contêm recursos com 403: qualquer recurso
`forbidden` marca o job como `failed`, mesmo com os demais concluídos.

## 6. Pedidos sem itens, sem cliente e referências históricas (casos separados)

- **Sem itens (442):** a listagem v2 não traz itens; dependem da hidratação por ID (item 5) e de o detalhe por ID
  estar liberado na conta (não verificado). Terminar a carga de Pedidos **pode** destravar a fila de hidratação,
  mas não garante o detalhe.
- **Sem cliente (76):** o recurso Clientes está concluído (33.166, `success` às 11:04) e mesmo assim esses
  `customerId` não existem no espelho. **Terminar Pedidos não resolve isto.** Hipóteses sem prova: clientes
  excluídos, de outra representada ou fora do que a listagem devolve. A aba de diagnóstico lista os IDs
  distintos e as datas para conferência no Mercos.
- **Condição de pagamento (1.186):** só 16 condições existem no cadastro atual; pedidos de 2020 referenciam
  IDs que o cadastro corrente não lista. Cadastro histórico: **não se resolve terminando Pedidos**.
- **Transportadora (5.080):** o recurso responde 403 (preservado). O valor `0` é exibido como “Cadastro não
  disponível · ID técnico 0”: o contrato não confirma que 0 signifique “sem transportadora”, então não foi
  tratado como ausência.

## 7. Recursos com 403

`segments`, `carriers`, `commercial-policies` e `product-prices` continuam bloqueados (o bloqueio é preservado e
registrado em checkpoint, execução e matriz de capacidades). A mensagem passou a ser **sanitizada** (extrai o
texto legível do Adaptor, mascara chaves/tokens e limita o tamanho; o corpo bruto não sai) em todas as
superfícies de leitura, inclusive para registros já gravados. “Sincronizar tudo” **não repete** um recurso negado
nas últimas 24 h (só o botão do próprio recurso insiste). O texto da Mercos pede liberar o recurso para o
ApplicationToken ou concluir a homologação; **isso não prova a causa** nesta conta e não foi afirmado.

## 8. Atualização automática, fatias e conciliação (rodada seguinte)

**Carga em fatias.** Um job de sincronização cede a vez depois de `ERP_SYNC_SLICE_SECONDS` (240 s; 0 desliga):
a página em curso já está confirmada junto do checkpoint, a execução termina como `continuing` (aparece como
*em andamento*, nunca como erro) e o recurso volta à fila como job novo (`requested_by = worker-continuation`),
**atrás** dos jobs que já esperavam (a fila ordena por `run_after` e `id`). Resultado comprovado em teste: outro
recurso roda *entre* as fatias de Pedidos; cada fatia retoma do cursor confirmado pela anterior; nenhum job fica
duplicado ou preso. A hidratação de itens passa a ser enfileirada junto, em vez de esperar o fim da carga, e
só se repete enquanto houver progresso (detalhe por ID negado encerra o ciclo).

**Job reassumido.** Se o processo morre ou o lease vence, o job volta à fila (`attempts > 1`). Um job “todos”
reassumido **não refaz** os recursos que ele mesmo já concluiu (`skipped_done`); a execução órfã vira
`interrupted` e a carga completa continua do último cursor confirmado.

**Atualização sem operador.** Com `ERP_AUTO_SYNC=true`, o agendador do BI (a cada 30 s) enfileira, por recurso
vencido, uma sincronização *incremental* a partir do checkpoint, sem navegador aberto nem sessão de operador.
Teste `test_new_changes_are_imported_without_browser_or_operator`: um pedido alterado e um novo entram no
espelho, o checkpoint avança e o único solicitante é `scheduler`. A chamada ao Adaptor parte do cursor confirmado
(com 5 s de sobreposição), não de varredura completa.

**Garantias verificadas por teste** (SQLite e PostgreSQL): página e checkpoint na mesma transação (falha depois de
gravar linhas desfaz tudo); lease renovado durante a carga longa e perda de lease sem confirmar nada
(`test_pg_concurrency`); job abandonado recuperado; 429 reagenda sem perder o cursor e interrompe o job (a cota é
da conta; o gate que a serializa fica no Adaptor, não alterado); recurso 403 não bloqueia os demais nem é repetido.

**O que NÃO foi comprovado em produção:** não houve sessão de operador nem acesso ao banco/logs nesta rodada; o
avanço real da carga e a primeira retomada dependem da publicação das correções.

**Conciliação ERP × BI (somente leitura).** `GET /api/v1/erp/integration/reconciliation/{orders|customers|products}`
(Integrações › *Diagnóstico de pedidos*). Compara pelo ID Mercos, no mesmo período e no **mesmo instante** (o menor
entre os cursores confirmados dos dois lados, ou `asOf` informado). Pedidos: tipo (cancelado/orçamento/pedido),
totais bruto/líquido, desconto, nº e soma de itens, cliente, vendedor, vínculo de cliente. Clientes: nome,
ativo, documento (só o ID sai, nunca o valor). Produtos: código, nome, ativo, preço. Cada divergência traz causa,
impacto e correção e é **explicada** (versões diferentes no tempo, carga do ERP ainda não alcançou, BI atrás) ou
**inexplicada** (mesma versão, valores diferentes). Não força contagens iguais e não escreve em nenhum lado.

A conciliação lê as tabelas do BI por construções SQL somente leitura (`orders`, `order_items`, `customers`,
`products`, `sync_states`), **sem importar o código legado** (o contrato de isolamento do ERP continua passando) e
sem emitir nenhuma escrita.
