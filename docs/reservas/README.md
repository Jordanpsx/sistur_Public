# Módulo: Reservas — Generic Booking System API

> **Source of truth** for the Reservas (Bookings) API contract.
> Update this file whenever a reservas endpoint is added, changed, or removed.

---

## Overview

> **Integrando de fora do Sistur?** Comece por
> [`GUIA_FRONTEND.md`](GUIA_FRONTEND.md) — itens, preços e criação de reserva,
> com as armadilhas que já custaram tempo (o `category_id` opcional do
> `/simular`, o `price_override` que soma o total e não o subtotal, o pool de
> capacidade compartilhado). Este documento descreve o modelo; aquele descreve
> o uso.

Reservas is a **headless API backend** designed to support multiple frontends (legacy WordPress, in-house portal, physical POS counter). The system manages generic bookings across different venues ("sources"), categories, and items with support for:

- **Dynamic Resource Selection:** Pricing tiers (items/tarifas) are decoupled from physical resources. Attendants choose specific resources (rooms, grills, capacity pools) **at booking time**, not in the catalog. This eliminates combinatorial explosion (5 tariffs × 50 resources = 55 records, not 250).
- **Resource Gallery UI:** Resources display with optional thumbnails, descriptions, and real-time availability. Accordion grouping by hierarchy (e.g., "Salas › Reuniões").
- **Soft deletion:** Records are never physically deleted; `deleted_at IS NULL` marks active records.
- **Versioning:** Edits do not update existing rows; instead, a new row is created with the same `group_id` and incremented `version`, and the old row is marked `status=ARCHIVED_VERSION`.
- **Dynamic RBAC:** New venues automatically create permission entries for all super-admin roles.

---

## Dynamic Resource Selection (Catalog Decoupling)

### The Problem: Combinatorial Explosion

Without proper architecture, a simple booking system with 5 tarifas (pricing tiers) × 50 physical resources (venues, rooms, grills, etc.) would require **250 catalog items**:

```
Item: "Day Use Inteira + Sala 01"      ← 1 row
Item: "Day Use Inteira + Sala 02"      ← 1 row
Item: "Day Use Inteira + Sala 03"      ← 1 row
...
Item: "Day Use Inteira + Sala 50"      ← 1 row
Item: "Day Use Meia + Sala 01"         ← 1 row
...
```

This makes management and pricing impossible.

### The Solution: Decouple Pricing from Physical Assets

**Key principle:** Tarifas (ReservaItem) are **generic pricing tiers**. Physical resources (ReservaResource) are chosen **at booking time**, not in the catalog.

```
ReservaSource (Venue: "Cachoeira")
├── ReservaCategory (Type: "Day Use")
│   ├── ReservaItem (Tariff: "Inteira", price=R$100, billing_type=FIXED)
│   │   └── [NO resource_id] — resource selected at booking time
│   └── ReservaItem (Tariff: "Meia", price=R$50, billing_type=FIXED)
│       └── [NO resource_id] — resource selected at booking time
├── ReservaCategory (Type: "Camping")
│   └── ReservaItem (Tariff: "Site com Barraca", price=R$200/night)
│       └── [NO resource_id] — resource selected at booking time
│
└── ReservaResource (Physical Asset: "Sala de Reunião 01", stock=1)
    ├── group_id: 3 (hierarchy: "Salas › Reuniões")
    ├── description: "Sala climatizada, capacidade 20 pessoas, projetor incluído"
    ├── image_url: "https://example.com/sala-01.jpg"
    └── Can be paired with ANY tariff via ReservaItemLink
```

**Result:** 5 + 50 = **55 records**, not 250!

### Booking Flow: How Resources Are Selected

1. **Customer visits booking portal**
   - Sees 5 available tariffs (Day Use Inteira, Meia, etc.) — generic options
   - Selects dates and quantity
   - System shows available resources (rooms, grills, etc.) via `/api/recursos/disponibilidade`

2. **Customer selects physical resource(s)**
   - For each tariff line item, chooses one resource (or none if immaterial)
   - Accordion UI groups resources by hierarchy (Salas › Reuniões › [Sala 01, Sala 02, ...])
   - Real-time availability badges show if resource is free

3. **At booking time (POST /reservas/api/)**
   - System creates ReservaItemLink rows with the selected resource_id
   - Unique constraint `(reserva_id, resource_id)` prevents double-booking the same physical asset
   - Price is validated server-side (anti-fraud check)
   - ReservaItemLink.locked_price captures the price snapshot

### Resource Gallery Fields

Resources now include optional gallery metadata for the UI:

| Field | Type | Purpose |
|---|---|---|
| `group_id` | FK → ReservaGroup | Organizes resources in hierarchy (e.g., "Salas › Reuniões") |
| `description` | String | Text displayed below resource name in accordion (e.g., "Climatizada, 20 pessoas, projetor") |
| `image_url` | URL | Thumbnail image (44×44 or larger) shown in resource gallery |

### Availability Calculation

The `/api/recursos/disponibilidade` endpoint calculates real-time availability:

```
available = stock_quantity - consumed

consumed = SUM(ReservaItemLink.quantity)
           WHERE resource_id = ?
           AND reserva overlaps [check_in, check_out]
           AND reserva NOT CANCELED
           AND reserva NOT ARCHIVED_VERSION
           AND (status = PAID OR (status = PENDING AND expires_at > NOW))
```

If `stock_quantity IS NULL`, the resource is **unlimited** (no overbooking possible).

---

## Data Models

### ReservaSource

Represents a venue or location where bookings are made (e.g., "Cachoeira", "Vinhedo").

**JSON schema (read):**

```json
{
  "id": 1,
  "name": "Cachoeira",
  "is_active": true,
  "deleted_at": null,
  "criado_em": "2026-02-24T18:30:00Z",
  "categories": [
    {
      "id": 1,
      "name": "Day Use",
      "deleted_at": null,
      "criado_em": "2026-02-24T18:30:00Z"
    }
  ]
}
```

### ReservaCategory

A category of bookings within a source (e.g., "Day Use", "Camping").

**JSON schema (read):**

```json
{
  "id": 1,
  "source_id": 1,
  "name": "Day Use",
  "deleted_at": null,
  "criado_em": "2026-02-24T18:30:00Z"
}
```

### ReservaResource

Physical inventory resource pool that multiple items can share (e.g., "Day Use Capacity", "Large Grill"). Includes gallery fields for presentation in resource selection accordion.

**JSON schema (read):**

```json
{
  "id": 1,
  "source_id": 1,
  "group_id": 2,
  "name": "Capacidade Day Use",
  "description": "Área para 50 pessoas, com churrascaria e piscina",
  "image_url": "https://example.com/images/dayuse-01.jpg",
  "stock_quantity": 50,
  "deleted_at": null,
  "criado_em": "2026-02-24T18:30:00Z"
}
```

**Stock:** `null` = unlimited inventory; integer ≥ 0 = limited quantity

**Gallery fields (optional):**
- `group_id` (FK → `ReservaGroup`, nullable): Groups resources in accordion by display hierarchy (e.g., "Churrasqueiras › Pequenas")
- `description` (string, nullable): Text description displayed below resource name in accordion
- `image_url` (string URL, nullable): Thumbnail displayed in resource selection gallery (44×44 or larger)

### ReservaItem

An item/tariff available for booking (e.g., "Cama", "Lanche", "Atividade"). Does **not** specify a resource — the resource is selected at booking time via `ReservaItemLink.resource_id`.

**JSON schema (read):**

```json
{
  "id": 1,
  "category_id": 1,
  "name": "Cama individual",
  "price": "150.00",
  "billing_type": "PER_DAY",
  "requires_deposit": false,
  "deleted_at": null,
  "criado_em": "2026-02-24T18:30:00Z"
}
```

**Billing types:**
- `FIXED` — fixed price per booking
- `PER_DAY` — charged per day in the reservation window
- `PER_HOUR` — charged by the hour

**Note:** This model is **decoupled from resources**. Multiple resources can be used with the same item in different bookings (or even in the same booking if the item appears multiple times with different resources).

### Reserva

The booking itself. Uses **versioning**: edits create new rows with same `group_id` and incremented `version`.

**JSON schema (read):**

```json
{
  "id": 1,
  "group_id": "550e8400-e29b-41d4-a716-446655440000",
  "version": 1,
  "source_id": 1,
  "category_id": 1,
  "customer_name": "João Silva",
  "customer_document": "12345678901",
  "origin": "WEB",
  "status": "PENDING",
  "check_in_date": "2026-03-15",
  "check_out_date": "2026-03-17",
  "expires_at": "2026-03-15T09:15:00Z",
  "subtotal_amount": "300.00",
  "discount_amount": "0.00",
  "service_fee_amount": "0.00",
  "total_amount": "300.00",
  "metadata_info": null,
  "deleted_at": null,
  "criado_em": "2026-02-24T18:30:00Z",
  "atualizado_em": "2026-02-24T18:30:00Z",
  "items": [
    {
      "id": 1,
      "reserva_id": 1,
      "item_id": 1,
      "resource_id": null,
      "quantity": 2,
      "locked_price": "150.00",
      "unit_price": "150.00",
      "item_total": "300.00",
      "deleted_at": null
    }
  ]
}
```

**Origins:**
- `WEB` — booked via web portal
- `BALCAO` — booked at the counter

**Statuses:**
- `PENDING` — awaiting payment or confirmation
- `PAID` — payment confirmed
- `CANCELED` — booking cancelled
- `COMPLETED` — check-out completed
- `ARCHIVED_VERSION` — previous version of a booking (marked when new version created)

### ReservaItemLink

Association between a booking and an item, with a **resource selection**, quantity, and price snapshot. This is where dynamic resource selection happens.

**JSON schema (read):**

```json
{
  "id": 1,
  "reserva_id": 1,
  "item_id": 1,
  "resource_id": null,
  "quantity": 2,
  "locked_price": "150.00",
  "unit_price": "150.00",
  "item_total": "300.00",
  "deleted_at": null
}
```

**Key fields:**
- `resource_id` (FK → `ReservaResource`, nullable): The physical asset selected for this line item at booking time. `null` = item does not require a physical resource (e.g., meals, fees)
- `locked_price` — snapshot of the price at booking time (Numeric, not a FK). This ensures historical accuracy

**Unique constraint:** `(reserva_id, resource_id)` — prevents the same physical asset from appearing twice in the same booking. Items without resources (resource_id IS NULL) are exempt.

**Relationship:** Many-to-many via ReservaItemLink. The same item can be booked with different resources in the same reservation:
```
Reserva #1
├── Item 1 (Day Use Inteira) + Resource A (Salas/Sala 01)
└── Item 1 (Day Use Inteira) + Resource B (Salas/Sala 02)
```

---

## Endpoints

### Module: Reservas (`/reservas`)

#### `GET /reservas/`

Dashboard operacional de reservas.

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `view`
**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `view` | string | "semanal" | `semanal` ou `lista` |
| `q` | string | "" | Free-text search on customer name, CPF or phone |
| `categoria_id` | int | null | Filter by category ID |
| `status` | enum | "" | Filter by booking status (PENDING, PAID, CANCELED, COMPLETED) |
| `sort` | string | "criado_em" | Sort field in list view |
| `dir` | string | "desc" | Sort direction |
| `week_offset` | int | 0 | Week offset in weekly view |

**Response:** `200 text/html` — renderiza `reservas/dashboard.html`

**Granular permissions:**

| Permission | Effect |
|---|---|
| `reservas.view_contact` | Shows phone/contact in list and weekly cards. |
| `reservas.view_financial` | Shows totals in list and weekly summaries. |

Reservation permissions may be global or venue-scoped. `RolePermission.source_id = NULL`
keeps the permission global; a non-null `source_id` limits that permission to the matching
`ReservaSource`.

Weekly view applies `ReservaAccessPolicy.filter_query()` to the reservation query.

**Error responses:**

| Scenario | Status | Body |
|---|---|---|
| Unauthenticated | 401 | Redirect to `/portal/login` |
| Missing permission | 403 | `{"erro": "NAO_AUTORIZADO"}` |

**Audit:** No (read-only)

---

#### `GET /reservas/api/public/catalogo`

Retorna catálogo público de fontes (venues), categorias e itens disponíveis para reserva.
Endpoint público — não requer autenticação.

Itens vinculados a um `ReservaResource` com `stock_quantity = 0` são excluídos automaticamente.
Quantidades de estoque nunca são expostas na resposta.

**Auth required:** No
**Permission required:** None

**Query params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `source_id` | int | No | Filtra resultado para uma única fonte pelo ID. Se omitido, retorna todas as fontes ativas. |

**Success response:** `200 application/json`

```json
{
  "sources": [
    {
      "id": 1,
      "name": "Cachoeira",
      "categories": [
        {
          "id": 1,
          "name": "Day Use",
          "items": [
            {
              "id": 1,
              "name": "Inteira",
              "internal_slug": "dayuse_inteira",
              "price": 100.0,
              "billing_type": "FIXED",
              "requires_deposit": false
            },
            {
              "id": 2,
              "name": "Meia",
              "internal_slug": "dayuse_meia",
              "price": 50.0,
              "billing_type": "FIXED",
              "requires_deposit": false
            }
          ]
        }
      ]
    }
  ]
}
```

**billing_type values:** `FIXED`, `PER_DAY`, `PER_HOUR`

**Error responses:**

| Cenário | Status | Body |
|---|---|---|
| `source_id` não é inteiro válido | 400 | `{"erro": "source_id deve ser um número inteiro."}` |
| Fonte não encontrada ou inativa | 200 | `{"sources": []}` (lista vazia, não 404) |
| Erro inesperado | 500 | `{"erro": "Erro ao carregar catálogo. Tente novamente mais tarde."}` |

**Notes:**
- Categorias com `items: []` são incluídas; o frontend pode ocultá-las se desejar.
- Se `modulo.reservas` estiver desativado em Configurações, retorna `403` (igual a todos os outros endpoints do blueprint).

**Audit:** Não (somente leitura)

---

#### `POST /api/public/simular`

Simulate booking price calculation (public, no authentication required). Used by frontends to calculate real-time totals during checkout.

**Response:** `200 application/json`

```json
{
  "items_breakdown": [
    {
      "item_id": 1,
      "item_name": "Inteira",
      "quantity": 2,
      "unit_price": 100.0,
      "billing_type": "FIXED",
      "item_total": 200.0
    }
  ],
  "subtotal": 500.0,
  "discount_amount": 50.0,
  "discount_details": {
    "early_bird": {"name": "Early Bird", "percent": 10}
  },
  "service_fee": 40.5,
  "service_fee_details": {"percent": 8.1, "amount": 40.5},
  "total": 490.5
}
```

**Audit:** No (read-only)

---

#### `GET /reservas/sources`

List all active venues (ReservaSource).

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `view`

**Response:** `200 application/json`

```json
{
  "sources": [
    {
      "id": 1,
      "name": "Cachoeira",
      "is_active": true,
      "criado_em": "2026-02-24T18:30:00Z",
      "categories": [
        {
          "id": 1,
          "name": "Day Use",
          "criado_em": "2026-02-24T18:30:00Z"
        }
      ]
    }
  ]
}
```

**Audit:** No (read-only)

---

#### `GET /reservas/<int:reserva_id>`

Exibe a página de detalhes de uma reserva (`detalhe.html`).

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `view`
**Path parameters:**

| Param | Type | Description |
|---|---|---|
| `reserva_id` | int | Primary key of the booking |

**Response:** `200 text/html` — renderiza `reservas/detalhe.html`

**Template context:**

| Variable | Type | Description |
|---|---|---|
| `reserva` | `Reserva` | Objeto da reserva com todos os relacionamentos carregados |
| `can_view_contact` | bool | Exibe CPF/CNPJ e telefone. |
| `can_view_financial` | bool | Exibe valores, subtotais, descontos, taxa e saldo. |
| `can_view_payments` | bool | Exibe transações, PIX, dados de pagamento e cauções. |

Essas flags são calculadas para a venue da reserva. Uma permissão escopada por `source_id` libera
o dado apenas quando `reserva.source_id` corresponde à venue da permissão.

**Items table — o que é exibido por linha (`ReservaItemLink`):**
- Badge de grupo (`link.item.group.display_path`) — caminho hierárquico (ex: "Churrasqueiras > Área A")
- Nome do item (`link.item.name`) — ex: "Churrasqueira pequena (A)"
- Nome do recurso físico atribuído (`link.resource.name`) — exibido abaixo do item quando `link.resource_id` não é nulo (ex: "📌 Churrasqueira A1")

**Error responses:**

| Scenario | Status | Response |
|---|---|---|
| Reservation not found or soft-deleted | 404 | Renderiza `reservas/erro.html` |
| Unauthenticated | 401 | Redirect to `/portal/login` |
| Missing permission or denied by policy | 403 | HTTP 403 |

**Audit:** No (read-only)

---

#### `GET /reservas/api/disponibilidade`

Calculate item availability for a given category and date range. This endpoint is used to determine which items are available (or how many units remain) during a specific check-in/check-out window.

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `view`
**Query parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `category_id` | int | Yes | ID of the category to check availability for |
| `check_in` | string (YYYY-MM-DD) | Yes | Check-in date in ISO 8601 format |
| `check_out` | string (YYYY-MM-DD) | Yes | Check-out date in ISO 8601 format |

**Response:** `200 application/json`

```json
{
  "category_id": 1,
  "check_in": "2026-03-15",
  "check_out": "2026-03-17",
  "items": [
    {
      "item_id": 1,
      "name": "Cama individual",
      "billing_type": "PER_DAY",
      "requires_deposit": false,
      "stock_quantity": 10,
      "consumed": 5,
      "available": 5
    },
    {
      "item_id": 2,
      "name": "Lanche",
      "billing_type": "FIXED",
      "requires_deposit": false,
      "stock_quantity": null,
      "consumed": 0,
      "available": null
    }
  ]
}
```

**Response field meanings:**

- `stock_quantity`: Total available units (null = unlimited/infinite stock)
- `consumed`: Units already booked for overlapping date ranges
- `available`: Remaining units available (`null` if stock_quantity is null)

**Error responses:**

| Scenario | Status | Body |
|---|---|---|
| Missing `category_id` | 400 | `{"erro": "Parâmetro 'category_id' é obrigatório."}` |
| Missing `check_in` | 400 | `{"erro": "Parâmetro 'check_in' é obrigatório."}` |
| Missing `check_out` | 400 | `{"erro": "Parâmetro 'check_out' é obrigatório."}` |
| Invalid date format | 400 | `{"erro": "Formato de data inválido. Use YYYY-MM-DD."}` |
| `check_out < check_in` | 400 | `{"erro": "check_out deve ser >= check_in."}` |

**Audit:** No (read-only)

---

#### `GET /reservas/api/recursos/disponibilidade`

Get real-time resource availability grouped by resource hierarchy (for accordion UI in booking forms). This endpoint powers the dynamic resource selection feature.

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `view`
**Query parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `source_id` | int | Yes | ID of the venue to check resources for |
| `check_in_date` | string (YYYY-MM-DD) | Yes | Check-in date in ISO 8601 format |
| `check_out_date` | string (YYYY-MM-DD) | Yes | Check-out date in ISO 8601 format |

**Response:** `200 application/json`

```json
{
  "source_id": 1,
  "check_in_date": "2026-03-15",
  "check_out_date": "2026-03-17",
  "recursos": {
    "Churrasqueiras": {
      "Pequeninhas": [
        {
          "id": 5,
          "name": "Churrasqueira A",
          "description": "Churrasqueira com cobertura, capacidade para 8 pessoas",
          "image_url": "https://example.com/grill-a.jpg",
          "stock_quantity": 1,
          "consumed": 0,
          "available": 1,
          "is_available": true,
          "group_id": 3,
          "group_path": "Churrasqueiras › Pequeninhas"
        },
        {
          "id": 6,
          "name": "Churrasqueira B",
          "description": "Churrasqueira com cobertura, capacidade para 12 pessoas",
          "image_url": "https://example.com/grill-b.jpg",
          "stock_quantity": 1,
          "consumed": 1,
          "available": 0,
          "is_available": false,
          "group_id": 3,
          "group_path": "Churrasqueiras › Pequeninhas"
        }
      ],
      "Grandes": [
        {
          "id": 7,
          "name": "Churrasqueira C",
          "description": null,
          "image_url": null,
          "stock_quantity": null,
          "consumed": 0,
          "available": null,
          "is_available": true,
          "group_id": 4,
          "group_path": "Churrasqueiras › Grandes"
        }
      ]
    }
  }
}
```

**Response field meanings:**

- `stock_quantity`: Total available units (null = unlimited/infinite stock)
- `consumed`: Units already booked for overlapping date ranges (aggregate across all ReservaItemLink rows with this resource_id)
- `available`: Remaining units available (`null` if stock_quantity is null; always integer otherwise)
- `is_available`: Boolean convenience flag (true if available > 0 or stock_quantity is null)
- `group_path`: Full hierarchy path (e.g., "Churrasqueiras › Pequeninhas") for display
- `group_id`: FK → ReservaGroup, used for sorting

**Error responses:**

| Scenario | Status | Body |
|---|---|---|
| Missing `source_id` | 400 | `{"erro": "Parâmetro 'source_id' é obrigatório."}` |
| Missing `check_in_date` | 400 | `{"erro": "Parâmetro 'check_in_date' é obrigatório."}` |
| Missing `check_out_date` | 400 | `{"erro": "Parâmetro 'check_out_date' é obrigatório."}` |
| Invalid date format | 400 | `{"erro": "Formato de data inválido. Use YYYY-MM-DD."}` |
| `check_out_date < check_in_date` | 400 | `{"erro": "check_out_date deve ser >= check_in_date."}` |
| Source not found | 404 | `{"erro": "Source não encontrado."}` |

**Integration:** This endpoint is called via AJAX by the reservation booking form when dates change, populating the resource selection accordion. The response is organized by group hierarchy (group_path) and sorted for consistent UI rendering.

**Audit:** No (read-only)

---

#### `GET /reservas/api/`

List all active bookings with filtering and pagination. Headless JSON API version of the HTML dashboard route.

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `view`
**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | "" | Free-text search on customer name or CPF |
| `source_id` | int | null | Filter by venue ID |
| `status` | enum | "" | Filter by booking status (PENDING, PAID, CANCELED, COMPLETED) |
| `page` | int | 1 | Pagination page number |
| `per_page` | int | 20 | Items per page |

**Response:** `200 application/json`

Returned reservation fields are redacted by `ReservaAccessPolicy.redact_dict()`:

| Missing permission | Redacted fields |
|---|---|
| `reservas.view_contact` | `customer_document`, phone/e-mail/document keys inside `metadata_info`. |
| `reservas.view_financial` | `subtotal_amount`, `discount_amount`, `service_fee_amount`, `total_amount`. |
| `reservas.view_payments` | `payment_id`, `payment_data`, `pix_data`, payment transactions and deposits. |

If a permission is scoped by `RolePermission.source_id`, the field is only returned for reservations
from that same venue. A global permission (`source_id = NULL`) returns the field for all venues.

```json
{
  "reservas": [
    {
      "id": 1,
      "group_id": "550e8400-e29b-41d4-a716-446655440000",
      "version": 1,
      "source_id": 1,
      "category_id": 1,
      "customer_name": "João Silva",
      "customer_document": "12345678901",
      "origin": "WEB",
      "status": "PENDING",
      "check_in_date": "2026-03-15",
      "check_out_date": "2026-03-17",
      "expires_at": "2026-03-15T09:15:00Z",
      "criado_em": "2026-02-24T18:30:00Z"
    }
  ],
  "total": 42,
  "page": 1,
  "per_page": 20
}
```

**Error responses:**

| Scenario | Status | Body |
|---|---|---|
| Invalid `status` value | 400 | `{"erro": "Status inválido: {status}"}` |
| Unauthenticated | 401 | Redirect to `/portal/login` |
| Missing permission | 403 | `{"erro": "NAO_AUTORIZADO"}` |

**Audit:** No (read-only)

---

#### `POST /reservas/api/`

Create a new booking with items. Returns a 15-minute soft lock via `expires_at` timestamp (see **Soft Lock Behaviour** section below).

**Auth required:** Yes → redirects to `/portal/login`
**Permission required:** `reservas` → `create`

**Request body (JSON):**

```json
{
  "source_name": "Cachoeira",
  "category_name": "Day Use",
  "customer_name": "João Silva",
  "customer_document": "12345678901",
  "origin": "WEB",
  "check_in_date": "2026-03-15",
  "check_out_date": "2026-03-17",
  "items": [
    {
      "item_id": 1,
      "resource_id": 5,
      "quantity": 2,
      "price_override": "300.00"
    }
  ]
}
```

**Field descriptions:**

- `source_name` (string, required): Name of the venue. If it doesn't exist, it will be created automatically.
- `category_name` (string, required): Name of the category within the source. If it doesn't exist within the source, it will be created automatically.
- `customer_name` (string, required): Full name of the customer.
- `customer_document` (string, optional): CPF or CNPJ of the customer.
- `origin` (enum, required): Must be `"WEB"` or `"BALCAO"`.
- `check_in_date` (string YYYY-MM-DD, required): Check-in date.
- `check_out_date` (string YYYY-MM-DD, optional): Check-out date. Categories configured as `single_day_only` ignore checkout and are stored as single-day bookings.
- `items` (array, required): At least one item must be specified.
  - `item_id` (int, required): ID of the item to book.
  - `resource_id` (int, optional): ID of the physical resource to assign. `null` or omitted = item does not require a resource.
  - `quantity` (int, required): Number of units to book.
  - `price_override` (string decimal, **required**): Price snapshot at booking time. Example: `"300.00"` or `"99.99"`.

**Response:** `201 Created`

```json
{
  "reserva": {
    "id": 1,
    "group_id": "550e8400-e29b-41d4-a716-446655440000",
    "version": 1,
    "source_id": 1,
    "category_id": 1,
    "customer_name": "João Silva",
    "customer_document": "12345678901",
    "origin": "WEB",
    "status": "PENDING",
    "check_in_date": "2026-03-15",
    "check_out_date": "2026-03-17",
    "expires_at": "2026-03-15T09:15:00Z",
    "criado_em": "2026-02-24T18:30:00Z",
    "items": [
      {
        "id": 1,
        "item_id": 1,
        "resource_id": 5,
        "item_name": "Cama individual",
        "resource_name": "Quarto 301",
        "quantity": 2,
        "locked_price": "150.00"
      }
    ]
  }
}
```

**Error responses:**

| Scenario | Status | Body |
|---|---|---|
| Missing required field | 400 | `{"erro": "Campo obrigatório ausente: '{field_name}'."}` |
| Invalid date format | 400 | `{"erro": "Formato de data inválido. Use YYYY-MM-DD."}` |
| `check_out_date < check_in_date` | 400 | `{"erro": "check_out_date deve ser igual ou posterior a check_in_date."}` |
| Empty items list | 400 | `{"erro": "A reserva deve conter ao menos um item."}` |
| Item not found | 400 | `{"erro": "Item com ID {item_id} não encontrado."}` |
| Missing `price_override` | 400 | `{"erro": "Item {item_id}: campo 'price_override' é obrigatório."}` |
| Insufficient stock | 400 | `{"erro": "Estoque insuficiente para o item '{item_name}'. Disponível: {available}, solicitado: {quantity}."}` |
| Duplicate item in payload | 400 | `{"erro": "Itens duplicados no payload: use um único entry por item_id."}` |
| Invalid `origin` value | 400 | `{"erro": "origin deve ser 'WEB' ou 'BALCAO', recebido: {origin}"}` |
| Unauthenticated | 401 | Redirect to `/portal/login` |
| Missing permission | 403 | `{"erro": "NAO_AUTORIZADO"}` |
| Cliente banido | 403 | `{"status": "error", "code": "CLIENT_BANNED", "erro": "Sem resposta do servidor. Tente novamente em instantes.", "message": "Sem resposta do servidor. Tente novamente em instantes."}` |

**Audit:** Yes (`AuditService.log_create("reservas", ...)`)

---

#### `POST /api/internal/reservas/ingest`

Endpoint headless para ingestion de reservas por sistemas internos via Bearer token (`QR_SERVICE_TOKEN`). Aplica automaticamente status PENDING + soft lock de 15 minutos. Campos extras do payload são armazenados em `metadata_info`.

**Auth required:** `Authorization: Bearer {QR_SERVICE_TOKEN}` — token configurado na env var do container Sistur. Sem sessão de usuário.

**Suporta dois formatos de itens:**

---

**Formato 1 — Item único** (legado, compatível com sistemas existentes):

```json
{
  "source_name": "Cachoeira",
  "category_name": "Day Use",
  "customer_name": "João Silva",
  "check_in_date": "2026-03-15",
  "item_id": 1,
  "resource_id": 5,
  "quantity": 2,
  "price_override": "60.00",
  "check_out_date": "2026-03-16",
  "customer_document": "12345678901",
  "origin": "WEB",
  "metadata_info": {
    "telefone": "11999990000",
    "email": "joao@email.com"
  }
}
```

**Formato 2 — Múltiplos itens** (WordPress plugin Day Use, multi-item bookings):

```json
{
  "source_name": "Cachoeira",
  "category_name": "Day Use",
  "customer_name": "João Silva",
  "check_in_date": "2026-03-15",
  "check_out_date": "2026-03-16",
  "customer_document": "12345678901",
  "origin": "WEB",
  "items": [
    {"item_id": 1, "resource_id": 5, "quantity": 2, "price_override": "60.00"},
    {"item_id": 2, "resource_id": null, "quantity": 1, "price_override": "15.00"},
    {"item_id": 3, "resource_id": 7, "quantity": 1, "price_override": "80.00"}
  ],
  "metadata_info": {
    "telefone": "11999990000",
    "email": "joao@email.com",
    "hora": "10:00",
    "forma_pagamento": "pix",
    "trazer_pet": "nao"
  }
}
```

**Campos obrigatórios (ambos os formatos):**

| Campo | Tipo | Descrição |
|---|---|---|
| `source_name` | string | Nome do venue (cria se não existir) |
| `category_name` | string | Nome da categoria dentro do venue |
| `customer_name` | string | Nome completo do cliente |
| `check_in_date` | string YYYY-MM-DD | Data de entrada |

**Campos obrigatórios — Formato 1 (item único):**

| Campo | Tipo | Descrição |
|---|---|---|
| `item_id` | int | ID do item (`ReservaItem.id`) |
| `price_override` | string decimal | Total do item (ex: `"60.00"`). Deve satisfazer validação anti-fraude do servidor |

**Campos obrigatórios — Formato 2 (múltiplos itens):**

| Campo | Tipo | Descrição |
|---|---|---|
| `items` | array | Lista de `{item_id, quantity, price_override}`. Mínimo 1 item |
| `items[].item_id` | int | ID do item (`ReservaItem.id`) |
| `items[].quantity` | int | Quantidade. Padrão: 1 |
| `items[].price_override` | string decimal | Total da linha = `unit_price × quantity`. Soma de todos os `price_override` deve corresponder ao total calculado pelo servidor (tolerância de R$0,01) |

**Campos opcionais (ambos os formatos):**

| Campo | Tipo | Descrição |
|---|---|---|
| `check_out_date` | string YYYY-MM-DD | Data de saída. Categorias de dia único ignoram checkout e são gravadas sem saída |
| `customer_document` | string | CPF ou CNPJ |
| `origin` | string | `"WEB"` ou `"BALCAO"`. Padrão: `"WEB"` |
| `quantity` | int | (Formato 1 apenas) Quantidade. Padrão: 1 |
| `metadata_info` | object | Metadados arbitrários explícitos. Mergeado com campos restantes do payload |
| `*` | any | Campos extras são incorporados ao `metadata_info` |

**Validação anti-fraude de preço:**

O servidor recalcula o total via `ReservaPricingService.calculate()`. A soma de todos os `price_override` deve coincidir com o total calculado (tolerância de `0.01`). Caso haja divergência, retorna `400` com a diferença explícita.

**Response:** `201 Created`

```json
{
  "sucesso": true,
  "reserva_id": 42,
  "group_id": "550e8400-e29b-41d4-a716-446655440000",
  "expires_at": "2026-03-15T09:15:00Z"
}
```

**Error responses:**

| Cenário | Status | Body |
|---|---|---|
| Token Bearer ausente ou inválido | 401 | `<html>401 Unauthorized</html>` |
| Body JSON ausente | 400 | `{"erro": "Body JSON ausente."}` |
| Campo obrigatório ausente | 400 | `{"erro": "Campo obrigatório ausente: '{field}'."}` |
| Nem `items` nem `item_id`+`price_override` | 400 | `{"erro": "Payload deve conter 'items' (array) ou 'item_id' + 'price_override'."}` |
| Formato de data inválido | 400 | `{"erro": "Formato de data inválido. Use YYYY-MM-DD."}` |
| Item não encontrado | 400 | `{"erro": "Item com ID {item_id} não encontrado."}` |
| `price_override` ausente no item | 400 | `{"erro": "Item {item_id}: campo 'price_override' é obrigatório."}` |
| Divergência de preço anti-fraude | 400 | `{"erro": "Divergência de preço: O valor calculado pelo servidor ({server:.2f}) difere do valor enviado ({frontend:.2f}). Diferença: {diff:.2f}."}` |
| Estoque insuficiente | 400 | `{"erro": "Estoque insuficiente para o item '{name}'. Disponível: {n}, solicitado: {q}."}` |
| Itens duplicados | 400 | `{"erro": "Itens duplicados no payload: use um único entry por item_id."}` |
| Cliente banido | 403 | `{"sucesso": false, "status": "error", "code": "CLIENT_BANNED", "erro": "Sem resposta do servidor. Tente novamente em instantes.", "message": "Sem resposta do servidor. Tente novamente em instantes."}` |

**Auditoria:** Sim (`AuditService.log_create("reservas", ...)` via `ReservaService.criar()`)

**Integração WordPress:** O plugin `reservas-cachoeira-girassol` usa este endpoint via `Sistur_API_Client::create_booking()` com Bearer token configurado em Settings → Sistur API. Para Day Use multi-item, envia Formato 2 com `price_override = unit_price × quantity` por item.

**Banimentos:** Antes de persistir, `ReservaService.criar()` consulta entradas ativas de `sistur_blacklist_entries` vinculadas ao `ReservaSource` alvo. O bloqueio pode ocorrer por nome, CPF, telefone (`metadata_info.telefone`/`phone`/`customer_phone`) ou e-mail (`metadata_info.email`/`customer_email`). A resposta `403 CLIENT_BANNED` permite que o WordPress exiba a mensagem pública genérica sem criar a reserva.

---

## Future Endpoints (Planning)

The following endpoints are planned but not yet implemented:

### `POST /reservas/<int:reserva_id>/editar`

Edit an existing booking (creates a new version).

**Expected request body:**

```json
{
  "customer_name": "João Silva Atualizado",
  "check_out_date": "2026-03-18",
  "items": [
    {
      "item_id": 1,
      "resource_id": 5,
      "quantity": 3,
      "price_override": "160.00"
    }
  ]
}
```

**Field descriptions:**
- `resource_id` (int, optional): ID of physical resource to assign to this line item. `null` or omitted = no resource required
- `price_override` (string decimal): Price snapshot at booking time

**Expected behavior:**
- Mark old row as `status=ARCHIVED_VERSION`
- Create new row with same `group_id`, incremented `version`, and updated fields
- Create new `ReservaItemLink` rows for the new version (including `resource_id`)
- Log the edit in `AuditLog` with `previous_state` and `new_state`
- Return `200 OK` with the new booking

---

### `POST /reservas/<int:reserva_id>/cancelar`

Cancel a booking.

**Expected behavior:**
- Mark booking as `status=CANCELED`
- Log the cancellation in `AuditLog`
- Return `200 OK`

---

## Versioning & Editing Behavior

When a booking is edited:

1. **Old row:** Marked with `status=ARCHIVED_VERSION` and remains in the database
2. **New row:** Inserted with the same `group_id`, version incremented, and updated field values
3. **Both rows:** Share the same `group_id` (UUID). The unique constraint `(group_id, version)` ensures no duplicate versions
4. **Audit trail:** All versions are preserved in the database; `AuditLog` captures the change

**Query active version:**

```sql
SELECT * FROM sistur_reservas
WHERE group_id = '550e8400-e29b-41d4-a716-446655440000'
  AND status != 'ARCHIVED_VERSION'
  AND deleted_at IS NULL
ORDER BY version DESC
LIMIT 1;
```

---

## Soft Delete Pattern

All Reservas models use `deleted_at: DateTime` for logical deletion (instead of `ativo: Boolean`).

**Rules:**
- `deleted_at IS NULL` → record is **active**
- `deleted_at IS NOT NULL` → record is **soft-deleted**
- Queries must always filter `deleted_at IS NULL` by default
- Physical deletion is **never** performed (preserves audit trail)

**Query active records:**

```sql
SELECT * FROM sistur_reservas WHERE deleted_at IS NULL;
```

---

## Dynamic RBAC Hook

When a new `ReservaSource` is inserted, a SQLAlchemy event listener automatically creates permission entries for all `super_admin` roles.

**Permissions created:**
- `modulo = "reservas_{source_name_lower}"`
- `acao = "view"`
- `acao = "edit"`

**Example:** Inserting `ReservaSource(name="Cachoeira")` creates:
- `RolePermission(role_id=1, modulo="reservas_cachoeira", acao="view")`
- `RolePermission(role_id=1, modulo="reservas_cachoeira", acao="edit")`

---

## Soft Lock Behaviour

When a booking is created via `POST /reservas/api/`, the system implements an **optimistic locking mechanism** using the `expires_at` timestamp to temporarily reserve inventory without requiring payment confirmation.

### How It Works

1. **Creation:** `POST /reservas/api/` creates a new `Reserva` with:
   - `status = PENDING`
   - `expires_at = NOW + 15 minutes`

2. **Inventory Hold:** During the 15-minute window, the inventory system treats the booking as **active** when calculating availability:
   - `GET /reservas/api/disponibilidade` includes items from PENDING bookings with `expires_at > NOW`
   - Stock is marked as "consumed" even though payment hasn't been confirmed
   - This prevents double-booking of limited items

3. **Expiration:** After 15 minutes:
   - The PENDING booking becomes "stale"
   - **Availability calculations** exclude it (because `expires_at ≤ NOW`)
   - The inventory is freed up for other bookings
   - **The old booking row remains in the database** (soft delete not applied)

4. **No Background Cleanup:**
   - Stale PENDING bookings are **never automatically deleted or archived**
   - They accumulate in the database indefinitely
   - This is intentional: they serve as a historical audit trail
   - Queries simply ignore them via the `expires_at > NOW` condition

### Implementation Detail

In `ReservaService.verificar_disponibilidade()`:

```python
overlapping_ids = (
    db.session.query(Reserva.id)
    .filter(
        Reserva.category_id == category_id,
        Reserva.deleted_at.is_(None),
        Reserva.status != ReservaStatus.ARCHIVED_VERSION,
        Reserva.status != ReservaStatus.CANCELED,
        or_(
            and_(
                Reserva.status == ReservaStatus.PENDING,
                Reserva.expires_at > datetime.now(timezone.utc),  # ← only non-stale
            ),
            Reserva.status == ReservaStatus.PAID,
        ),
        Reserva.check_in_date <= req_out_effective,
        effective_checkout >= check_in,
    )
    .all()
)
```

**`_verificar_estoque()` applies the same rule during creation** — but only since
2026-08-17. Before that it did not, and this document claimed it did.

The bug: `_verificar_estoque` derived its blocking statuses purely from
`ReservaStatusConfig.blocks_resource`, and `PENDING` is configured `false`. So an
unpaid booking held nothing. The same stock-1 barbecue pit could be booked any
number of times, and the customers only found out at the counter. The two
functions disagreed: `verificar_disponibilidade` reported "sold out" while
`criar()` accepted the booking.

The fix keeps the two rules distinct, because they are genuinely different:

| Source | Meaning | Bounded by `expires_at`? |
|---|---|---|
| `ReservaStatusConfig.blocks_resource` | Permanent hold (`PAID`, `EM_USO`, `DEPOSIT`, `PARTIALLY_PAID`) | **No** |
| `ReservaService._STATUS_COM_SOFT_LOCK` | Temporary hold (`PENDING`) | **Yes** |

Two traps this arrangement avoids, both verified against production data:

- **Do not fix this by flipping `blocks_resource` to `true` for PENDING.**
  `_verificar_estoque` would then ignore the deadline, and the 530 abandoned
  PENDING rows (oldest from March) would block their resources forever.
- **Do not apply the deadline to the other statuses.** `expires_at` is never
  cleared on payment, so all 120 `PAID` and 614 `EM_USO` rows carry a deadline
  in the past. Expiring them would release every paid booking at once.

Regression tests: `tests/services/test_reserva_soft_lock.py`.

### Use Cases

**Scenario 1: Customer completes payment within 15 minutes**
- Booking remains PENDING → `POST /reservas/<id>/pagar` updates status to PAID
- Inventory hold is maintained indefinitely
- Stale rows never expire

**Scenario 2: Customer abandons booking**
- 15 minutes pass
- New bookings can now use the reserved inventory
- Old PENDING row remains in database (archived by time, not deleted)
- Useful for analytics: "How many bookings started but never completed?"

**Scenario 3: Manual admin cleanup (future endpoint)**
- A future `DELETE /reservas/<id>` endpoint could hard-delete stale rows
- Or move them to an `ARCHIVED_VERSION` status if they need to be visible in reports
- Currently not implemented

---

## UI Templates & Admin Interface

### Admin: Resource Management (`resource_form.html`)

When creating/editing a `ReservaResource`, the admin form includes:

| Field | Type | Required | Description |
|---|---|---|---|
| `source_id` | Dropdown | ✅ | Select venue (resource belongs to) |
| `group_id` | Dropdown | ❌ | Select display group (e.g., "Salas › Reuniões") |
| `name` | Text | ✅ | Resource name (e.g., "Sala 01") |
| `description` | Textarea | ❌ | Gallery description (exibida no accordion) |
| `image_url` | URL | ❌ | Gallery thumbnail image URL |
| `is_unlimited` | Checkbox | — | "No limit to capacity" (sets stock_quantity = NULL) |
| `stock_quantity` | Number | ✅ | Max capacity (auto-disabled if "unlimited") |

### Admin: Booking Edits (`reserva_edit.html`)

When admins edit a reservation, they can select or change the physical resource assigned to each tariff line item.

**Template context (route: `GET /reservas/admin/reservas/<reserva_id>/editar`):**

| Variable | Type | Description |
|---|---|---|
| `reserva` | `Reserva` | Objeto da reserva sendo editada |
| `itens_disponiveis` | `list[ReservaItem]` | Itens da categoria da reserva |
| `qtd_atual` | `dict[int, int]` | `item_id → quantity` dos links ativos |
| `resource_atual` | `dict[int, int]` | `item_id → resource_id` dos links ativos |
| `resource_atual_nome` | `dict[int, str]` | `item_id → resource.name` dos links ativos (para exibição no label do accordion) |
| `grupos_tree` | `list` | Árvore hierárquica de grupos de recursos do source da reserva |
| `recursos_sem_grupo` | `list[ReservaResource]` | Recursos sem grupo associado |
| `telefone_atual` | `str` | Telefone extraído de `reserva.metadata_info` |

**Resource Selection UI:**
- Each item (tariff) has a collapsible `<details>` accordion
- Accordion summary label shows:
  - **"Recurso selecionado — {nome}"** when a resource is already assigned (uses `resource_atual_nome`)
  - **"Selecionar recurso físico (opcional)"** when no resource is assigned
- Accordion groups resources by their `ReservaGroup` hierarchy
- Each resource displays:
  - Thumbnail image (if available)
  - Resource name
  - Description (if available)
  - **Real-time availability badge** (green = available, red = unavailable, "..." = loading)
  - Capacity info (if stock_quantity is not null)
- Radio buttons allow one resource per item (or "Sem recurso físico" / None)

**Changing a churrasqueira (or any physical resource):**
- Operator selects a different radio button inside the accordion
- On submit, the new `resource_{{ item.id }}` form field is sent
- `ReservaService.atualizar()` creates a new version with the updated `resource_id` on the affected `ReservaItemLink`

**AJAX Availability Loading:**
- When dates change, JavaScript calls `/api/recursos/disponibilidade`
- 300ms debounce to avoid excessive API calls
- Availability badges update to show real-time status
- UI automatically opens accordion groups with selected resources

**JavaScript features:**
- Accordion chevron rotation on toggle (`<details>` state)
- Resource option highlight on selection (teal border + background)
- Debounced availability reload on date changes
- Graceful fallback if AJAX fails

### Admin: Mapa de Ativos (`mapa_ativos.html`)

The Asset Map provides a real-time operational view of physical resource occupancy.

**Key Features:**
- **Hierarchical Accordion View**: Resources are grouped using a nested `<details>` structure based on their `ReservaGroup` assignments (e.g., *Zona Norte › Churrasqueiras Pequenas*) or labeled as "Independent" if ungrouped.
- **Recursive Rendering**: Implementation uses Jinja2 macros (`render_group_map`) to handle arbitrary levels of nesting.
- **Dynamic Summary Metrics**: JavaScript-powered badges (Total, Available, Occupied) that automatically recalculate based on **visible items**. If a group is collapsed, its assets are excluded from the top-level summary, allowing operators to focus on specific zones.
- **Asset Cards**: Compact grid-based cards with dynamic slot colors driven by `ReservaStatusConfig`:
  - **Green**: Free (always static green — no status involved)
  - **Status color** (e.g., green for PAID, amber for PENDING): Occupied slot — color comes from `status_config_map[slot.status].color_hex`
  - **DEPOSIT color** (default purple `#7C3AED`): Deposit (Caução) slots take visual precedence — color comes from `status_config_map['DEPOSIT'].color_hex`
- **Dynamic Blocking Statuses**: The links query filters using statuses with `ReservaStatusConfig.blocks_resource=True`, not hardcoded values.
- **Template context:** `status_config_map: dict[str, ReservaStatusConfig]` — passed by `mapa_ativos()` route.

**URL**: `/reservas/radar/ativos`
**Permission**: `reservas` → `view`

---

### Admin: Status Config (`status_config.html`)

Settings page for dynamic status configuration. Allows operators to update labels, colors, and business rules without code changes.

**URL**: `GET /reservas/admin/config/status`
**Permission**: `reservas` → `edit`

**Model: `ReservaStatusConfig`** — table `sistur_reserva_status_config`

| Column | Type | Description |
|---|---|---|
| `internal_name` | String(50), unique | Slug matching `ReservaStatus` enum OR special key (e.g., `DEPOSIT`) |
| `label` | String(100) | Portuguese label displayed in UI |
| `color_hex` | String(7) | Hex color `#RRGGBB` for badges and slot indicators |
| `blocks_resource` | Boolean | If `True`, reservas with this status count as occupying stock |
| `show_on_dashboard` | Boolean | If `True`, status appears in dashboard summary cards |
| `is_system_reserved` | Boolean | If `True`, entry cannot be deleted via admin panel |

**Default seed data** (auto-inserted on table creation):

| internal_name | label | color_hex | blocks_resource | show_on_dashboard | is_system_reserved |
|---|---|---|---|---|---|
| `PENDING` | Pendente | `#F59E0B` | ✅ | ✅ | ✅ |
| `PAID` | Pago | `#10B981` | ✅ | ✅ | ✅ |
| `COMPLETED` | Finalizada | `#0D9488` | ❌ | ✅ | ✅ |
| `CANCELED` | Cancelada | `#EF4444` | ❌ | ✅ | ✅ |
| `DEPOSIT` | Com Caução | `#7C3AED` | ❌ | ❌ | ❌ |

**Special `DEPOSIT` entry**: Not a `ReservaStatus` value — used exclusively for the Mapa de Ativos slot color when `slot.has_deposit=True`. Can be deleted or recolored by operators.

**Admin actions (POST routes):**
- `POST /reservas/admin/config/status/<id>/editar` — update label/color/flags
- `POST /reservas/admin/config/status/<id>/deletar` — delete (blocked for `is_system_reserved=True`)
- `POST /reservas/admin/config/status/novo` — create new custom entry

---

## Error Format

All error responses follow the standard format:

```json
{
  "erro": "ERROR_CODE",
  "mensagem": "Human-readable error message"
}
```

**Common error codes:**

| Code | Meaning |
|---|---|
| `NAO_AUTORIZADO` | Authenticated but lacks permission |
| `NAO_ENCONTRADO` | Resource does not exist |
| `VALIDACAO_FALHOU` | Invalid input (e.g., missing required field) |
| `RECURSO_DUPLICADO` | Resource already exists (e.g., duplicate venue name) |
| `ESTOQUE_INSUFICIENTE` | Item quantity exceeds available stock |

---

## Timestamps

All timestamps are **ISO 8601, UTC**:
- Format: `"2026-02-24T18:30:00Z"`
- Always UTC (`Z` suffix or `+00:00`)
- Used in: `criado_em`, `atualizado_em`, `deleted_at`, `expires_at`

---

## Auth & Permissions

All endpoints (except `/portal/login` and `/portal/logout`) require:

1. **Session cookie:** `funcionario_id` in the server-side session
2. **RBAC permission:** The route decorator `@require_permission(modulo, acao)` enforces access control
3. **Super-admin bypass:** Users with `Role.is_super_admin=True` bypass all permission checks

**Permission format:**
- `modulo` = "reservas"
- `acao` = "view" | "view_contact" | "view_financial" | "view_payments" | "create" | "edit" | "delete" | "export" | "configurar"

### `edit` vs. `configurar`

| Ação | Significa | Cobre |
|---|---|---|
| `edit` | mexer na reserva **de um cliente** | `/reservas/admin/reservas/<id>/editar`, CRUD de inventário (fontes, recursos, itens, grupos) |
| `configurar` | administrar o **módulo** | tudo em `/reservas/admin/config/*`: hub, ajustes de preço, taxa de serviço, status, banimentos, avisos e o calendário de datas especiais |

A separação existe porque quem opera o balcão precisa de `edit` para corrigir uma reserva,
mas não deveria poder mudar a tabela de preços do venue inteiro.

A migration `54fb7d898c6d` concede `configurar` a toda role que já possuía `reservas.edit`
com `source_id IS NULL`, então nenhum perfil existente perde acesso no deploy.
`has_permission` exige escopo global (`source_id IS NULL`) para qualquer ação que não seja
`view` — por isso `configurar` não é escopável por venue.

---

## Service Layer

Business logic for all operations lives in `app/services/reserva_service.py` (to be implemented).

**Mandatory operations:**

1. **Every mutation calls `AuditService`** (auditoria obrigatória)
   - `AuditService.log_create()` — when creating
   - `AuditService.log_update()` — when editing (with `previous_state` and `new_state`)
   - `AuditService.log_delete()` — when soft-deleting

2. **All service methods are `@staticmethod`** with `ator_id: int | None` parameter
   - No Flask imports
   - Pure Python, Cython-ready

**Example signature (to be implemented):**

```python
class ReservaService:
    @staticmethod
    def criar(
        source_id: int,
        category_id: int,
        customer_name: str,
        customer_document: str | None,
        origin: ReservaOrigin,
        check_in_date: date,
        check_out_date: date | None,
        items: list[dict],
        ator_id: int | None = None,
    ) -> Reserva:
        """Cria uma nova reserva com itens associados.

        Args:
            source_id: ID da fonte (venue).
            category_id: ID da categoria.
            customer_name: Nome do cliente.
            customer_document: CPF ou CNPJ (opcional).
            origin: Origem da reserva (WEB ou BALCAO).
            check_in_date: Data de check-in.
            check_out_date: Data de check-out (opcional).
            items: Lista de {"item_id": int, "quantity": int}.
            ator_id: ID do usuário que está criando a reserva.

        Returns:
            Instância de Reserva persistida no banco de dados.

        Raises:
            ValueError: Se source_id, category_id ou item_id não existir.
            ValueError: Se estoque for insuficiente.
        """
```

---

## Migration & Deployment

### Schema Changes (Database)

The Dynamic Resource Selection feature requires the following schema migrations:

**`ReservaResource` model — NEW fields:**
- `group_id` (FK → `sistur_reserva_groups.id`, nullable, SET NULL)
- `description` (Text, nullable)
- `image_url` (VARCHAR 512, nullable)

**`ReservaItem` model — REMOVED field:**
- `resource_id` (FK → `sistur_reserva_resources.id`) — **NO LONGER USED**
  - Resources are now selected at booking time via `ReservaItemLink.resource_id`

**`ReservaItemLink` model — ADDED field:**
- `resource_id` (FK → `sistur_reserva_resources.id`, nullable, SET NULL)
- **Unique constraint changed:** from `(reserva_id, item_id)` to `(reserva_id, resource_id)`
  - Reason: Prevents the same physical asset from being booked twice in the same reservation

### Running Migrations

```bash
# Local development
flask db migrate -m "Dynamic resource selection: move resource_id to item_link, add gallery fields"
flask db upgrade

# VPS production
docker exec -it sistur-flask-app flask db migrate -m "Dynamic resource selection: ..."
docker exec -it sistur-flask-app flask db upgrade
```

The migration is **automatically applied** during GitHub Actions deploy (configured in `.github/workflows/deploy.yml`).

### Data Migration Strategy

If upgrading from a previous version where `ReservaItem.resource_id` was populated:

1. **Freeze bookings:** Put system in maintenance mode (`SystemSetting.modulo.reservas = false`)
2. **Backfill `ReservaItemLink.resource_id`:** Copy existing `ReservaItem.resource_id` to matching `ReservaItemLink` rows:
   ```sql
   UPDATE sistur_reserva_item_links r
   SET resource_id = (
     SELECT resource_id FROM sistur_reserva_items i
     WHERE i.id = r.item_id
   )
   WHERE resource_id IS NULL;
   ```
3. **Verify:** Check that all active items have resources assigned
4. **Resume operations:** Set `SystemSetting.modulo.reservas = true`

**Note:** For new installations, no data migration is needed — the schema is created cleanly from models.

---

## Sistema de Status Dinâmico

### Visão Geral

O status de uma reserva é armazenado como `VARCHAR(50)` (não mais `ENUM` MySQL). Isso permite a criação de **status customizados** via painel admin sem alteração de código ou migrações.

### Modelo: `ReservaStatusConfig`

Tabela `sistur_reserva_status_config` — configuração em tempo de execução dos status.

| Campo | Tipo | Descrição |
|---|---|---|
| `id` | int | PK |
| `internal_name` | string(50), unique | Slug identificador (ex: `PENDING`, `NEW_CUSTOM`) |
| `label` | string(100) | Rótulo exibido na interface (pt-BR) |
| `color_hex` | string(7) | Cor hexadecimal para badges e calendário |
| `blocks_resource` | bool | Se `True`, reservas neste status ocupam estoque físico |
| `show_on_dashboard` | bool | Se `True`, aparece nos cards da visão semanal |
| `is_system_reserved` | bool | Se `True`, impede exclusão/renomeação via admin |

**Status pré-populados (seed automático na criação da tabela):**

| `internal_name` | `label` | `blocks_resource` | `show_on_dashboard` | `is_system_reserved` |
|---|---|---|---|---|
| `PENDING` | Pendente | ✅ | ✅ | ✅ |
| `PARTIALLY_PAID` | Parcialmente Pago | ✅ | ✅ | ✅ |
| `PAID` | Pago | ✅ | ✅ | ✅ |
| `COMPLETED` | Finalizada | ❌ | ✅ | ✅ |
| `CANCELED` | Cancelada | ❌ | ✅ | ✅ |
| `DEPOSIT` | Com Caução | ✅ | ❌ | ❌ |

### TypeDecorator: `ReservaStatusType`

A coluna `Reserva.status` usa um `TypeDecorator` personalizado (`app/models/reservas.py`) que:

1. **Salva no banco:** `VARCHAR(50)` — qualquer string
2. **Lê do banco:**
   - Se o valor é um nome de `ReservaStatus` enum (ex: `"PENDING"`) → retorna o membro do enum
   - Se o valor é um status customizado (ex: `"NEW"`) → retorna `_StatusStr(valor)`, uma subclasse de `str` com `.value` property
3. **Compatibilidade:** Templates que fazem `{{ reserva.status.value }}` funcionam para ambos os casos, sem nenhuma mudança nos templates

```python
# Coluna Reserva.status definida como:
status = db.Column(ReservaStatusType(), default=ReservaStatus.PENDING, nullable=False, index=True)

# _StatusStr — compatibilidade Jinja2:
class _StatusStr(str):
    @property
    def value(self) -> str:
        return str(self)
```

### Como os status customizados afetam a UI

| Componente | Como usa `ReservaStatusConfig` |
|---|---|
| **Dropdown de status** (`reserva_edit.html`) | Itera `status_configs` para gerar as opções `<select>` |
| **Visão semanal** (`dashboard.html`) | Filtra reservas pelos `internal_name` onde `show_on_dashboard = True` |
| **Mapa de ativos** | Considera como "ocupado" os `internal_name` onde `blocks_resource = True` |
| **Modal de troca rápida** (`dashboard.html`) | Itera `status_configs` para gerar os botões de seleção |

### Criando um Status Customizado

1. Acesse `/reservas/admin/config/status/novo`
2. Preencha `internal_name` (slug), `label`, cor e flags (`blocks_resource`, `show_on_dashboard`)
3. Salve — o novo status aparece imediatamente nos dropdowns e modal de troca

> **Importante:** `is_system_reserved = False` nos status customizados. Eles podem ser editados ou removidos a qualquer momento, desde que não haja reservas ativas com aquele status.

### Migração Necessária (MySQL ENUM → VARCHAR)

Ao implantar esta funcionalidade em um banco já existente (com `ENUM` na coluna), é necessário rodar a migração `reserva_status_varchar`:

```bash
# Dentro do container no VPS
docker exec -it sistur-flask-app flask db upgrade
```

A migração executa:

```sql
ALTER TABLE sistur_reservas
  MODIFY COLUMN status VARCHAR(50) NOT NULL DEFAULT 'PENDING';
```

> Se o deploy foi feito antes da migração, o MySQL retornará `Data truncated for column 'status'` ao tentar salvar um status customizado que não existe no ENUM original. Aplique `flask db upgrade` para resolver.

---

---

## Pagamento por Cartão — Link via Mercado Pago Preferences

### Visão Geral

Além do PIX, o operador pode gerar um **link de pagamento por cartão de crédito** para uma reserva. O link abre o checkout hosted do Mercado Pago, onde o cliente informa os dados do cartão e paga. Quando o pagamento é confirmado, o webhook do MP atualiza a reserva automaticamente para `PAID`.

**Fluxo completo:**

```
Operador clica "Gerar Link de Pagamento"
  └─ POST /reservas/admin/reservas/<id>/gerar-link-cartao
       └─ MercadoPagoService.gerar_link_pagamento_cartao()
            └─ Cria PaymentTransaction(PENDING, payment_method="credit_card")
            └─ sdk.preference().create(external_reference=txn.id)
            └─ Retorna { checkout_url, transaction_id, preference_id, expires_at }

Operador envia checkout_url ao cliente
  └─ Cliente acessa URL → Mercado Pago Hosted Checkout → paga com cartão

Mercado Pago envia webhook POST /api/v1/payments/webhook
  └─ { "type": "payment", "data": { "id": "<mp_payment_id>" } }
       └─ processar_webhook() → _processar_webhook_novo()
            └─ Detecta external_reference = txn.id (UUID)
            └─ Atualiza PaymentTransaction.status = APPROVED
            └─ _recalcular_saldo_reserva() → Reserva.status = PAID
```

### Endpoint

`POST /reservas/admin/reservas/<int:reserva_id>/gerar-link-cartao`

Ver documentação completa em `docs/api_catalog.md`.

### Detalhes da Implementação

**`MercadoPagoService.gerar_link_pagamento_cartao(reserva_id, payer_email=None)`**
- Localização: `app/services/payment_service.py`
- Usa a **Preferences API** do MP (`sdk.preference().create()`), não a Payment API
- O `external_reference` da Preference é o UUID do `PaymentTransaction` criado
- Quando o cliente paga, o MP cria um Payment que herda o `external_reference` da Preference
- O webhook recebe o `type=payment`, e o `_processar_webhook_novo()` existente identifica pelo UUID e atualiza a reserva — **sem nenhuma alteração no fluxo de webhook**
- A URL de checkout (`init_point`) é armazenada em `PaymentTransaction.ticket_url`
- O `preference_id` e `sandbox_init_point` ficam em `PaymentTransaction.mp_response_snapshot`

**Restrições da Preference:**
- PIX (`bank_transfer`) e boleto (`ticket`) são excluídos — link exclusivo para cartão
- Máximo de 12 parcelas
- Expira em 24 horas a partir da geração

**Idempotência:** Cada clique em "Gerar Link" cria uma nova `PaymentTransaction` e uma nova Preference. Links anteriores expiram normalmente pelo prazo configurado no MP.

---

## Superfície não coberta acima (auditoria 2026-07)

O blueprint `reservas` cresceu para ~107 rotas; as seções anteriores cobrem o núcleo (3-tier,
versionamento, precificação, pagamentos). As famílias abaixo existem no código e **não têm
schema completo neste arquivo** — resumo de contrato está em `docs/api_catalog.md`; aqui vai
só o propósito, para quem for expandir alguma delas escrever o schema no lugar certo.

| Família de rotas | Prefixo | Propósito |
|---|---|---|
| **CRUD de configuração admin** | `/admin/config/status`, `/admin/sources`, `/admin/categorias`, `/admin/recursos` (+ `/todos`, `/restaurar`, `/excluir-definitivo` — soft-delete recuperável), `/admin/grupos`, `/admin/itens` (+ mesmo padrão de restore/purge), `/admin/config/banimentos`, `/admin/config/ajustes-preco` (+ `/tiers`, `/calendar`), `/admin/config/avisos` | Telas de administração do 3-tier inventory (Source→Category→Resource/Item), regras de banimento de clientes, regras de precificação dinâmica (incluindo overrides por calendário) e avisos configuráveis exibidos no checkout |
| **Radar operacional** | `GET /radar/ativos` | Painel ao vivo de reservas ativas/check-ins do dia |
| **Analytics** | `/analytics/`, `/analytics/export/fluxo-caixa.csv`, `/analytics/api/dados` | Dashboard de métricas (receita, ocupação) + export CSV de fluxo de caixa |
| **Consumo/controle de itens** | `/consumo-itens` (+ `/reservas-detalhe`), `/controle-itens` (+ `/reservas-detalhe`, `/link/<id>/entregar`) | Consumo agregado por item (quanto foi vendido de cada tarifa) vs. controle de entrega física (quem já retirou o quê) — dois eixos distintos, não duplicação |
| **Relatório do dia** | `GET /relatorio-dia/<date_str>` | Fechamento operacional de um dia específico |
| **Chat** | `/chat`, `/chat/abrir-contato` | Integração com atendimento (Chatwoot) a partir do contexto de uma reserva |
| **PDV** | `/pdv`, `/pdv/reconciliacao`, `/pdv-sw.js`, `/nova`, `/predefinicoes` | Balcão físico — ver `docs/pdv/README.md` para o fluxo de turno/venda; estas rotas são a ponte reservas↔PDV |
| **Ingestão interna** | `POST /api/internal/ingest` | Mesmo padrão de validação server-side de preço do endpoint público, para integrações internas (POS/legado) |
| **Timeline** | `GET /api/timeline` | Dados para visualização de ocupação ao longo do tempo |

Se for expandir alguma dessas famílias, documente o schema completo aqui antes do próximo push —
o contrato de API é bloqueante para o módulo Reservas, por ser headless e consumido por
múltiplos frontends.

---

## References

- **Models:** `app/models/reservas.py`
- **Blueprint:** `app/blueprints/reservas/routes.py`
- **Service:** `app/services/reserva_service.py`, `app/services/payment_service.py`
- **Templates:**
  - `app/templates/reservas/admin/resource_form.html` — Resource creation/editing
  - `app/templates/reservas/admin/reserva_edit.html` — Reservation editing with resource selection
  - `app/templates/reservas/admin/resource_accordion.html` — Reusable resource selection accordion
- **Related:** `docs/reservas/` — Full API documentation

---

## Datas Especiais e Precificação por Dia

### O gatilho é o flag, não o tipo

Cada `ReservaItem` tem quatro tarifas. A escolha por diária é:

```
data especial → price_holiday   (prioridade máxima)
fim de semana → price_weekend   (Sáb=5, Dom=6 — sexta é dia útil)
dia útil      → price_weekday   (Seg–Sex)
                                 cada nível cai para item.price se for NULL
```

"Data especial" é **qualquer** `GlobalEvent` com `afeta_precificacao=True` — feriado
nacional, feriado local ou alta temporada. O `tipo` do evento é rótulo/cor no calendário,
não regra de preço. A coluna `price_holiday` mantém o nome por compatibilidade de schema;
na UI ela é rotulada "Preço Data Especial".

`ReservaPricingService._load_holidays()` delega a
`CalendarService.datas_que_afetam_preco(inicio, fim)`, cumprindo a regra do Calendário Global:
nenhuma consulta de data especial fora do Calendário Global, nenhuma tabela de feriado
por módulo.

### Períodos

`GlobalEvent.data_fim` (nullable) permite um evento cobrir um intervalo: "Alta Temporada
Julho" é **uma linha** de 01/07 a 31/07, não 31 linhas. `data_fim` vazio = evento de um
dia. Eventos recorrentes anuais com intervalo são projetados no ano consultado e
funcionam mesmo cruzando a virada de ano (ex.: 20/12 → 05/01).

### Onde se cadastra

Duas telas escrevem na mesma tabela `sistur_global_events`, com políticas diferentes —
ver `docs/api_catalog.md` ("Reservas — Configuração do Módulo") e
`docs/configuracoes/README.md` ("Calendário Global") para o contrato completo:

- `/reservas/admin/config/ajustes-preco` — admin do módulo (`reservas.configurar`). Cria
  eventos que afetam **só preço** (`origem="reservas"`, `afeta_folha=False`).
- `/admin/configuracoes/` — super admin. Enxerga a coluna **Origem**, edita qualquer
  evento e pode **promover** um evento de reservas a feriado do sistema marcando
  "Afeta folha/ponto". Promovido, o evento fica somente-leitura no módulo.

### Taxa de serviço e desconto linear

Ambos vivem em `ReservaConfig` e agora têm UI em `/reservas/admin/config/ajustes-preco`
(`ReservaConfigService`, auditado com módulo `reserva_config`). **Atenção ao formato de
armazenamento — os dois são diferentes**, e `ReservaPricingService` só lê nesses lugares:

| Campo | Como é guardado |
|---|---|
| `service_fee_percent` | chave de topo, valor escalar: `ReservaConfig(config_key="service_fee_percent", config_value=8.1)` |
| `flat_discount_percent` | **aninhado** no JSON: `ReservaConfig(config_key="pricing_rules", config_value={"flat_discount_percent": 5})` |

Isso muda a granularidade da herança:

- `service_fee_percent` herda por chave — a fonte sobrepõe só ele.
- `flat_discount_percent` herda junto com o objeto `pricing_rules` inteiro: se a fonte
  tem `pricing_rules` próprio, ele substitui o global por completo, sem merge
  (é como `_get_config_value` funciona).

Campo vazio na tela remove o override e volta a herdar; quando `pricing_rules` fica sem
nenhuma chave, a linha é apagada em vez de virar `{}` no banco.

As chaves legadas de early bird dentro de `pricing_rules` (`early_bird_enabled`,
`early_bird_days`, `early_bird_discount_percent`) **não** têm UI de propósito — elas
duplicariam as regras Early Bird em tabela (`AdvanceDiscountRule`), que são a fonte de
verdade da tela. A gravação preserva o que já estiver no JSON.
