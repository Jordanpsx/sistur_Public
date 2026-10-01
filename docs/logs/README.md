# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

# Módulo: Logs de Integração

> Captura e visualização de erros originados em frontends externos (WordPress, Mercado Pago Bricks, integrações internas) e do próprio Sistur durante operações de pagamento e geração de QR Code PIX.

---

## Visão Geral

O módulo de Logs de Integração existe para dar visibilidade sobre falhas que ocorrem **fora do contexto de sessão do SISTUR** — principalmente durante o fluxo de pagamento do cliente final no WordPress.

Diferente do `AuditLog` (que cobre ações de usuários autenticados), este módulo captura:

- Erros do Mercado Pago Bricks retornados ao browser do cliente
- Falhas no código JavaScript interno do WordPress
- Falhas de sincronização entre WordPress e SISTUR
- **Erros internos do Sistur** ao processar pagamentos ou gerar QR Code PIX (source `SISTUR_QR`)

### Por que `reserva_id` não tem FK?

O campo `reserva_id` é armazenado como inteiro simples, sem restrição de chave estrangeira. Isso é **intencional**:

> Um dos principais cenários de erro é exatamente quando o vínculo reserva → SISTUR está quebrado. Uma FK impediria o log do erro que mais importa registrar.

---

## Modelo de Dados

**`IntegrationLog`** — `app/models/integration_log.py`, tabela `sistur_integration_logs`

```sql
CREATE TABLE sistur_integration_logs (
    id            INTEGER       PRIMARY KEY AUTO_INCREMENT,
    reserva_id    INTEGER       NULL,          -- sem FK intencional
    customer_name VARCHAR(255)  NULL,          -- nome do cliente da reserva (diagnóstico sem JOIN)
    error_source  VARCHAR(50)   NOT NULL,      -- MP_BRICKS | WP_INTERNAL | SISTUR_SYNC | SISTUR_QR
    error_code    VARCHAR(100)  NULL,
    error_message TEXT          NULL,
    raw_payload   JSON          NULL,          -- objeto bruto do erro (apenas dict)
    user_agent    VARCHAR(255)  NULL,
    created_at    DATETIME      NOT NULL,
    INDEX ix_sistur_integration_logs_reserva_id (reserva_id),
    INDEX ix_sistur_integration_logs_error_source (error_source),
    INDEX ix_sistur_integration_logs_created_at (created_at)
);
```

### Valores aceitos em `error_source`

| Valor | Origem |
|---|---|
| `MP_BRICKS` | Erro retornado diretamente pelo SDK do Mercado Pago Bricks no browser |
| `WP_INTERNAL` | Erro no código JavaScript interno do plugin WordPress |
| `SISTUR_SYNC` | Falha na sincronização do WordPress com o endpoint do SISTUR |
| `SISTUR_QR` | Erro interno do Sistur ao processar pagamento ou gerar QR Code PIX (registrado automaticamente pelo backend) |

---

## Endpoints

### `POST /api/logs/payment-error`

Persiste um erro de integração de pagamento enviado pelo frontend WordPress.

**Auth required:** Nenhuma — endpoint público.

> **Por que público?** O endpoint é chamado diretamente pelo browser do cliente durante o checkout. Não há como incluir um Bearer token no JavaScript sem expô-lo. O CORS restringe origens aceitas a `cachoeiradogirassol.com.br`. A rota é **write-only** e não expõe nenhum dado existente.

**Blueprint:** `app/blueprints/logs/routes.py`, registrado em `/api/logs`

**Request body (JSON):**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `error_source` | string | Sim | Um de: `MP_BRICKS`, `WP_INTERNAL`, `SISTUR_SYNC`, `SISTUR_QR` |
| `error_message` | string | Sim | Descrição legível do erro (max 2000 chars) |
| `reservation_id` | int | Não | ID da reserva no SISTUR, se disponível |
| `customer_name` | string | Não | Nome do cliente da reserva para diagnóstico (max 255 chars) |
| `error_code` | string | Não | Código de erro retornado pelo MP ou sistema (max 100 chars) |
| `raw_data` | object | Não | Objeto bruto do erro para diagnóstico. Ignorado se não for dict |

**Exemplo de request:**

```json
{
  "error_source": "MP_BRICKS",
  "error_message": "Payment was rejected: cc_rejected_insufficient_amount",
  "reservation_id": 42,
  "error_code": "cc_rejected_insufficient_amount",
  "raw_data": {
    "status": "rejected",
    "status_detail": "cc_rejected_insufficient_amount",
    "payment_method_id": "visa"
  }
}
```

**Resposta de sucesso (HTTP 201):**

```json
{ "logged": true, "id": 17 }
```

**Erros:**

| Status | Situação |
|---|---|
| 400 | Payload JSON ausente ou inválido |
| 400 | `error_source` não está entre os valores aceitos |
| 400 | `error_message` vazio ou ausente |

---

### `GET /admin/logs`

Interface administrativa para visualizar os logs de integração por período.

**Auth required:** Sessão ativa + (`Role.is_super_admin = True` ou permissão `audit.view_all`)

**Blueprint:** `app/blueprints/admin/routes.py`, registrado em `/admin`

**Template:** `app/templates/admin/integration_logs.html`

**Query parameters:**

| Param | Tipo | Default | Descrição |
|---|---|---|---|
| `data` | string (ISO date) | data de hoje | Data de início do período (`YYYY-MM-DD`) |
| `data_fim` | string (ISO date) | igual a `data` | Data de fim do período (`YYYY-MM-DD`) |
| `source` | string | `` (todos) | Filtrar por `error_source` |
| `page` | int | 1 | Paginação (25 por página) |

**Layout:**

- **Sidebar esquerdo — Calendário interativo:**
  - Exibe um calendário mensal navegável com botões `‹` / `›` para mudar de mês.
  - Dias com erros registrados exibem um **ponto vermelho** na célula.
  - **Seleção de intervalo:** primeiro clique define a data de início, segundo clique define a data de fim. O período fica destacado. Clique em "Aplicar" submete o formulário.
  - O calendário carrega **todos** os dias com erros sem limite de histórico (`dias_com_erros_map: {ISO-date: count}`).
- **Área principal:** Tabela paginada dos logs no período selecionado com colunas: Hora, Fonte (badge), Código, Mensagem (truncada), Reserva ID.
- **Sem IP, de propósito:** quem envia os erros é sempre um servidor (WordPress, site, Mercado Pago), então o IP gravado era o do servidor e não servia para diagnóstico. Guardar o IP do visitante, por outro lado, acabaria com a visita anônima ao site. A coluna foi removida.
- **Modal de detalhe:** Botão "Ver" abre modal com `error_message` completo + `raw_payload` formatado como JSON + `user_agent`.
- **Filtro de fonte:** Dropdown preserva o período selecionado via campos hidden `data` e `data_fim`.

**Cores dos badges por fonte:**

| Fonte | Cor |
|---|---|
| `MP_BRICKS` | Azul |
| `WP_INTERNAL` | Laranja |
| `SISTUR_SYNC` | Roxo |
| `SISTUR_QR` | Vermelho |

---

## Serviço de registro — `IntegrationLogService`

Todo log passa por `app/services/integration_log_service.py`. Ele trunca campos,
classifica o erro, aceita `reserva_id` como int/str e **nunca propaga exceção** —
o chamador está sempre num caminho de erro e não pode ser interrompido por uma
falha secundária de persistência.

```python
from app.services.integration_log_service import IntegrationLogService

IntegrationLogService.registrar(
    error_source="SISTUR_SYNC",
    error_code="WEBHOOK_ORPHAN_TRANSACTION",
    error_message="…",
    reserva_id=123,
    customer_name="Maria",
    raw_payload={"mp_payment_id": "456"},
)
```

Origem fora do conjunto válido não descarta o log: vira `SISTUR_SYNC` com o valor
original preservado na mensagem.

---

## Auto-logging pelo Sistur (SISTUR_QR)

Desde a atualização de 2026-03-30, o próprio backend do Sistur persiste automaticamente `IntegrationLog` ao detectar erros nos endpoints de pagamento.

### Quando é gerado um log `SISTUR_QR`?

| Código do erro | Descrição | Endpoint |
|---|---|---|
| `PAYMENT_VALIDATION_ERROR` | Reserva inválida ou dados inconsistentes | `POST /api/v1/payments/process` |
| `PAYMENT_CONFIG_ERROR` | `MP_ACCESS_TOKEN` não configurado | `POST /api/v1/payments/process` |
| `PAYMENT_UNEXPECTED_ERROR` | Exceção não esperada no processamento | `POST /api/v1/payments/process` |
| `PAYMENT_ON_EXPIRED_RESERVA` | Pagamento iniciado com soft lock já vencido (não bloqueia — alerta de overbooking) | `POST /api/v1/payments/process` |
| `PIX_VALIDATION_ERROR` | Reserva inválida na geração de PIX manual | `POST /api/v1/payments/generate-manual-pix` |
| `PIX_CONFIG_ERROR` | `MP_ACCESS_TOKEN` não configurado | `POST /api/v1/payments/generate-manual-pix` |
| `PIX_UNEXPECTED_ERROR` | Exceção não esperada na geração de PIX | `POST /api/v1/payments/generate-manual-pix` |
| `PIX_NO_QR_CODE` | MP processou mas não retornou QR Code | `POST /api/v1/payments/generate-manual-pix` |

### Falhas de sincronização (`SISTUR_SYNC`)

Cobrem os dois pontos cegos mais graves: pagamento confirmado que não vira reserva,
e reserva que nem chega a existir para o cliente poder pagar.

| Código do erro | Descrição | Onde |
|---|---|---|
| `WEBHOOK_CONFIG_ERROR` | Webhook do MP não processado por erro de configuração — pagamentos aprovados não atualizam reservas | `POST /api/v1/payments/webhook` |
| `WEBHOOK_UNEXPECTED_ERROR` | Exceção inesperada no webhook — a reserva pode ficar sem confirmação | `POST /api/v1/payments/webhook` |
| `WEBHOOK_ORPHAN_TRANSACTION` | Pagamento **aprovado** cujo `external_reference` não existe no Sistur — conciliar manualmente | `MercadoPagoService._processar_webhook_novo` |
| `WEBHOOK_ORPHAN_PAYMENT` | Pagamento **aprovado** sem reserva com aquele `payment_id` (caminho legado) | `MercadoPagoService._processar_webhook_legado` |
| `INGEST_EMPTY_BODY` | Requisição de criação de reserva sem body | `POST /api/internal/reservas/ingest` |
| `INGEST_VALIDATION_ERROR` | Campo obrigatório ausente ou data em formato inválido | idem |
| `INGEST_EMPTY_ITEMS` | Payload sem itens — normalmente slug de catálogo não resolvido no WordPress | idem |
| `INGEST_REJECTED` | Recusa de regra de negócio: divergência de preço (anti-fraude), item inexistente, estoque esgotado | idem |
| `INGEST_CLIENT_BANNED` | Cliente na blacklist — vê mensagem genérica e reclama que não consegue reservar | idem |
| `INGEST_UNEXPECTED_ERROR` | Exceção inesperada ao criar a reserva (antes, devolvia HTML de erro sem log) | idem |

### Configuração de pagamento ausente (`MP_BRICKS`)

| Código do erro | Descrição | Onde |
|---|---|---|
| `MP_PUBLIC_KEY_MISSING` | Public Key do MP não configurada — o checkout não carrega para **nenhum** cliente. Throttle de 1h para não inundar o painel | `GET /api/v1/payments/config` |

### Dados registrados em cada log `SISTUR_QR`

- **`reserva_id`**: ID da reserva afetada
- **`customer_name`**: Nome do cliente da reserva (extraído automaticamente do banco)
- **`error_message`**: Descrição human-readable incluindo ID e nome do cliente
- **`error_code`**: Código do tipo de erro (tabela acima)
- **`raw_payload`** (quando aplicável): status MP e detalhes do erro bruto

---

## Integração WordPress

### ⚠️ O browser do cliente NÃO chama este endpoint diretamente

A `sistur_api_base_url` configurada no WordPress é um nome de DNS interno do Docker
(`http://sistur-flask:5000`). O browser do cliente final não resolve esse host — e,
numa página HTTPS, uma chamada `http://` é bloqueada como mixed content antes de sair.
Enquanto o JS apontou direto para lá, **todo log de origem no browser se perdeu em
silêncio** (por isso a tabela nunca teve nenhuma linha `MP_BRICKS` vinda de cliente real).

O caminho correto é o proxy server-side do plugin:

```
browser → POST admin-ajax.php?action=rcg_log_client_error  (WordPress)
        → POST /api/logs/payment-error                     (Sistur, rede interna)
```

O proxy é público (`nopriv` — o cliente não está logado), com limite de
`RCG_CLIENT_LOG_MAX_POR_MINUTO` (20) requisições por IP por minuto e redação
server-side de campos de cartão (`token`, `securityCode`, `cvv`…), que nunca são
persistidos.

**Uso no JS do plugin** (`assets/js/shared/rcg-error-logger.js`):

```javascript
window.RCG_ErrorLogger.log({
  source: 'WP_INTERNAL',            // ou MP_BRICKS
  code: 'DAYUSE_PIX_AJAX_ERROR',
  message: 'Falha ao gerar o QR Code PIX (Day Use): timeout',
  reservationId: 123,
  customerName: 'Maria',
  raw: RCG_ErrorLogger.fromXhr(xhr, status),
});
```

### Códigos emitidos pelo plugin

| Código | Quando |
|---|---|
| `INGEST_NONCE_INVALID` | Formulário recusado por nonce vencido (página em cache) |
| `INGEST_EMPTY_ITEMS` | Nenhum item resolvido no catálogo Sistur |
| `INGEST_PRICING_FAILED` | Falha na simulação de preços antes de criar a reserva |
| `INGEST_SISTUR_<status>` | Sistur recusou a criação com HTTP 4xx/5xx |
| `INGEST_NETWORK_ERROR` | Sistur inalcançável a partir do WordPress |
| `INGEST_CLIENT_BANNED` | Cliente bloqueado pela blacklist |
| `PIX_INVALID_INPUT` | Valor ou e-mail inválidos ao gerar PIX |
| `PIX_MISSING_RESERVA_ID` | Formulário não enviou o ID da pré-reserva |
| `PIX_MISSING_TOKEN` / `CARD_MISSING_TOKEN` | Token da API do Sistur não configurado no WP |
| `PIX_NETWORK_ERROR` / `PIX_SISTUR_<status>` | Falha de rede ou recusa do Sistur ao gerar PIX |
| `CARD_NONCE_INVALID` | Pagamento com cartão recusado por nonce inválido/expirado |
| `CARD_MISSING_RESERVA_ID` | Pagamento com cartão sem ID de reserva |
| `CARD_INVALID_INPUT` | Corpo da requisição de cartão vazio/ilegível |
| `CARD_NETWORK_ERROR` / `CARD_SISTUR_<status>` | Falha de rede ou recusa do Sistur no cartão |
| `CARD_PHP_EXCEPTION` | Exceção PHP no handler de cartão |
| `MP_SDK_LOAD_FAILED` | SDK do Mercado Pago não carregou no browser (adblock, rede, CDN) |
| `BRICK_CREATE_FAILED` | Formulário de pagamento não renderizou |
| `DAYUSE_INGEST_AJAX_ERROR` / `CAMPING_INGEST_AJAX_ERROR` | Falha de rede ao criar a pré-reserva |
| `DAYUSE_PIX_AJAX_ERROR` / `CAMPING_PIX_AJAX_ERROR` | Falha de rede ao gerar o QR Code PIX |
| `CARD_CLIENT_NETWORK_ERROR` | Falha de rede ao enviar o pagamento com cartão |

---

## Referências

- **Modelo:** `app/models/integration_log.py`
- **Blueprint (public):** `app/blueprints/logs/routes.py` — registrado em `/api/logs`
- **Blueprint (admin):** `app/blueprints/admin/routes.py` — `GET /admin/logs`
- **Template:** `app/templates/admin/integration_logs.html`
- **API Catalog:** `docs/api_catalog.md` — seção "Module: Logs de Integração"
