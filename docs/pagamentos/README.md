# Módulo: pagamentos

Prefixo: `/api/v1/payments` · Blueprint: `app/blueprints/pagamentos/routes.py` · Service:
`app/services/payment_service.py` (`MercadoPagoService`) · Model: `app/models/payment.py`

Integração com Mercado Pago. **Este blueprint é uma das duas portas de entrada** para
`MercadoPagoService` — a outra vive em `reservas` (painel admin). Ver "Quem mais chama o
service", abaixo, antes de assumir que uma mudança aqui cobre
todos os fluxos de pagamento do sistema.

---

## Rotas (só as deste blueprint)

| Rota | Método | Auth | Propósito |
|---|---|---|---|
| `/process` | POST | `@require_internal_token` (Bearer `QR_SERVICE_TOKEN`) | Recebe dados de pagamento do proxy WordPress/MP Bricks (PIX ou token de cartão) |
| `/generate-manual-pix` | POST | `@require_internal_token` | Gera PIX manualmente para uma reserva existente (fluxo de balcão via admin WordPress) |
| `/status/<payment_id>` | GET | **Pública** | Polling para o WordPress — mapeia `ReservaStatus` para `approved`/`rejected`/`pending` |
| `/config` | GET | **Pública** | `{enabled, public_key}` para inicializar o MP Bricks no frontend — nunca expõe o access token |
| `/webhook` | POST | **Pública** (chamada direta do Mercado Pago) | Notificação de pagamento — sempre reconsulta a API do MP, nunca confia no payload |

> A docstring do módulo em `routes.py` está desatualizada — cita só `/process` e `/webhook`.
> As 5 rotas acima são as reais.

### `POST /process`

Cria `PaymentTransaction(PENDING)` e chama `MercadoPagoService.processar_pagamento()`. `200` para
`approved`/`pending`/`in_process`, `422` para `rejected`/`cancelado`, `400` erro de validação,
`500` erro de config/inesperado. `_transaction_id` é removido da resposta antes de devolver ao
WordPress. Falhas viram `IntegrationLog(error_source="SISTUR_QR")`.

### `POST /generate-manual-pix`

Monta os dados do pagador a partir da própria `Reserva` (email fallback `<id>@sistur.local`,
nome dividido de `customer_name`, CPF de `customer_document`). `404` reserva não encontrada/
deletada, `400` sem `total_amount`, `422` MP não devolveu QR code.

### `GET /status/<payment_id>`

Busca `Reserva.payment_id == payment_id` (excluindo soft-deletadas).
`PAID`→`"approved"`, `CANCELED`→`"rejected"`, qualquer outro (inclusive `PARTIALLY_PAID`)→
`"pending"`. `404` se não achar reserva — o WordPress deve continuar fazendo polling nesse caso
(webhook ainda não chegou).

### `POST /webhook`

Aceita `{"type": "payment", "data": {"id": "..."}}`, também `action="payment.updated"` e query
params `?data.id=X`/`?id=X`. Sempre retorna `200`, exceto `400` (payload totalmente ausente) e
`500` (`MP_ACCESS_TOKEN` não configurado).

---

## Modelo — `PaymentTransaction`

Tabela `sistur_payment_transactions`. Migrations: `39dfe6dc3fa6` (criação — status sem
`REFUND_FAILED`), `69546d499593` (`refund_id`, `refunded_at`, + `Reserva.overbooking_extornada`),
`20a965b0ae9c` (adiciona `REFUND_FAILED` ao enum).

| Coluna | Tipo | Papel |
|---|---|---|
| `id` | String(36), PK | UUID — enviado ao MP como `external_reference` |
| `reserva_group_id` | String(36), indexed | Chave de agregação **estável entre versões** da reserva (ao contrário de `reserva_id`) |
| `reserva_id` | FK `sistur_reservas.id` (SET NULL) | Aponta pra versão atual da reserva |
| `status` | Enum `TransactionStatus` | `PENDING`/`APPROVED`/`REJECTED`/`CANCELLED`/`REFUNDED`/`REFUND_FAILED` |
| `amount` | Numeric(10,2) | Valor desta transação específica (pode ser parcial) |
| `payment_method`, `mp_payment_id` (indexed), `mp_status`, `mp_status_detail` | — | Rastreamento do lado MP |
| `qr_code`, `qr_code_base64`, `ticket_url` | — | Dados de exibição do PIX/checkout |
| `refund_id`, `refunded_at` | — | Preenchidos pelo auto-estorno da "Defesa Ativa" (ver abaixo) |
| `mp_response_snapshot` | JSON | Snapshot bruto da resposta do MP — auditoria/disputa |

`Reserva.overbooking_extornada` (Boolean) marca reservas canceladas/estornadas automaticamente
pela Defesa Ativa.

---

## `MercadoPagoService` — métodos

| Método | Papel |
|---|---|
| `_access_token()` | Cadeia de fallback: **vault** (`SecretService`) → `SystemSetting` legado (`ConfiguracaoService`) → env `MP_ACCESS_TOKEN` |
| `processar_pagamento(reserva_id, dados, partial_amount=None)` | Usa **Payment API** do MP. Valor vem de `reserva.total_amount` (anti-fraude), salvo `partial_amount` explícito. PIX expira em 12min. Cartão aprovado na hora: marca `APPROVED`, recalcula saldo, roda defesa de overbooking |
| `gerar_link_pagamento_cartao(reserva_id, payer_email=None)` | Usa **Preferences API** (não Payment API) — exclui PIX/boleto, até 12 parcelas, expira em 24h. Detecta sandbox pelo prefixo `TEST-` do token |
| `_recalcular_saldo_reserva(group_id)` *(privado)* | Soma transações `APPROVED` do `reserva_group_id`; define `PENDING`/`PARTIALLY_PAID`/`PAID` (tolerância 0.005). Em transição para `PAID`: dispara `ChatwootService.enviar_comprovante_pagamento` (em try/except — falha aqui nunca reverte o pagamento) |
| `_emitir_reembolso_automatico(...)` *(privado)* | Ver "Defesa Ativa contra overbooking" |
| `cancelar_pagamentos_concorrentes(reserva_id_aprovada)` | Após aprovação limpa, recheca reservas `PENDING` concorrentes pelo mesmo recurso — cancela só as que **de fato** estourariam o estoque agora |
| `processar_webhook(notification)` | Sempre reconsulta `sdk.payment().get(payment_id)` — nunca confia no status do payload. Roteia por `external_reference`: UUID → caminho novo (`_processar_webhook_novo`); não-UUID → caminho legado (`_processar_webhook_legado`, por `Reserva.payment_id` direto) |

Os dois caminhos de webhook coexistem por compatibilidade — pagamentos criados antes do
ledger multi-transação (`PaymentTransaction`) só têm `Reserva.payment_id`, não um UUID.

---

## Defesa Ativa contra overbooking

Depois de **qualquer** aprovação de pagamento (webhook PIX, webhook cartão, ou aprovação
imediata de cartão), o sistema rechecha estoque. Se houve overbooking:

1. `Reserva` já foi marcada `PAID` (dinheiro já está com o cliente confirmado)
2. Aviso #1 aos admins
3. Estorno automático e **idempotente** via API de Refund do MP (`X-Idempotency-Key:
   refund-{mp_payment_id}`) — nunca marca `REFUNDED` sem confirmação real do MP
4. `Reserva` PAID→CANCELED, `overbooking_extornada=True`
5. Aviso #2 aos admins, com texto diferente se o estorno falhou (`REFUND_FAILED`)
6. Se o estorno falhar, ainda assim cancela a reserva e grava `IntegrationLog` — aceita "sem
   vaga + estorno não confirmado" como o menor mal, exige intervenção manual do operador (decisão
   deliberada, documentada em comentário no código)

Quando a aprovação **não** gera overbooking, roda `cancelar_pagamentos_concorrentes` em vez
disso — cancela pendentes concorrentes que realmente ficariam sem estoque, não os que ainda cabem.

---

## Quem mais chama o service (não é só este blueprint)

| Rota | Blueprint | Auth | Chama |
|---|---|---|---|
| `POST /reservas/admin/reservas/<id>/gerar-pix` | `reservas` | `reservas.edit` | `processar_pagamento(..., partial_amount=...)` — PIX do "saldo devedor" |
| `POST /reservas/admin/reservas/<id>/gerar-link-cartao` | `reservas` | `reservas.edit` | `gerar_link_pagamento_cartao` |
| `POST /reservas/admin/reservas/<id>/verificar-pagamento` | `reservas` | `reservas.edit` | `processar_webhook` manual, uma reserva |
| `POST /reservas/admin/verificar-pagamentos-em-massa` | `reservas` | `reservas.edit` | `processar_webhook` em lote (janela máx. 7 dias) |

**Não há reuso de cobrança.** `/generate-manual-pix` deste blueprint e as rotas admin de
`reservas` sempre criam uma `PaymentTransaction` + cobrança/
preference novas a cada chamada.

---

## Permissões

Não existe grupo RBAC `"pagamentos"` — este blueprint é 100% máquina-a-máquina/público, sem
`@require_permission`. Autorização é via `@require_internal_token` (Bearer, reaproveita
`QR_SERVICE_TOKEN`) nas duas rotas internas, ou totalmente pública nas outras três. Quem chama
`MercadoPagoService` a partir de `reservas` usa `reservas.edit`.

---

## Testes

`tests/services/test_payment_service.py` — cobertura extensa do service (SDK do MP mockado):
resolução do access token, `_recalcular_saldo_reserva` (transições de status, tolerância,
múltiplas transações), `processar_pagamento` (reserva inexistente, PIX cria PENDING, cartão
aprova na hora, `partial_amount`, token de cartão ausente), webhook legado e novo (aprovar/
parcial/rejeitar/cancelar/idempotência), `_UUID_RE`, `gerar_link_pagamento_cartao` (sandbox,
PIX/boleto excluídos, sem `total_amount`, sem preference_id do MP).

**Gaps conhecidos:** nenhum teste de rota HTTP para o blueprint `pagamentos` em si (só o service
é testado); `_emitir_reembolso_automatico` e `cancelar_pagamentos_concorrentes` não têm teste
dedicado localizado neste arquivo.
