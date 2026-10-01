# PDV (Point of Sale) — Project Plan

**Status:** Phases 1 & 2 implemented and deployed to staging. Phase 3A/3B implemented in code, including the F5-offline shell fix and the offline limited-stock override UX; real-terminal recovery validation and E2E/concurrency tests still pending.
**Owner module:** `app/blueprints/pdv/` + `app/services/pdv_service.py` + `app/models/pdv.py`, consumes existing `ReservaService` / `ReservaPricingService` / `MercadoPagoService`
**Depends on:** Reservas Core (3-tier inventory), Pagamentos (Mercado Pago)
**Live at:** `GET /reservas/pdv` (staging) — sidebar "PDV (Caixa)", between "Nova Reserva (Balcão)" and "Predefinições"

---

## 0. Implementation Status

| Phase | Status | Commits (develop) |
|---|---|---|
| **Phase 1 — Backend & API** | ✅ Done | `6843bea` (models/service/routes/tests/migration `9794af438eb5`), `8bb8ede` (support endpoints `/shifts/current`, `/sources`) |
| **Phase 2 — SPA (online)** | ✅ Done | `8bb8ede` (initial SPA), `4da5e51` (premium POS layout redesign), `89d032f` (theming fix — semantic tokens) |
| **Phase 3 — Recoverable offline sync** | 🟡 Sync foundation, reconciliation/override backend, F5-offline shell fix, and offline override UX all implemented; real-terminal recovery validation and E2E/concurrency tests still pending | `5e12563` adds IndexedDB `sync_queue`, migration from legacy `localStorage`, service worker registration, Background Sync when available, and `online`/30s fallback. Follow-up work (`1dbfc70`) adds manager reconciliation inbox/API for `PdvOfflineSyncLog` and the backend guardrail for manager-authorized overbooking overrides. Latest pass fixes the F5-offline shell bug (see deviation §8 below — `pdv-sw.js` now caches the Tailwind CDN script and Google Fonts CSS, not just the HTML shell) and adds an offline 🔴-item override flow that records a CPF+reason declaration without granting `force_overbooking` (see deviation §9). AES-GCM remains optional hardening after recovery is proven in real terminals. |

Test coverage: 53 tests in `tests/services/test_pdv_service.py` (adds offline-override-note validation, queue-replay idempotency under retry, stale-cache stock-rejection, and dual-source cash close/approval scenarios); full suite green (1177 passed) as of the last run. Migration applied to the staging Postgres DB.

**Notable deviations from the original plan, and why** (kept here so future edits don't "fix" them back to the original spec by mistake):

1. **IndexedDB store name is `catalog`, keyed by `source_id`** (not `pdv_catalog` as a flat store) — implemented in `pdv.html`'s `idb()`/`idbGet()`/`idbSet()`. Functionally equivalent to §3.1.
2. **The sync queue now lives in IndexedDB (`pdv.sync_queue`)**, with automatic migration from the legacy `localStorage` key (`pdv_queue_v1`). It drains via service worker/Background Sync when available and still has the `online` event + 30s polling fallback while the tab is open. Queue encryption is deliberately **not** an MVP blocker: the operational priority is never losing a real counter sale already paid in cash.
3. **Two extra read endpoints were added** beyond §2.3's table, both needed for the SPA to actually boot:
   - `GET /api/v1/pdv/shifts/current` — returns the operator's OPEN shift (or null), used to restore the POS view after F5 without re-deriving it from the held-cart/localStorage side. Not in the original endpoint table.
   - `GET /api/v1/pdv/sources` — lists active venues for the shift-open venue selector.
4. **`PdvService.registrar_venda` fills in a missing customer name via `ReservaService._proximo_nome_balcao()`** (the existing "Balcão N" sequential walk-in name), matching the legacy counter form's behavior — not explicitly specified in §2.3 but required for parity.
5. **`anexar_comanda` (comanda append) recomputes pricing for the *entire* reservation** (existing lines + new ones combined) via `ReservaPricingService.calculate()` before calling `ReservaService.atualizar()`, and stores `PdvSale.amount_total` as *only the newly-anexed delta* (new total − previous total), not the reservation's grand total — this keeps the shift's cash/sales report (§ Phase 1 `relatorio_turno`) additive and non-double-counting across multiple comanda appends to the same guest.
6. **The UI ships a visual layout beyond §3.4's ASCII sketch**: a 65/35 split (catalog 65% / cart 35%) instead of a roughly-even two-column layout, category filter pills above the search results, item cards (not a bare list) with the price right-aligned and slug/id muted, and a sticky cart footer with 3 fast-payment buttons (Dinheiro/Cartão/Pix) that jump straight into the payment step with the method preselected — a shortcut not in the original keyboard-map spec but additive to it (the `F8`/`1-5` keyboard flow still works unchanged).
7. **Theming constraint discovered during implementation, now binding for any future PDV UI work**: because most of the SPA's markup is injected via JS `innerHTML`, Tailwind's `dark:` variants and literal `slate-*`/`white` utility classes do **not** reliably theme correctly (verified broken in both light and dark mode on first ship, fixed in `89d032f`). All PDV styling must use the project's semantic CSS tokens (`var(--c-surface)`, `--c-surface-2`, `--c-text)`, `--c-text-muted`, `--c-border`, `--c-primary*`, and the `--c-success/warning/danger/info-*` pairs from `_design_system.html`) via dedicated `.pdv-*` component classes defined once in the template's `<style>` block — never Tailwind color utilities inside JS-generated HTML.
8. **Root causes of the F5-offline bug (there were two, found in sequence)**: *(a)* the boot sequence: `bootPos()` awaited `GET /shifts/current` without a try/catch — offline, the fetch rejects, the exception escaped `init()`, and the SPA hung on "Carregando PDV…" forever even with a perfectly cached shell (reproduced via screenshot in staging). Fixed by caching the open shift in `localStorage` (`pdv_shift_v1`, written on every successful online boot, cleared on shift close) and restoring it when `/shifts/current` fails with a network error; `loadSources()`/`openShift()` got the same guards with operator-readable offline messages (opening a shift genuinely requires network — that's a POST). *(b)* the asset layer: the service worker (`pdv-sw.js`) only cached the shell's HTML response, not its runtime dependencies. `_design_system.html` loads Tailwind via the **Play CDN** (`cdn.tailwindcss.com`), which generates every utility class (`hidden`, `flex`, `grid`, ...) live in the browser from a `<script>` tag — with no network, that script 404s from the SW's perspective and the shell renders as unstyled/broken HTML, which read as "F5 offline still fails" in earlier validation. The fix adds a `cache-first-with-background-revalidation` handler for `cdn.tailwindcss.com`, `fonts.googleapis.com`, and `fonts.gstatic.com` (the fonts CSS is parsed to also warm the referenced `.woff2` URLs), warmed on install and on every `PDV_CACHE_SHELL` message from the client. **Any future dependency added to `_design_system.html` (a new CDN script, a new font host) must be added to `pdv-sw.js`'s `ASSET_HOSTS`/`WARM_ASSETS`, or F5-offline will silently regress again.** Cache bumped to `pdv-shell-v2`/`pdv-assets-v1`; the `activate` handler now purges any `pdv-*` cache not in the current known set, so stale shells don't linger across deploys.
9. **Offline limited-stock override is a *client-side declaration*, not a security control**: `PdvService.registrar_venda(offline_override_note={"cpf", "reason"})` lets the SPA add a 🔴 item to the cart while offline after a manager types a CPF (11 digits, not password-verified — there is no network to check it against) and a ≥10-char reason. This is deliberately **not** the same code path as `manager_override`/`force_overbooking` (§2.3, §4.2): it never bypasses `_verificar_estoque()`'s lock. The note travels with the queued sale into `metadata_info.pdv_offline_override_note`, `PdvOfflineSyncLog.payload_snapshot`, and a dedicated `AuditLog` (`action=update`, `new_state.action="offline_override_note"`) purely so a manager can be held accountable for the declaration during reconciliation — the *actual* stock decision is still made by the server at sync time and can and will raise `OverbookingError` → `REJECTED_STOCK` if the item sold out elsewhere in the meantime. Do not "fix" this by wiring `offline_override_note` into `force_overbooking`; that would defeat the entire green/red-light guarantee in §4.2.
10. **Fechamento de caixa now tracks two troco sources**: physical cash drawer and a PIX change fund. `PdvSale.change_source` is set only for CASH sales with change due: `CASH` means the drawer received tendered and paid the change, so the drawer grows by `amount_total`; `PIX` means the drawer keeps the full tendered amount and the PIX fund decreases by `amount_change`. `PdvShift` stores opening/closing/expected values for both sources. If either source differs at close, the operator must enter `discrepancy_reason`; the shift moves to `PENDING_APPROVAL` until a different supervisor approves it. This was added as a backend/API guardrail first; the SPA close modal still needs a full ergonomic pass to expose PIX count and approval UX cleanly.

---

## 1. Executive Summary & Current State Analysis

### 1.1 What exists today ("modo balcão")

Counter sales currently piggyback on the generic reservation form:

- `GET /reservas/nova` (`app/blueprints/reservas/routes.py:1908`) — a **fully server-rendered Jinja page** (`app/templates/reservas/admin/reserva_nova.html`) that dumps the *entire* catalog (all sources/categories/items/resources/groups) into an inline `<script>` JSON blob on every load, regardless of which venue the operator actually works at.
- A `#chk-balcao` checkbox (`reserva_nova.html:64-79`) relaxes validation (customer name optional, check-in defaults to now) and reveals a "Status Inicial" dropdown — this is the *entirety* of "PDV mode." There is no cart object, no held sales, no offline handling, no receipt/change-due UI.
- `POST /reservas/nova` (`routes.py:1948`) is a **classic synchronous form POST**: full page reload on both success (redirect to reservation detail) and failure (re-render the whole page + catalog blob again, with an overbooking modal that forces a full resubmit).
- Anonymous walk-ins get an auto-generated "Balcão N" name (`ReservaService._proximo_nome_balcao`, `reserva_service.py:94`) — a cosmetic convenience, not a real queue/session concept.
- Counter reservations are marked `PAID` by operator self-attestation (a dropdown), with **no integration into `MercadoPagoService`** at all — there is no cash/change workflow, no card-present flow, nothing.

### 1.2 Why this bottlenecks high-volume counter sales

| Problem | Root cause | Impact |
|---|---|---|
| Full page reload per sale | Server-rendered form + redirect-on-success pattern | Operator loses ~2-4s of dead time per transaction; no way to serve the next customer while a payment is pending |
| No cart persistence | Quantities live only as `<input>` values in the DOM | A network blip, browser crash, or accidental navigation loses the entire in-progress sale |
| No "hold sale" | No concept exists at all | If a customer needs to step away, the operator must abandon or force-complete the transaction |
| Catalog re-fetched every visit | No client-side cache scoped to the venue | Unnecessary payload size and latency on every "next customer" |
| No offline mode | No service worker, no local queue | Any internet hiccup — common on a rural/tourism VPS uplink — stops all counter sales dead, even for a bag of firewood paid in cash |
| Price double-computation hack | `/nova` form reverse-engineers `price_override` per item to satisfy the same anti-fraud check built for public/untrusted clients (`routes.py:2107-2131`) | Fragile, and wrong abstraction for a trusted internal channel |
| No payment integration | Operator self-attests `PAID` via dropdown | No reconciliation, no card/PIX terminal support, no accounting trail distinguishing cash vs. card vs. comanda |

### 1.3 What the PDV module solves

A dedicated, keyboard-optimized SPA that treats the counter as a **trusted internal channel** (skip the anti-fraud price-override dance that `/api/public/simular` needs for untrusted web clients — see §2.3), adds a real client-side cart with hold/resume and offline resilience, and integrates payment collection (cash, external card terminal, or Mercado Pago PIX) directly into the sale-close step. Critically, it must **not** weaken the two concurrency safeguards already proven in production:

1. `ReservaService._verificar_estoque()`'s pessimistic `SELECT ... FOR UPDATE` lock (`reserva_service.py:1810-1815`), which serializes stock checks across all three existing channels (WordPress, WhatsApp bot, internal staff UI).
2. The `ReservaStatusConfig.blocks_resource` model — only statuses flagged as blocking (default: `PAID`) reserve physical stock.

The PDV reuses `ReservaService.criar()` as its only write path into inventory — it does not invent a parallel stock model.

---

## 2. Database & Backend Architecture

### 2.1 New tables

All new tables follow existing SISTUR conventions: `sistur_` prefix, `criado_em`/`atualizado_em`, soft delete via `deleted_at`, service layer owns all writes, every mutation calls `AuditService.log()`.

#### `sistur_pdv_shifts` (POS shift / caixa tracking)

Tracks the cash-register session an operator opens at the start of a workday and closes at the end — needed for cash reconciliation (opening float, expected vs. counted cash at close).

```python
class PdvShift(db.Model):
    __tablename__ = "sistur_pdv_shifts"
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sistur_reserva_sources.id"), nullable=False, index=True)
    operador_id = db.Column(db.Integer, db.ForeignKey("sistur_funcionarios.id"), nullable=False, index=True)
    opening_float_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    closing_counted_amount = db.Column(db.Numeric(10, 2), nullable=True)
    closing_expected_amount = db.Column(db.Numeric(10, 2), nullable=True)  # computed from PdvSale sums at close
    status = db.Column(db.Enum("OPEN", "CLOSED", name="pdv_shift_status"), nullable=False, default="OPEN")
    opened_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)
```

Only one `OPEN` shift per `(source_id, operador_id)` at a time — enforced in `PdvShiftService`, not at the DB level (mirrors how `ReservaConfig` enforces uniqueness in code + a DB unique constraint as backstop is *not* appropriate here since multiple operators can have concurrent open shifts at different terminals of the same venue).

#### `sistur_pdv_sales` (the sale record — thin wrapper, not a duplicate of `Reserva`)

A PDV sale is **not** a new inventory model. Every completed sale corresponds 1:1 to a `Reserva` row (created via the exact same `ReservaService.criar()` used by every other channel) or an "Add to Comanda" append (a new `ReservaItemLink` batch against an existing `Reserva`). This table exists purely to record **how the sale was paid** and to power the offline sync queue and shift reconciliation — it is not a second source of truth for stock.

```python
class PdvSale(db.Model):
    __tablename__ = "sistur_pdv_sales"
    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(db.Integer, db.ForeignKey("sistur_pdv_shifts.id"), nullable=False, index=True)
    reserva_id = db.Column(db.Integer, db.ForeignKey("sistur_reservas.id"), nullable=True, index=True)  # null while a comanda-append is still resolving
    client_sale_uuid = db.Column(db.String(36), nullable=False, unique=True)  # idempotency key, generated client-side at cart-close time
    sale_type = db.Column(db.Enum("STANDALONE", "COMANDA_APPEND", name="pdv_sale_type"), nullable=False)
    payment_method = db.Column(db.Enum("CASH", "EXTERNAL_CARD_TERMINAL", "MP_PIX", "MP_CARD", name="pdv_payment_method"), nullable=False)
    amount_total = db.Column(db.Numeric(10, 2), nullable=False)
    amount_tendered = db.Column(db.Numeric(10, 2), nullable=True)   # cash handed over, for change calculation / audit
    amount_change = db.Column(db.Numeric(10, 2), nullable=True)
    synced_offline = db.Column(db.Boolean, nullable=False, default=False)  # true if this sale was queued while offline and synced later
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True)
```

`client_sale_uuid` is the linchpin of the offline Store & Forward design (§4.1) — the client generates it once when the cart is closed, and it is the idempotency key the backend uses to silently no-op a retried/duplicate sync.

#### `sistur_pdv_held_carts` (server-side backstop for "Hold Sale")

The primary hold mechanism is client-side (`localStorage`, §3.1) so it survives offline. This table is a **secondary, best-effort mirror** used only so a held cart survives a full browser/OS crash or the operator switching terminals — not required for MVP but specified here since the roadmap (§5, Phase 2) includes it.

```python
class PdvHeldCart(db.Model):
    __tablename__ = "sistur_pdv_held_carts"
    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(db.Integer, db.ForeignKey("sistur_pdv_shifts.id"), nullable=False, index=True)
    label = db.Column(db.String(100), nullable=True)  # e.g. customer name or "Mesa 3"
    cart_json = db.Column(db.JSON, nullable=False)     # {items: [...], customer_ref, source_id, ...}
    held_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)  # set when restored & completed, or discarded
```

#### `sistur_pdv_offline_sync_log` (audit trail for the sync queue)

```python
class PdvOfflineSyncLog(db.Model):
    __tablename__ = "sistur_pdv_offline_sync_log"
    id = db.Column(db.Integer, primary_key=True)
    client_sale_uuid = db.Column(db.String(36), nullable=False, index=True)
    device_id = db.Column(db.String(100), nullable=False)  # stable per-terminal identifier, not per-user
    payload_snapshot = db.Column(db.JSON, nullable=False)  # exact payload the client queued, for forensics
    outcome = db.Column(db.Enum("ACCEPTED", "REJECTED_STOCK", "REJECTED_DUPLICATE", "REJECTED_VALIDATION", name="pdv_sync_outcome"), nullable=False)
    error_detail = db.Column(db.Text, nullable=True)
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
```

### 2.2 Reused, unmodified models

No changes to `ReservaItem`, `ReservaResource`, `Reserva`, `ReservaItemLink`, or `ReservaStatusConfig`. The PDV is additive.

### 2.3 API endpoints

All under `app/blueprints/pdv/routes.py`, `url_prefix="/api/v1/pdv"`, `@login_required` + a new `pdv.operate` permission (RBAC group `pdv: ["operate", "manage_shift", "view_reports"]`).

| Method & Path | Purpose |
|---|---|
| `POST /api/v1/pdv/shifts/open` | Open a shift: `{source_id, opening_float_amount}` → `PdvShift` |
| `POST /api/v1/pdv/shifts/<id>/close` | Close a shift: `{closing_counted_amount}` → reconciliation report (expected vs. counted, diff) |
| `GET /api/v1/pdv/catalog?source_id=` | **Catalog sync endpoint.** Full item/resource/pricing snapshot for one venue, with a `version` hash/ETag so the client can cheaply detect staleness. Backed by `ReservaService.catalogo()` (already exists) — thin wrapper, cacheable with `Cache-Control` + `ETag`. |
| `POST /api/v1/pdv/quote` | Thin wrapper around the existing `POST /reservas/api/public/simular` logic, but **internal/trusted**: returns server-calculated totals without requiring the client to reverse-engineer `price_override`. |
| `POST /api/v1/pdv/sale` | **Core write endpoint.** See below. |
| `POST /api/v1/pdv/sale/comanda-append` | Append items to an existing open `Reserva` (adds `ReservaItemLink` rows against it) instead of creating a new `Reserva`. |
| `GET /api/v1/pdv/customer-lookup?q=` | Search active reservations/guests by CPF, name fragment, or QR token for comanda linking. |
| `GET /api/v1/pdv/shifts/<id>/report` | Sales report for a shift (by payment method) — reconciliation support. |

#### `POST /api/v1/pdv/sale` — request contract

```json
{
  "client_sale_uuid": "b2f1...-uuid-v4",
  "shift_id": 42,
  "source_id": 1,
  "customer_name": "João Silva",
  "customer_document": "52998224725",
  "items": [{"item_id": 7, "quantity": 2, "resource_id": null}],
  "payment_method": "CASH",
  "amount_tendered": 100.00
}
```

Server behavior (`PdvService.registrar_venda`, in `app/services/pdv_service.py`):

1. **Idempotency check first**: `SELECT PdvSale WHERE client_sale_uuid = ?`. If found, return the existing result (200, not a duplicate creation) — this makes retried offline-sync submissions safe.
2. Calls `ReservaPricingService.calculate()` server-side (never trusts a client-sent total) to get the authoritative price — the PDV UI's own quote (`/pdv/quote`) is only ever a preview.
3. Determines the initial `Reserva.status` directly from `payment_method`:
   - `CASH` / `EXTERNAL_CARD_TERMINAL` → create with status `PAID` **immediately**, in the same DB transaction as the stock check. Unlike the WhatsApp/WordPress/online flows, a counter sale has already collected money by the time this request fires — there is no reason to route it through `PENDING` + the Active-Defense compensating-transaction machinery built for asynchronous payment methods (§4 of the research; `payment_service.py`'s `_emitir_reembolso_automatico`). This is a deliberate divergence from the online flows and should be called out in `docs/pdv/README.md`.
   - `MP_PIX` / `MP_CARD` → create with status `PENDING`, then call the existing `MercadoPagoService` PIX/checkout generation (reuses `generate-pix`/`generate-card-link` logic) — this path **does** inherit the existing webhook + Active Defense reconciliation, because payment confirmation is genuinely asynchronous here.
4. Calls `ReservaService.criar(..., origin=ReservaOrigin.BALCAO, ...)` — same function every other channel uses, so the pessimistic `with_for_update()` lock in `_verificar_estoque()` applies identically. No bypass.
5. On success, writes the `PdvSale` row (payment_method, amount_tendered/change, shift_id) inside the **same transaction** as the `Reserva` creation (SQLAlchemy session, single commit) — a `PdvSale` must never exist without its `Reserva`, and vice versa for standalone sales.
6. On `OverbookingError`, return `409 Conflict` with the conflict detail — the SPA never resubmits a whole form (§1); it shows an inline banner and lets the operator adjust quantity/resource without losing the rest of the cart.

#### Stock lock reuse — no new locking code

The plan explicitly does **not** introduce a second `SELECT ... FOR UPDATE` code path. `PdvService.registrar_venda` calls into `ReservaService.criar()` exactly like `bot_api` and `api_internal` do today, inheriting `_verificar_estoque()`'s existing lock. This is the single most important guardrail in this plan: **the PDV must never touch `ReservaResource`/`ReservaItemLink` rows directly.**

---

## 3. Frontend & State Management Strategy

### 3.1 Client-side caching layer

| Data | Storage | Rationale |
|---|---|---|
| **Active cart (current sale in progress)** | `localStorage` (`pdv_active_cart_v1`), written on every state change (debounced ~150ms) | Must survive F5/crash instantly and synchronously — `localStorage` is simple, synchronous, and small enough (a cart is a few KB) |
| **Held/parked carts** | `localStorage` (`pdv_held_carts_v1`, array) mirrored best-effort to `PdvHeldCart` (§2.1) when online | Same size/access-pattern reasoning as active cart; server mirror only for cross-device recovery |
| **Item catalog (name/id/slug/price, per venue)** | `IndexedDB` (`pdv_catalog` store), refreshed via `GET /api/v1/pdv/catalog` ETag check on shift-open and periodically (e.g., every 15 min if online) | Catalog is larger and semi-structured (needs indexed lookup by name/slug for the search-as-you-type feature) — IndexedDB is the right tool, `localStorage`'s synchronous API and ~5-10MB ceiling make it a poor fit for a catalog that might include images |
| **Completed-offline sale queue (Store & Forward)** | `IndexedDB` (`pdv_sync_queue` store) | Needs structured querying (pending vs. synced), the Background Sync API integrates with IndexedDB natively, and payloads must survive a full app close, unlike a `sessionStorage`-tier need |

**Why not put everything in IndexedDB?** The active-cart/held-cart data must be read synchronously on every keystroke to drive the "0ms" instant filtering requirement — `localStorage` is synchronous, IndexedDB is not. Reserve IndexedDB for the catalog (read-heavy, rarely written) and the sync queue (write-heavy, read only on reconnect).

### 3.2 Unified intelligent search — client-side only

On catalog load, build an in-memory index (array of `{id, name, slug, name_normalized}` — normalized = lowercased, accent-stripped for pt-BR text) once from the IndexedDB catalog. Every keystroke filters this in-memory array (simple substring/prefix match; no fuzzy library needed at this catalog size — typically dozens to low hundreds of items per venue). Zero network calls after the initial catalog sync. Recompute the index only when the catalog ETag changes.

### 3.3 State machine — Hold Sale & F5 recovery

```
                ┌──────────────┐
        start   │   EMPTY      │
       ────────▶│ (no cart)    │◀────────────────────┐
                └──────┬───────┘                      │
                       │ add item                     │ complete sale /
                       ▼                               discard held cart
                ┌──────────────┐   hold    ┌──────────────┐
                │  ACTIVE_CART │──────────▶│  HELD (n)    │
                │ (editing)    │◀──────────│ (parked)     │
                └──────┬───────┘  restore  └──────────────┘
                       │ checkout
                       ▼
                ┌──────────────┐  network/API error
                │  CHECKOUT    │───────────────┐
                │ (payment UI) │               │
                └──────┬───────┘               ▼
                       │ payment ok      ┌──────────────┐
                       ▼                 │ QUEUED_OFFLINE│──▶ (Background Sync,
                ┌──────────────┐         │ (IndexedDB)   │    see §4.1)
                │  COMPLETE    │         └──────────────┘
                │ → reset →    │
                │   EMPTY      │
                └──────────────┘
```

- **On app load**: check `localStorage.pdv_active_cart_v1`. If present and not older than a configurable staleness window (default 4h — a full shift), silently restore into `ACTIVE_CART` state and refocus the search input. This is the F5/crash recovery path — no user action needed.
- **Hold**: snapshot the current cart into `pdv_held_carts_v1` with a label (customer name or auto "Cliente N"), clear the active-cart slot, return to `EMPTY` with search refocused. O(1), no network call.
- **Restore**: pick a held cart from a dropdown/list, move it back into the active-cart slot.
- **Checkout → Complete**: on payment confirmation (cash tendered, or terminal/PIX confirmed), fire `POST /api/v1/pdv/sale`. On success, clear the active cart and return straight to `EMPTY` with the search input focused — no navigation, no page reload, matching the "zero page reloads" requirement.
- **Checkout → Queued Offline**: if `navigator.onLine === false` or the fetch fails with a network error (not an HTTP error — HTTP errors like `409` are real server responses and must be shown, not queued), write the payload to the IndexedDB sync queue instead, mark the sale "pending sync" in the UI (e.g., a small badge/counter), and still return to `EMPTY` immediately — **the operator experience for a Green Light item is unaffected by connectivity**; only Red Light items are blocked in the UI before this state is even reachable (§4.2).

### 3.4 UI/UX Specification — keyboard-first operation (intuitiveness & functionality)

The SPA is not "a prettier form" — it is an operator instrument. The design contract below is what makes it usable at queue speed by a staff member with 10 minutes of training.

#### Layout — three fixed zones, nothing ever moves

```
┌─────────────────────────────────────────────┬──────────────────────┐
│  🔍 [ Busca de item — sempre focada ]       │  CARRINHO            │
│  ┌───────────────────────────────────────┐  │  2× Lenha      30,00 │
│  │ ▸ Lenha (bundle)          R$ 15,00 🟢 │  │  1× Espetinho  18,00 │
│  │   Espetinho               R$ 18,00 🟢 │  │                      │
│  │   Day Use Inteira         R$ 60,00 🔴 │  │  ────────────────    │
│  │   ...                                 │  │  TOTAL   R$ 48,00    │
│  └───────────────────────────────────────┘  │                      │
│  [F4 Segurar] [F6 Retomar (2)] [F7 Comanda] │  [F8 → PAGAMENTO]    │
├─────────────────────────────────────────────┴──────────────────────┤
│  ⚡ online · turno #42 · Maria · 3 vendas na fila offline           │
└─────────────────────────────────────────────────────────────────────┘
```

- **Search results list is always visible** (not a dropdown that opens/closes) — the operator's eyes never re-scan a changed layout. Top result is always pre-highlighted.
- **Cart panel is persistent on the right** with running total in the largest type on screen. On the payment step, the same panel flips to the tender/troco view — no navigation, no modal stacking.
- **Status bar** (bottom) permanently shows: connectivity state, open shift, operator, and the offline-queue counter. Offline mode additionally tints the app header amber — the operator must never *discover* they were offline after the fact.

#### Focus contract (non-negotiable)

The search input owns focus at all times except during the payment step. Every action returns focus to it automatically: after add-to-cart, after hold, after restore, after sale completion, after dismissing an error. The operator should be able to run an entire shift without touching the mouse.

#### Keyboard map

| Key | Action |
|---|---|
| type + `Enter` | Add highlighted item, qty 1, keep typing for next item |
| `↑`/`↓` | Move highlight in results |
| `+` / `-` (or `*3` then Enter) | Increment/decrement/set qty of last-added cart line |
| `Del` | Remove selected cart line |
| `F4` | Hold current cart (prompts one-keystroke label, `Enter` accepts auto-label) |
| `F6` | Open held-carts list (shows count in the button label) |
| `F7` | Customer/comanda lookup (CPF, name, QR) |
| `F8` | Go to payment |
| `1`/`2`/`3`/`4` (payment step) | Select Dinheiro / Cartão maquininha / PIX / Cartão MP |
| `Esc` | Back one step (payment → cart), never destroys the cart |

#### Cash & troco UX

On selecting Dinheiro, show quick-tender buttons for the common Brazilian notes ≥ total (`R$ 50`, `R$ 100`, `R$ 200`, plus "Valor exato") *and* a free-entry field. Troco is computed **locally, on every keystroke**, rendered in display-size type with a distinct color — this is pure client arithmetic on the already-known total; it must never wait on any request. `Enter` confirms and completes the sale.

#### Feedback rules

- Every keystroke/click produces a visible response in **< 100ms** (add-to-cart line flash, qty pulse) — perceived speed is a feature requirement, not polish.
- Sale completion = short success toast with the sale number + optional receipt action, cart clears, focus returns to search. The toast must not require dismissal.
- `409` stock conflict = inline red banner **on the offending cart line only**, with the server's available count and one-tap "ajustar para N" — the rest of the cart is untouched (§2.3 step 6).
- Red Light items while offline render with a lock badge and a plain-language reason on hover/tap ("Estoque limitado — indisponível offline"), never a silent no-op.

### 3.5 Performance & Efficiency Engineering

Explicit budgets and the design decisions that meet them — this is where the PDV must beat the current balcão form, not just match it.

| Metric | Budget | How |
|---|---|---|
| Search keystroke → filtered list | < 16ms (one frame) | In-memory index (§3.2), no network, no IndexedDB read on keypress |
| Add item → cart + total updated | < 50ms | **Totals are computed client-side** from cached prices/pricing-rules (see below) |
| Sale completion → ready for next customer | < 500ms online; **0ms perceived** for cash | Optimistic reset: for `CASH`/`EXTERNAL_CARD_TERMINAL`, clear the cart and refocus search the instant the operator confirms; the `POST /sale` runs in the background and, on network failure, falls into the same idempotent sync queue as an offline sale. A rejected sale (`409`) surfaces as a manager-alert exception, not by blocking the line. |
| Catalog sync payload | < 200KB gzipped | Text-only catalog (no image bytes — `image_url`s lazy-load if ever shown); `ETag`/`304` means steady-state refreshes transfer ~0 bytes |
| Cold app start (cache warm) | < 1s to interactive | App shell served/cached by the service worker; catalog read from IndexedDB, not network |

**Kill the per-keystroke pricing round-trip.** The current balcão form fetches `/api/public/simular` on a 400–600ms debounce for every cart change — that latency floor is unacceptable at a counter. Instead, `GET /api/v1/pdv/catalog` ships the venue's pricing inputs (item prices incl. weekday/weekend/holiday tiers, `pricing_rules`, `service_fee_percent` from `ReservaConfig`) and the client computes the display total locally as a mirror of `ReservaPricingService.calculate()`. The server remains the sole authority — `registrar_venda` recalculates and, if the client total diverges (stale catalog), returns a `409`-style divergence for the UI to refresh-and-retry. `/pdv/quote` stays as a low-frequency confirmation call (e.g., on entering the payment step when online), guarded by `AbortController` + a monotonic sequence number so out-of-order responses can never render stale totals (a live bug in the current form — `reserva_nova.html` uses only `clearTimeout`).

**Server-side efficiency notes:**
- Hold the `FOR UPDATE` lock for the minimum span: `registrar_venda` runs `ReservaPricingService.calculate()` and all validation *before* entering the locked stock-check + insert section, so pricing math never executes inside the lock.
- Sync-queue drains are serial per terminal (§4.1) precisely to keep lock contention flat when several terminals reconnect after an outage.
- Verify (don't assume) that the availability aggregate in `_verificar_estoque()` is index-served: check for a composite index covering `sistur_reserva_item_links (resource_id, deleted_at)` and `sistur_reservas (check_in_date, status)`; if missing, add in Phase 1 as an index-only migration — the PDV multiplies the call rate of this exact query.
- `GET /customer-lookup` must be indexed on `customer_document` (already indexed per model) and rate-limited debounced client-side (300ms) — it is the one intentionally online-only search.

---

## 4. Offline Resilience & Conflict Resolution Protocol

### 4.1 Store & Forward sync queue

1. **Enqueue** (client): on a Green Light sale completed while offline (or where the `fetch` to `/api/v1/pdv/sale` fails with a network error), write `{client_sale_uuid, payload, enqueued_at}` to the IndexedDB `pdv_sync_queue` store. `client_sale_uuid` is generated client-side (UUID v4) at this moment — it is the only globally unique handle the offline sale has until the server assigns a `Reserva.id`.
2. **Register** a Background Sync event (`registration.sync.register('pdv-sale-sync')`) via the service worker so the browser retries automatically once connectivity returns, even if the PDV tab isn't focused. Fallback for browsers without Background Sync support (Safari): an `online` event listener plus a periodic `setInterval` retry (e.g., every 30s while `navigator.onLine`).
3. **Sync** (service worker, on `sync` event or fallback): drain the queue in FIFO order (oldest sale first — matters for `_proximo_nome_balcao`-style sequential numbering and for cash-drawer chronology), POST each to `/api/v1/pdv/sale` one at a time (not parallel — avoids the server seeing a burst of concurrent `FOR UPDATE` waits for the same resource from the same terminal).
4. **Server idempotency**: because `client_sale_uuid` is unique (§2.3 step 1), a sale that partially succeeded (e.g., server committed but the response was lost to a flaky connection) will not be double-created on retry — the server detects the existing `PdvSale` row and returns the original result.
5. **Reconcile UI**: once a queued sale syncs successfully, remove it from the queue and clear its "pending sync" badge. If the server rejects it (see §4.2 Red Light enforcement below, or a genuine stock conflict discovered only at sync time — possible for a Yellow-ish case, see note), surface a manager-facing alert (`PdvOfflineSyncLog.outcome = REJECTED_*`) rather than silently dropping the sale; the transaction and cash are already real-world facts by the time this is discovered, so this becomes a reconciliation/accounting task, not a UI error for the original operator.
6. **Encryption at rest is optional hardening, not an MVP blocker**: the first Phase 3 deliverable is a queue that never strands a real sale already paid at the counter. The IndexedDB queue must be readable after F5 while offline, browser close/reopen, OS restart, and service-worker-only sync. Only after those recovery cases pass on the real terminal should AES-GCM be considered.
   - **Do not use volatile keys**: never use a memory-only, shift-scoped, or login-prompt-derived key for queued sales. Any key that disappears on reload can permanently strand cash sales.
   - **If AES-GCM is enabled later**: generate one non-extractable `CryptoKey` per terminal/device and persist the `CryptoKey` object in IndexedDB (`pdv_terminal_key`). The key must survive browser restarts without operator action.
   - **Fail closed, never delete**: if the queue exists but the key is unavailable/corrupt, the UI must show a manager-facing "offline queue locked on this terminal" alert, block destructive cache reset actions, and preserve every queued payload until manual reconciliation. Do not silently drop or overwrite entries.
   - **Operational rule**: encryption must not reduce the guarantee that pending offline sales survive terminal maintenance. The persistent queue counter is the source of truth: any terminal with `N > 0` pending sales is not safe to reset, clear, or reprovision.

### 4.2 Green Light vs. Red Light rules

The traffic-light decision must be enforced **client-side before allowing checkout to proceed to the payment step**, using the cached catalog metadata — not discovered only at sync time. Extend the `/api/v1/pdv/catalog` payload with two new flags per item, computed server-side from existing model fields:

```python
# In the catalog serializer:
"pdv_offline_allowed": item.category.name not in LIMITED_RESOURCE_CATEGORIES
                        and (item.resource is None or item.resource.stock_quantity is None or item.resource.stock_quantity > SAFE_OFFLINE_THRESHOLD),
```

Concretely:

| | Rule | Enforcement point |
|---|---|---|
| 🟢 **Green** | Item's linked `ReservaResource.stock_quantity IS NULL` (unlimited — gastronomy, firewood, beverages) **AND** payment method is `CASH` or `EXTERNAL_CARD_TERMINAL` | Cart allows checkout to proceed offline; item is flagged with a green dot in the search results |
| 🔴 **Red — payment method** | `MP_PIX` / `MP_CARD` selected while offline | Payment-method selector **disables** these options entirely when `navigator.onLine === false`, with a tooltip explaining why — never let the operator pick a method that structurally cannot work offline |
| 🔴 **Red — limited resource** | Item's linked `ReservaResource.stock_quantity` is a finite number (Day Use tickets, BBQ grills, camping spots) | While offline, these items show a red/locked badge in search results and **cannot be added to the cart at all**, unless... |
| 🔴→🟡 **Manager override** | Same as above | A manager-role PIN/login unlocks a one-time override per item-add, which is logged (`AuditLog`, module `pdv`, action `overbooking_override_offline`) with the manager's `actor_id` and a mandatory reason field. The sale still queues offline and is subject to the same post-sync stock re-validation as everything else — the override changes the *UI gate*, not the server-side guarantee. |

**Important nuance from the codebase research**: "stock" in SISTUR is *derived*, not a decrementing counter — `ReservaService._verificar_estoque()` sums live `ReservaItemLink` quantities over the overlap window at read/write time (§3 of the research). This means the client's cached "available count" for a Red Light item is a snapshot from the last catalog sync and can be stale by the time an offline sale for it finally reaches the server (someone else may have sold the last unit online in the meantime). The manager-override path above must be documented to staff as "the safety valve for tickets the venue is *sure* haven't sold out" — final authority is always the server's live `FOR UPDATE` check at sync time, which can and will reject an override sale with `409` (surfacing as `PdvOfflineSyncLog.outcome = REJECTED_STOCK`).

---

## 5. Phased Implementation Roadmap

### Phase 1 — Backend & API (no offline, no new UI)

- [x] `app/models/pdv.py`: `PdvShift`, `PdvSale`, `PdvHeldCart`, `PdvOfflineSyncLog` models + migration (`9794af438eb5`)
- [x] `PdvService` (`app/services/pdv_service.py`): `abrir_turno`, `fechar_turno`, `registrar_venda` (idempotent on `client_sale_uuid`), `anexar_comanda`, `relatorio_turno` — zero Flask imports, `AuditService.log()` on every mutation
- [x] `app/blueprints/pdv/routes.py`: endpoints from §2.3 plus support endpoints `/shifts/current`, `/sources`, and held-cart mirror endpoints; RBAC group `pdv: ["operate", "manage_shift", "view_reports"]`
- [x] Confirm `ReservaService.criar()` accepts a `payment_method`-derived initial status of `PAID` synchronously for `CASH`/`EXTERNAL_CARD_TERMINAL` without going through `PENDING` — implemented and documented as a deliberate divergence in `docs/pdv/README.md`
- [x] `MP_PIX`/`MP_CARD` sale flow keeps the Reserva `PENDING`; the PDV payment UI/checkout contract is in place and continues to rely on the existing asynchronous payment confirmation path
- [x] Unit tests (happy path, AuditLog, `ValueError` on invalid input, idempotency-replay returns same result, stock-conflict → `OverbookingError`/409)
- [x] `docs/pdv/README.md` + `docs/api_catalog.md` entries (mandatory per architecture rules before merge)

### Phase 2 — Client-Side Cache & UI (online-only SPA)

- [x] SPA shell (no full-page navigation) mounted at `/reservas/pdv` — single Jinja shell + client-rendered app integrated with existing session auth and sidebar
- [x] Catalog sync on shift-open → IndexedDB (`pdv` db, `catalog` store keyed by `source_id`), in-memory search index (§3.2)
- [x] Cart UI: qty-1 defaults, instant cash-tendered/change calculator, item search with `0ms` client-side filtering
- [x] Keyboard-first operation per §3.4: full hotkey map, focus contract (search input always regains focus), fixed three-zone layout, quick-tender note buttons
- [x] Client-side pricing preview + authoritative `/pdv/quote` with `AbortController` + sequence guard; server remains the source of truth
- [x] Optimistic sale completion for CASH/terminal (perceived 0ms reset; background POST falls into the idempotent queue on network failure)
- [ ] Performance budget verification on the actual counter hardware (<16ms search, <50ms add-to-cart, <1s warm-cache cold start)
- [x] Hold Sale UI backed by `localStorage` (§3.3 state machine) + best-effort `PdvHeldCart` mirror
- [x] Anti-F5 protection: `localStorage` cart mirroring + restore-on-load
- [x] Comanda-append flow: customer lookup (CPF/name/QR) → attach sale to existing `Reserva`
- [x] Shift open/close UI with cash reconciliation report
- [x] Manual render/theming QA for the shipped counter UI; mobile/tablet pass should still be repeated on real staff hardware before broad rollout

### Phase 3 — Recoverable Offline Sync & Security Guardrails

Already present before the dedicated Phase 3 pass:

- [x] Green/Red Light enforcement in the item search and payment-method selector (§4.2), driven by `pdv_offline_allowed` catalog flag
- [x] Server-side idempotent replay handling + `PdvOfflineSyncLog` recording support for offline sync attempts
- [x] Client-side FIFO drain fallback via `online` event + 30s polling while the PDV tab is open

#### Phase 3A — Recoverable sync first

- [x] Service worker registration, Background Sync API integration (with polling fallback for unsupported browsers)
- [x] IndexedDB `sync_queue` store + drain-on-reconnect logic (§4.1), including migration from legacy `localStorage` queue
- [x] Offline queue UI: persistent pending counter, last sync attempt/error, retry-now action, and warning before closing shift with pending sales
- [x] App shell cache for `/reservas/pdv` now also caches its runtime dependencies (Tailwind Play CDN script, Google Fonts CSS + referenced `.woff2`), fixing the root cause of F5-while-offline rendering broken/unstyled (see deviation §8) — `pdv-sw.js` `cache-first` handler on `ASSET_HOSTS`, cache bumped to `pdv-shell-v2`/`pdv-assets-v1` with stale-cache purge on `activate`
- [ ] Queue recovery verification on **real counter hardware/terminals**: pending sales survive F5 while offline, full browser close/reopen, OS restart, and service-worker-only sync — the shell-dependency fix above is verified via Flask test client + code review, not yet via an actual offline browser session on staging hardware

#### Phase 3B — Business reconciliation

- [x] Manager-facing sync inbox: `GET /reservas/pdv/reconciliacao` + `GET /api/v1/pdv/sync-logs`, backed by compact `PdvOfflineSyncLog` snapshots without raw payload exposure
- [x] Backend manager-override guardrail: `POST /api/v1/pdv/manager-override/authorize` validates CPF/senha + `pdv.manage_shift`, `POST /sale` requires `manager_override` for `force_overbooking`, and usage writes a dedicated `AuditLog` with manager/reason
- [x] Offline limited-stock override UX: adding a 🔴 item while offline opens a modal collecting manager CPF + reason (≥10 chars); `S.cart.offlineOverride` unlocks the item for *this cart only* client-side and travels as `offline_override_note` on the queued sale — it does **not** set `force_overbooking` server-side, so `_verificar_estoque()`'s lock still decides at sync time (see deviation §9)
- [x] Server-side test for the stale-cache rejection path: `test_sync_estoque_esgotado_rejeita_e_audita` creates the conflicting sale first (simulating another channel selling the last unit), then proves an `offline_override_note`-carrying sync still raises `OverbookingError` → `REJECTED_STOCK` in `PdvOfflineSyncLog`, with the manager's note preserved in the payload snapshot for reconciliation
- [x] Server-side test for idempotent queue replay: `test_replay_fila_offline_apos_reconexao` resubmits the identical payload 4 times (simulating a service-worker retry storm after a lost 200 response) against a single-unit resource and asserts exactly one `Reserva`/`PdvSale` is ever created, with the trilha showing 1 `ACCEPTED` + 3 `REJECTED_DUPLICATE`
- [ ] Browser-level E2E test: simulate offline queueing of a Green Light cash sale via DevTools network throttling (not just a service-layer call), reconnect, verify single `Reserva`/`PdvSale` created and the queue entry clears from the UI
- [x] Load/concurrency test: `tests/harness/run_concurrency_pdv.py` — standalone harness (real Postgres required; `FOR UPDATE` doesn't serialize on SQLite) that creates a disposable `sistur_concurrency` database on the staging Postgres server, then fires simultaneous barrier-synchronized threads across all three channels against the same finite resource. Validated on staging: stock=1 vs 8 threads (3 PDV + 3 WEB + 2 BOT) → exactly 1 success / 7 `OverbookingError`; stock=3 vs 12 threads → exactly 3 successes / 9 rejections; blocking-status consumption always == stock (no oversell). Run with: `docker exec -w /app -e PYTHONPATH=/app sistur-teste-sistur-flask-1 python tests/harness/run_concurrency_pdv.py`. **Config-dependent invariant surfaced by this work**: the no-oversell guarantee for the PDV's two-commit flow (criar → PENDING commit → PAID commit) relies on `PENDING.blocks_resource=True` (the seeded default) — if an admin ever unsets it in Status de Reservas, a race window opens between the two commits. Do not make PENDING non-blocking without first making the PDV set PAID inside `criar()`'s own transaction.
- [x] Backend/API for dual-source cash close: physical drawer + PIX change fund, `change_source` on CASH sales, `PENDING_APPROVAL` when counted/expected diverges, supervisor approval endpoint, and migration `2a7c9d0e4f11`
- [x] SPA close-modal UX for dual-source cash close: collect PIX counted value, show cash/PIX divergence detail, collect required discrepancy reason, and surface pending approval status to the operator

#### Phase 3C — Optional encryption hardening

- [ ] Evaluate whether local queue encryption is required after the recoverable queue is stable in production-like terminals
- [ ] If required, implement AES-GCM with a terminal-scoped persisted `CryptoKey`; never use memory-only, shift-scoped, or login-derived keys
- [ ] If key access fails, show a manager alert and preserve the queue for manual reconciliation; never delete encrypted pending sales automatically

---

## 6. Key Risks & Explicit Non-Goals

- **Non-goal:** barcode scanning (explicitly out of scope per requirements).
- **Non-goal:** a second stock-tracking mechanism. The PDV must always resolve to `ReservaService.criar()` / `ReservaService.atualizar()` for anything touching `ReservaItemLink` or `ReservaResource` — duplicating `_verificar_estoque()`'s locking logic anywhere in `pdv_service.py` is a hard blocker at code review.
- **Risk:** the "pay first, create `PAID` directly" pattern for cash/terminal sales is a deliberate divergence from every existing channel's PENDING-then-reconcile pattern. This must be called out prominently in `docs/pdv/README.md` so a future engineer touching `ReservaStatusConfig` or the Active Defense subsystem in `payment_service.py` doesn't assume all `Reserva` rows pass through `PENDING`.
- **Risk:** offline manager-override sales create an unavoidable window where the UI says "allowed" but the server later says "rejected" — this is a business process risk (staff must be trained that override ≠ guarantee), not something further engineering can fully close, given SISTUR's derived (non-counter-column) stock model.

---

## 7. Recommended Improvements After Phase 2

These are not blockers for the current PDV launch, but they should guide the next increments so the project improves operator speed, business result, practicality, and daily experience without adding unnecessary architecture.

### 7.1 Agility / operator speed

- **One-keystroke repeat sale**: add a "repeat last sale" shortcut for common consumable-only carts (e.g., firewood + beverage). This should clone the previous completed cart into a fresh cart, not duplicate the previous `client_sale_uuid`.
- **Top items / favorites row**: pin the 8-12 most sold items for the current venue above search results. Populate from recent `PdvSale` frequency, with a manual override later if needed.
- **Faster tender flow**: when `CASH` is selected and amount tendered equals total, allow `Enter` to confirm immediately. Keep the existing explicit confirm button for safety.
- **Receipt/summary shortcut**: after completion, show a tiny non-blocking "Venda # / Reserva #" toast with copy/print action, then immediately return focus to search.

### 7.2 Business result / reconciliation

- **Shift health panel**: show pending offline count, cash expected, card/PIX totals, and rejected-sync count in the shift close modal. Make unresolved rejected syncs a manager task before final close.
- **Payment method reconciliation export**: add a CSV/export endpoint for shift totals by method and operator. This is higher business value than complex charts in the MVP.
- **Rejected offline sales inbox**: create a simple manager-facing list for `PdvOfflineSyncLog.outcome = REJECTED_*`, with status, reason, payload snapshot, and resolution notes.
- **End-of-day operational checklist**: before closing the terminal, require `offline_queue_count = 0` or manager acknowledgement with reason.

### 7.3 Practicality / implementation risk

- **Split the inline SPA JS only when Phase 3 starts**: the current single-template approach is acceptable for Phase 2, but service worker + IndexedDB queue + crypto/key handling will be easier to test if the PDV client code moves into static JS modules.
- **Introduce a tiny queue adapter**: hide current `localStorage` queue and future IndexedDB queue behind functions like `queueAdd`, `queueListPending`, `queueMarkSynced`, and `queueMarkRejected`. This makes Phase 3A a swap, not a rewrite.
- **Keep AES-GCM behind a feature flag**: default off until terminal recovery tests pass. Security hardening should never be able to strand a paid sale by surprise.
- **Measure before optimizing**: add lightweight `performance.mark()` probes for search, add-to-cart, quote latency, and sale completion. Store only aggregate timings in console/dev logs unless a real telemetry table is needed.

### 7.4 UX / counter experience

- **Offline status should be impossible to miss**: keep the amber status bar, but also add a persistent "N pendentes" badge near the checkout/shift close area.
- **Use direct language for blocked actions**: replace generic errors with operator-actionable copy, e.g. "Sem internet: PIX desativado. Use dinheiro ou maquininha."
- **Make stock semantics visible but compact**: keep the green/limited dots in item cards and add a small legend in the catalog footer. Avoid modal explanations during rush hours.
- **Tablet pass before broader rollout**: verify touch targets, scroll behavior, and keyboard fallback at 375px and a common tablet width. Counter staff may not always use the desktop terminal.
