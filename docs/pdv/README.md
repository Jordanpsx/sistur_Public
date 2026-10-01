# Módulo PDV (Ponto de Venda) — Fases 1 (Backend & API) e 2 (SPA)

Prefixo: `/api/v1/pdv` · Blueprint: `app/blueprints/pdv/routes.py` · Service: `app/services/pdv_service.py` · Models: `app/models/pdv.py`

Plano completo do projeto (3 fases): `PDV_PROJECT_PLAN.md` na raiz do repositório.

---

## Arquitetura — invariantes não-negociáveis

1. **Nenhum caminho paralelo de estoque.** Toda venda avulsa resolve para
   `ReservaService.criar(origin=BALCAO)` e toda comanda para
   `ReservaService.atualizar()` — herdando o lock pessimista
   (`SELECT ... FOR UPDATE`) de `_verificar_estoque()`. O PDV **nunca** toca
   `ReservaResource`/`ReservaItemLink` diretamente.
2. **Idempotência por `client_sale_uuid`.** UUID v4 gerado no cliente no
   fechamento do carrinho. Reenvio (fila offline, retry de rede) retorna a
   venda original com HTTP 200 e `duplicate: true` — nunca duplica.
3. **Status inicial derivado de QUEM processou o pagamento, não do meio**
   (divergência deliberada do fluxo online PENDING→webhook). A distinção que
   importa não é "pix vs. cartão" — é se o dinheiro já está com o operador
   (o sistema só registra) ou se o **sistema** está processando o pagamento
   (aí sim existe uma espera real):
   - `CASH` / `EXTERNAL_CARD_TERMINAL` / `EXTERNAL_PIX` → Reserva nasce
     **PAID** imediatamente. Dinheiro, pix ou cartão já recebidos pelo
     operador **fora do sistema** (na mão, pix direto na conta, maquininha
     própria do estabelecimento) — não há razão para o maquinário de
     compensação do Active Defense construído para pagamento assíncrono.
   - `MP_PIX` / `MP_CARD` → Reserva nasce **PENDING**; o **sistema** gera a
     cobrança (QR/link) via `/api/v1/payments` e o webhook Mercado Pago
     confirma depois — herda Active Defense normalmente.
   - `COMANDA` (só em `anexar_comanda`) → sem pagamento no ato; itens entram
     na conta da reserva do hóspede, cobrados no check-out.

   **Bug corrigido (jul/2026):** até então não existia `EXTERNAL_PIX` — o
   único atalho de "Pix" na UI apontava para `MP_PIX`, fazendo o sistema
   gerar uma cobrança e aguardar confirmação para um pagamento que **já
   tinha sido recebido diretamente no balcão**. `EXTERNAL_PIX` fecha essa
   lacuna, espelhando o par que já existia para cartão
   (`EXTERNAL_CARD_TERMINAL` vs. `MP_CARD`).

**Atenção futura:** engenheiros mexendo em `ReservaStatusConfig` ou no Active
Defense (`payment_service.py`) não devem assumir que toda `Reserva` passa por
`PENDING` — vendas PDV em dinheiro nascem `PAID`.

---

## Permissões (RBAC)

Grupo `pdv` em `app/core/permission_catalog.py`:

| Permissão | Uso |
|---|---|
| `pdv.operate` | Operar o caixa: catálogo, cotação, vendas, comanda, carrinhos suspensos, lookup |
| `pdv.manage_shift` | Abrir/fechar turno, registrar sangria/suprimento |
| `pdv.view_reports` | Relatório de vendas do turno |

Todas as rotas exigem sessão ativa (`@login_required`).

---

## Modelos

| Tabela | Papel |
|---|---|
| `sistur_pdv_shifts` | Turno de caixa: fundo de troco, conferência de gaveta no fechamento |
| `sistur_pdv_sales` | Como a venda foi paga + chave de idempotência; 1:1 com a Reserva/versão criada |
| `sistur_pdv_held_carts` | Espelho servidor (best-effort) de carrinhos suspensos — o primário é localStorage |
| `sistur_pdv_offline_sync_log` | Trilha de cada tentativa de sync offline (ACCEPTED / REJECTED_*) |
| `sistur_pdv_movimentacoes_caixa` | Sangria/suprimento — movimentação manual de caixa fora do fluxo de vendas, com centro de custo separado do PDV de origem |

Migration: `9794af438eb5`. Movimentações de caixa: `81a6c5f3201b`.

---

## Endpoints

### `POST /api/v1/pdv/shifts/open` — permissão `pdv.manage_shift`

Abre turno para o operador em sessão.

Request: `{"source_id": 1, "opening_float_amount": 100.0, "opening_float_pix": 50.0}`
Response 201: `{"shift_id": 42, "status": "OPEN", "opening_float_amount": 100.0, "opening_float_pix": 50.0, "opened_at": "..."}`
Erros 400: venue inexistente · fundo negativo · turno já aberto para este operador/venue.

### `POST /api/v1/pdv/shifts/<id>/close` — permissão `pdv.manage_shift`

Fecha o turno conferindo duas fontes de troco:

- `closing_expected_amount`: fundo físico + vendas CASH, considerando se o troco saiu da gaveta ou via PIX.
- `closing_expected_pix`: fundo PIX de troco − trocos CASH devolvidos via PIX.

**O fundo PIX é estritamente decrescente, de propósito (jul/2026).** É a reserva fixa usada só
para dar troco em pix — nunca cresce durante o turno. Vendas `EXTERNAL_PIX` (pix recebido direto
do cliente) **não entram aqui**: esse dinheiro vai para a conta bancária/pix do estabelecimento,
fora do que esta rota concilia. Elas continuam visíveis em `GET /api/v1/pdv/shifts/<id>/report`
(agrupado por `payment_method`), sem exigir uma "contagem física" que não existe para pix
recebido — decisão explícita para não confundir "reserva de troco" com "conta corrente do
estabelecimento". `MP_PIX`/`MP_CARD` também não entram em nenhuma das duas — liquidam direto na
conta Mercado Pago.

Request: `{"closing_counted_amount": 480.0, "closing_counted_pix": 25.0, "discrepancy_reason": null}`
Response 200: `{"shift_id", "status", "closing_expected_amount", "closing_counted_amount", "difference", "closing_expected_pix", "closing_counted_pix", "difference_pix"}`
Response 409: quebra de caixa sem motivo (`{"erro", "detalhe": {"expected_cash", "counted_cash", "diff_cash", "expected_pix", "counted_pix", "diff_pix"}}`).
Se houver divergência com `discrepancy_reason` (mín. 10 chars), o turno fica `PENDING_APPROVAL` e precisa de aprovação.

### `POST /api/v1/pdv/shifts/<id>/approve-close` — permissão `pdv.manage_shift`

Aprova fechamento em `PENDING_APPROVAL` e conclui o turno (`CLOSED`). O operador
do turno não pode aprovar a própria quebra.

### `GET /api/v1/pdv/shifts/pending-approval` — permissão `pdv.view_reports`

Lista turnos com quebra de caixa aguardando aprovação, incluindo operador,
venue, diferenças física/PIX e motivo informado.

### `GET /api/v1/pdv/shifts/current` — permissão `pdv.operate`

Turno OPEN do operador em sessão (restauração da SPA pós-F5).
Response 200: `{"shift": {"shift_id", "source_id", "source_name", "opening_float_amount", "opened_at"}}` ou `{"shift": null}`.

### `GET /api/v1/pdv/sources` — permissão `pdv.operate`

Venues ativos para o seletor de abertura de turno.
Response 200: `{"sources": [{"id", "name"}]}`.

### `GET /api/v1/pdv/shifts/<id>/report` — permissão `pdv.view_reports`

Response 200: `{"shift": {...}, "totals_by_method": [{"method", "count", "total"}], "grand_total", "sales_count"}`
404 se turno não encontrado.

### `GET /api/v1/pdv/catalog?source_id=<id>` — permissão `pdv.operate`

Catálogo do venue (formato de `ReservaService.catalogo()`) com o campo extra
por item **`pdv_offline_allowed`**:

- `true` (🟢): item sem grupo de recursos físicos, ou grupo com ao menos um
  recurso ilimitado (`stock_quantity IS NULL`) — consumível (lenha, gastronomia).
- `false` (🔴): grupo só com recursos finitos (Day Use, churrasqueiras,
  camping) — a SPA bloqueia a venda offline destes itens.

Cache: resposta traz `ETag`; envie `If-None-Match` para receber `304` sem payload.

Response 200: `{"source": {..., "categories": [...]}, "generated_at": "..."}`

### `POST /api/v1/pdv/quote` — permissão `pdv.operate`

Cotação autoritativa server-side (`ReservaPricingService.calculate`). A SPA
usa como confirmação de baixa frequência — o preview por tecla é local.

Request: `{"source_id": 1, "items": [{"item_id": 7, "quantity": 2}], "manual_discount_percent": 0, "check_in": "2026-07-06T10:00:00"?, "check_out": "..."?, "category_id": 2?}`
(check_in/check_out default: agora — venda de balcão imediata)
Response 200: formato completo de `ReservaPricingService.calculate` (items_breakdown, subtotal, discount, service_fee, total).

### `POST /api/v1/pdv/manager-override/authorize` — permissão `pdv.operate`

Valida CPF/senha de um funcionário com `pdv.manage_shift` para autorizar um
override pontual do PDV.

Request:
```json
{
  "cpf": "529.982.247-25",
  "senha": "******",
  "reason": "Motivo operacional com pelo menos 10 caracteres"
}
```

Response 200:
```json
{
  "manager_override": {
    "manager_id": 12,
    "manager_name": "Gerente PDV",
    "reason": "Motivo operacional com pelo menos 10 caracteres"
  }
}
```

O retorno deve ser enviado no `POST /sale` quando houver override. O backend
revalida `manager_id` e registra `AuditLog` quando o override é usado.

### `POST /api/v1/pdv/sale` — permissão `pdv.operate`

Venda avulsa. **Idempotente por `client_sale_uuid`.**

Request:
```json
{
  "client_sale_uuid": "uuid-v4",
  "shift_id": 42,
  "source_name": "Cachoeira",
  "category_name": "Loja",
  "items": [{"item_id": 7, "quantity": 2, "resource_id": null}],
  "payment_method": "CASH",
  "customer_name": "Maria Souza",
  "customer_document": "52998224725",
  "amount_tendered": 100.0,
  "change_source": "CASH",
  "manual_discount_percent": 0,
  "force_overbooking": false,
  "manager_override": null,
  "offline_override_note": null,
  "synced_offline": false,
  "device_id": "terminal-01"
}
```

- `payment_method`: `CASH` | `EXTERNAL_CARD_TERMINAL` | `EXTERNAL_PIX` | `MP_PIX` | `MP_CARD` |
  `COMANDA` (só via `anexar_comanda`, não aqui). `native_enum=False` — nenhuma migration
  necessária para acrescentar valor novo. Ver a seção "Status inicial" acima para a distinção
  que importa entre os três primeiros e os dois do meio.
- `customer_name` omitido → walk-in "Balcão N" (mesmo padrão do form balcão).
- `amount_tendered` (CASH): servidor valida ≥ total e grava o troco.
- `change_source` (CASH): `CASH` quando o troco saiu da gaveta física;
  `PIX` quando o operador devolveu o troco pela conta PIX de troco. Esse campo
  alimenta a conferência de fechamento das duas fontes.
- `synced_offline: true` + `device_id`: marca a venda como vinda da fila
  offline e registra a tentativa em `sistur_pdv_offline_sync_log`.
- `force_overbooking: true` exige `manager_override` autorizado por
  `POST /manager-override/authorize`; o uso gera `AuditLog` separado com
  gerente, motivo, `client_sale_uuid` e flag `synced_offline`. Esse caminho
  **ignora** o lock de estoque (uso presencial, online, com senha validada).
- `offline_override_note: {"cpf", "reason"}` — declaração **não verificada
  por senha** coletada offline na SPA quando um item de estoque limitado (🔴)
  é adicionado ao carrinho sem rede. Ao contrário de `manager_override`, ela
  **não** seta `force_overbooking` nem ignora `_verificar_estoque()`: o lock
  de estoque continua com a palavra final no sync, podendo rejeitar com `409`
  mesmo com a nota presente. A nota fica gravada em
  `Reserva.metadata_info.pdv_offline_override_note`, no
  `payload_snapshot` do `PdvOfflineSyncLog` e em `AuditLog` dedicado
  (`action=update`, `new_state.action="offline_override_note"`), para que a
  reconciliação gerencial saiba quem autorizou a tentativa mesmo quando ela é
  rejeitada.

Responses:
- **201**: `{"sale": {...}, "reserva_id": 123, "pricing": {...}, "duplicate": false}`
- **200**: duplicata idempotente (`"duplicate": true`, `pricing: null`)
- **400**: validação (turno fechado, item inexistente, troco insuficiente, método inválido)
- **409**: `OverbookingError` — `{"erro", "conflito": {"resource_name", "conflitos"}}`.
  A SPA mostra banner inline na linha do item; nunca reenvia o formulário todo.

### `POST /api/v1/pdv/sale/comanda-append` — permissão `pdv.operate`

Anexa itens à comanda de uma reserva ativa. Cria **nova versão** da Reserva
(`ReservaService.atualizar`); se estava PAID e o total mudou, o próprio
atualizar rebaixa para PENDING (hóspede deve a diferença no check-out).

Request: `{"client_sale_uuid", "shift_id", "reserva_id", "items": [{"item_id", "quantity"}]}`
Response 201: `{"sale": {...}, "reserva_id": <NOVA versão>, "duplicate": false}` — o cliente deve atualizar o id da reserva.
200 duplicata · 400 validação · 409 conflito de estoque.

`PdvSale.amount_total` da comanda = total novo − total anterior (valor anexado).

### `GET /api/v1/pdv/customer-lookup?q=<termo>&source_id=<id>?` — permissão `pdv.operate`

Busca reservas ativas (não arquivadas/canceladas/deletadas) por CPF/CNPJ
(dígitos) ou fragmento de nome (mín. 3 chars). Máx. 20 resultados.

Response 200: `{"results": [{"reserva_id", "group_id", "customer_name", "customer_document", "status", "check_in_date", "total_amount"}]}`

### Carrinhos suspensos — permissão `pdv.operate`

Espelho servidor best-effort (o hold primário é localStorage na SPA):

- `GET /held-carts?shift_id=` → `{"held_carts": [{"id", "label", "cart_json", "held_at"}]}` (pendentes, mais antigos primeiro)
- `POST /held-carts` com `{"shift_id", "cart_json", "label"?}` → 201 `{"held_cart_id"}`
- `POST /held-carts/<id>/resolve` → 200 (retomado ou descartado) · 404

### Movimentações de caixa (sangria/suprimento) — permissão `pdv.manage_shift`

Sangria/suprimento **não têm fila offline** — ao contrário da venda, é um
evento raro e presencial (gerente ao lado do operador), sem caso de uso
"registrei sem rede e sincronizo depois". Serviço: `MovimentacaoCaixaService`
(`app/services/movimentacao_caixa_service.py`). Separação física × P&L: ver
docstring de `TipoMovimentacaoCaixa` em `app/models/pdv.py`.

- `POST /movimentacao-caixa/sangria` — body `{"caixa_turno_id", "tipo_movimentacao", "valor", "descricao", "conta_destino_id"?, "centro_de_custo_id"?, "manager_override"?}`. `tipo_movimentacao`: `TRANSFERENCIA_INTERNA` (exige `conta_destino_id`) | `RECOLHIMENTO_COFRE` | `PAGAMENTO_DESPESA` (exige `centro_de_custo_id`, PK de `Area`). `funcionario_id` vem da sessão, nunca do body. → 201 `{"movimentacao"}` · 400.
- `POST /movimentacao-caixa/suprimento` — body `{"caixa_turno_id", "valor", "descricao", "manager_override"?}`. → 201 `{"movimentacao"}` · 400.
- `GET /movimentacao-caixa?shift_id=` → `{"movimentacoes": [...]}`, mais recente primeiro. Funciona também para turno já fechado (conferência histórica).
- `GET /areas` → `{"areas": [{"id", "name"}]}` — áreas ativas, para o seletor de centro de custo em `PAGAMENTO_DESPESA`.

**Autorização acima de R$ 500,00** (`MovimentacaoCaixaService.LIMITE_EXIGE_AUTORIZACAO`): `autorizado_por_id` deixa de ser recomendado e passa a ser obrigatório. A SPA resolve isso reaproveitando o fluxo já existente de `POST /manager-override/authorize` (CPF+senha do gestor, validado contra `pdv.manage_shift`) — o resultado (`{manager_id, reason}`) viaja como `manager_override` no body da sangria/suprimento, exatamente como já acontece em `force_overbooking` na venda. Não há um novo mecanismo de autorização, é o mesmo.

`conta_destino_id` é uma referência solta de propósito (sem FK no banco): pode ser o `PdvShift.id` de outra gaveta ou uma conta virtual externa sem linha nesta base. A v1 da UI pede só o número do turno de destino — não valida se ele existe.

---

## Fase 2 — SPA do Caixa (`GET /reservas/pdv`)

**Rota:** `GET /reservas/pdv` (blueprint `reservas`, endpoint `reservas.pdv_caixa`) — `@login_required` + `pdv.operate`.
**Template:** `app/templates/reservas/pdv.html` (autocontido: HTML + CSS + JS inline, padrão do módulo).
**Sidebar:** item "PDV (Caixa)" na seção *PDV / Operacional*, entre "Nova Reserva (Balcão)" e "Predefinições" (`reservas_base.html`).

### Arquitetura client-side

| Dado | Armazenamento | Chave |
|---|---|---|
| Carrinho ativo (anti-F5) | `localStorage`, debounce 150ms, TTL 4h | `pdv_cart_v1` |
| Carrinhos suspensos | `localStorage` + espelho best-effort `POST /held-carts` | `pdv_held_v1` |
| Catálogo por venue | `IndexedDB` (db `pdv`, store `catalog`) com ETag → 304 | key = source_id |
| Fila offline de vendas | `IndexedDB` (db `pdv`, store `sync_queue`) com migração automática do legado `localStorage` | key = `client_sale_uuid` |
| App shell do PDV | Cache Storage via service worker | `pdv-shell-v2` → `/reservas/pdv` |
| Dependências de runtime do shell (Tailwind CDN, fontes) | Cache Storage via service worker, cache-first com revalidação em background | `pdv-assets-v1` |
| ID do terminal | `localStorage` (gerado uma vez) | `pdv_device_id` |

Valores monetários em **centavos (int)** em todo o client-side.

### Fluxos implementados

- **Boot:** `GET /shifts/current` → sem turno = tela de abertura (`GET /sources`); com turno = POS direto. O app shell `/reservas/pdv` **e suas dependências de runtime** (`cdn.tailwindcss.com`, fontes do Google) são cacheados pelo service worker para F5 offline — cachear só o HTML não bastava, pois o Tailwind Play CDN gera as classes utilitárias via JS no navegador; sem ele, F5 offline carregava HTML sem estilo nenhum. Catálogo é lido do IndexedDB e revalidado por ETag quando há rede.
- **Busca 0ms:** índice em memória (nome+slug+id normalizados, sem acentos); filtro por keystroke sem rede.
- **Contrato de foco:** a busca recupera foco após toda ação; digitar fora de um campo redireciona o foco para ela.
- **Atalhos:** `Enter` adiciona · `↑↓` navega · `+`/`−` qtd da última linha · `Del` remove · `F4` segurar · `F6` retomar · `F7` comanda · `F8` pagamento · `1-6` método · `Esc` volta.
- **Pagamento:** total local instantâneo + cotação autoritativa via `/quote` (AbortController + sequence guard contra resposta fora de ordem). Dinheiro: botões de cédulas ≥ total + troco calculado a cada tecla.
- **Métodos rápidos (rodapé do carrinho):** Dinheiro, Cartão (maquininha própria), Pix — as três
  opções "já recebido pelo operador", que liquidam na hora e funcionam offline. `MP_PIX`/`MP_CARD`
  não têm atalho rápido de propósito — só aparecem no painel completo de pagamento, porque exigem
  rede e uma decisão consciente de "vou gerar uma cobrança agora", não um clique automático.
- **Painel completo de pagamento (6 opções):** os três atalhos acima, mais **Pix — gerar cobrança
  (Mercado Pago)** e **Cartão — gerar link (Mercado Pago)** (ambos aguardam confirmação via
  webhook) e **Comanda do hóspede**. Pix e Cartão aparecem duas vezes cada, de propósito — recebido
  direto no balcão e gerado pelo sistema são fluxos diferentes, não o mesmo método com rótulo
  diferente.
- **Confirmação otimista:** carrinho limpa e foco volta à busca imediatamente; `POST /sale` roda em background. Erro HTTP (400/409) restaura o carrinho e mostra banner inline (409 com nome do recurso em conflito). Falha de **rede** com itens 🟢 + CASH/maquininha → fila offline IndexedDB (`synced_offline: true` + `device_id`), drenada em FIFO pelo service worker/Background Sync quando disponível e pelo fallback da aba (`online` + 30s).
- **Fila offline operacional:** barra inferior exibe vendas pendentes, última tentativa/erro e botão "Sincronizar agora"; o modal de fechamento do turno repete o alerta e oferece retry antes da conferência de gaveta.
- **Regras semáforo na UI:** offline → itens 🔴 (`pdv_offline_allowed: false`) abrem um modal de autorização gerencial (CPF + motivo ≥10 chars) em vez de bloquear silenciosamente; a autorização libera o item **apenas neste carrinho** e viaja como `offline_override_note` na venda enfileirada — não seta `force_overbooking`, o lock de estoque real ainda decide no sync (pode rejeitar com `REJECTED_STOCK`). Métodos MP e Comanda continuam desabilitados offline (exigem rede estruturalmente); barra de status fica âmbar.
- **Comanda (F7):** lookup por CPF/nome → vincula hóspede ao carrinho → método 5 no pagamento faz `POST /sale/comanda-append`.
- **Movimentar caixa:** botão na barra de status abre modal de sangria (recolhimento/despesa/transferência) ou suprimento — tipo, valor, descrição, centro de custo (para despesa) ou turno de destino (para transferência); valor ≥ R$ 500 abre inline CPF+senha+motivo do gestor (mesmo mecanismo de `manager-override/authorize` já usado em `force_overbooking`). Lista as movimentações do turno dentro do próprio modal.
- **Fechar turno:** modal com valor contado → exibe esperado/contado/diferença; avisa se há vendas na fila offline antes de fechar. O esperado (`PdvService._esperados_fechamento`) já soma fundo + vendas em dinheiro **e aplica sangrias/suprimentos do turno** — uma sangria feita durante o turno não gera falsa "quebra de caixa" ao fechar, desde que o operador conte a gaveta corretamente (fundo + vendas − sangrias + suprimentos).

### Teste de concorrência multi-canal (harness)

`tests/harness/run_concurrency_pdv.py` prova que o lock `FOR UPDATE` de
`_verificar_estoque()` impede overbooking sob disputa simultânea real dos
três canais (PDV/BALCAO via `PdvService`, WEB/WordPress e BOT via
`ReservaService.criar`). Requer Postgres (SQLite não serializa `FOR UPDATE`);
cria e derruba um banco descartável `sistur_concurrency` no mesmo servidor
do staging — nunca toca no `sistur_db`.

```bash
docker exec -w /app -e PYTHONPATH=/app <container-do-sistur> \
    python tests/harness/run_concurrency_pdv.py
```

Cenários validados: stock=1 × 8 threads → 1 sucesso/7 rejeições;
stock=3 × 12 threads → 3 sucessos/9 rejeições; consumo real sempre == stock.

**Invariante de configuração:** a garantia depende de
`PENDING.blocks_resource=True` (default). Se um admin desmarcar isso em
Status de Reservas, abre-se uma janela de corrida entre o commit PENDING do
`criar()` e o commit do `PAID` no PDV — não desative sem antes mover a
atribuição de PAID para dentro da transação do `criar()`.

### Pendências para a Fase 3

Validar em hardware/terminal real (não só via cliente de teste Flask) que F5 offline, fechar/reabrir navegador, reiniciar sistema e sync sem aba aberta funcionam com o shell cache corrigido; escrever E2E de navegador (DevTools offline) para a fila e para o caso de estoque esgotado descoberto no sync; avaliar AES-GCM apenas como hardening opcional depois que a fila recuperável estiver provada em campo.

---

## Fila offline — contrato Fase 3 (referência)

A SPA/Service Worker drenam a fila IndexedDB em FIFO, um POST por vez, sempre
com `synced_offline: true` + `device_id`. Sucesso, duplicata e rejeições
definitivas (`400`/`409`) removem a entrada; erros temporários como sessão,
permissão ou `5xx` preservam a fila para nova tentativa/reconciliação.
Resultados possíveis já suportados pelo backend nesta fase:

| Outcome (`sistur_pdv_offline_sync_log`) | Significado |
|---|---|
| `ACCEPTED` | Venda criada normalmente |
| `REJECTED_DUPLICATE` | Retry de venda já registrada — cliente remove da fila |
| `REJECTED_STOCK` | Conflito de estoque descoberto no sync — vira alerta gerencial (dinheiro já coletado) |
| `REJECTED_VALIDATION` | Payload inválido (ex.: item deletado desde o cache) |

### Reconciliação gerencial

- `GET /api/v1/pdv/sync-logs?outcome=&limit=` — permissão `pdv.view_reports`.
  Lista as tentativas recentes em `sistur_pdv_offline_sync_log`, com limite
  máximo de 200 linhas e resumo operacional do payload (`shift_id`, venue,
  método de pagamento, quantidade de itens), sem expor o JSON bruto da venda.
- `GET /reservas/pdv/reconciliacao` — tela HTML densa para triagem de syncs
  aceitos, duplicados e rejeitados. Ela é apenas uma inbox de reconciliação;
  a resolução operacional/contábil continua fora desta primeira entrega.

---

## Testes

`tests/services/test_pdv_service.py` — happy paths, AuditLog,
idempotência, PAID imediato p/ CASH, troco, PENDING p/ MP_PIX, walk-in
"Balcão N", OverbookingError em recurso finito, trilha de sync, flags
offline do catálogo, conferência de gaveta e listagem de reconciliação.

`tests/services/test_movimentacao_caixa_service.py` — sangria/suprimento:
happy paths por tipo, AuditLog, validações (`centro_de_custo_id`,
`conta_destino_id`, turno fechado, valor inválido), `autorizado_por_id` vs.
`manager_override` acima do limite, e a integração com
`PdvService.fechar_turno`/`_esperados_fechamento` (sangria reduz o esperado,
suprimento aumenta, turno fecha limpo quando o contado reflete a
movimentação e acusa quebra quando não reflete).

```bash
docker exec sistur-teste-sistur-flask-1 python -m pytest tests/services/test_pdv_service.py tests/services/test_movimentacao_caixa_service.py -q
```
