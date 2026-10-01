# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

# Module: Avisos — Proactive Attendance Monitoring & Internal Notifications

## Overview

**Avisos** (notifications) is a centralized system for detecting and reporting tardiness/absences, managing justified absences, and delivering multi-level alerts to employees and supervisors.

### Key Features

1. **Proactive Tardy Detection** — Background scheduler detects missed clock-ins and applies provisional hour-bank debits
2. **Absence Justification** — Employees/RH can justify absences (folgas, atestados, férias); approval automatically reverses provisional debits
3. **Centralized Notifications** — Single `Aviso` model for all alerts; global notification bell in topbar with unread badge
4. **Supervisor Alerts** — Tardy/absence notifications auto-delivered to area supervisors based on RBAC

### Business Context

- **Problem:** Supervisors had no visibility into tardiness until manually checking logs
- **Solution:** Automated detection + real-time notifications + approval workflow
- **Integration:** Tightly coupled with PontoService (`TimeDay`, `auto_debit_aplicado`) and role-based permissions

---

## Models

### `Aviso` — Notification Record

**Table:** `sistur_avisos`

| Field | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `destinatario_id` | FK → `sistur_funcionarios` (CASCADE) | Recipient employee |
| `remetente_id` | FK → `sistur_funcionarios` (SET NULL) | Sender (NULL = system) |
| `titulo` | String(200) | Short title |
| `mensagem` | Text | Body (nullable) |
| `tipo` | Enum(TipoAviso) | ATRASO, FALTA, or SISTEMA |
| `is_lido` | Boolean | Read/unread flag |
| `criado_em` | DateTime | Creation timestamp (indexed) |

**Relationships:**
- `destinatario` → Funcionario (lazy="select")
- `remetente` → Funcionario (lazy="select")

**Enums:**
- `TipoAviso.ATRASO` — Employee detected late (after 20-min tolerance)
- `TipoAviso.FALTA` — Unexcused absence confirmed at EOD
- `TipoAviso.SISTEMA` — Administrative/system message

### `AusenciaJustificada` — Justified Absence Record

**Table:** `sistur_ausencias_justificadas`

| Field | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `funcionario_id` | FK → `sistur_funcionarios` (CASCADE) | |
| `data` | Date (indexed) | Absence date |
| `tipo` | Enum(TipoAusencia) | FOLGA, ATESTADO, or FERIAS |
| `aprovado` | Boolean | Approval status |
| `criado_por_id` | FK → `sistur_funcionarios` (SET NULL) | RH/admin who created |
| `observacao` | Text | Notes (e.g., "Medical certificate #12345") |
| `criado_em` | DateTime | |

**Constraints:**
- Unique: `(funcionario_id, data)` — one absence per employee per day

**Enums:**
- `TipoAusencia.FOLGA` — Scheduled day off
- `TipoAusencia.ATESTADO` — Medical/legal certificate
- `TipoAusencia.FERIAS` — Vacation/leave

### `Funcionario` Model Extensions

**New column:** `horario_entrada_padrao` (Time, nullable)
- Expected start time for tardiness detection
- NULL = employee excluded from monitoring

---

## Services

### `AvisoService`

**Location:** `app/services/aviso_service.py`

#### CRUD Methods

**`criar(destinatario_id, titulo, mensagem, tipo, remetente_id=None, ator_id=None) → Aviso`**
- Creates internal notification
- Validates recipient is active
- Logs audit event (AuditService.log_create)

**`marcar_lido(aviso_id, ator_id) → Aviso`**
- Marks notification as read
- Idempotent (already-read returns silently)
- Logs audit update

**`marcar_todos_lidos(funcionario_id, ator_id) → int`**
- Bulk marks all unread notifications for employee
- Returns count of marked items
- Logs single audit event with count

**`deletar(aviso_id, ator_id) → None`**
- Removes notification (IDOR guard required in blueprint)
- Logs audit delete

**`contar_nao_lidos(funcionario_id) → int`**
- Counts unread notifications (used by context processor)
- No audit logging (read-only)

**`listar(funcionario_id, page=1, per_page=20) → Pagination`**
- Paginated list (most recent first)
- Used by `/avisos/` page

**`encerrar_por_referencia(referencia_tipo, referencia_id, ator_id=None, desfecho_titulo=None, desfecho_mensagem=None, excluir_destinatarios=None) → int`** (2.11)
- Closes the sibling `Aviso` rows of a multi-approver request once one approver resolves it —
  e.g. an `AjustePontoRequest` notified to 3 supervisors; the first to act should silence it
  for the other two instead of leaving stale pending notifications.
- Marks all matching `is_lido=False` avisos as read; optionally sends each remaining
  recipient a `SISTEMA` "resolved by X" follow-up (skipped for `excluir_destinatarios`, e.g.
  the requester who already gets a personal outcome notice elsewhere).
- Returns count of avisos closed.

**`verificar_integridade_banco_horas() → dict`** (2.9c)
- **Trigger:** APScheduler daily.
- Calls `RHService.divergencias_banco_horas()` and, for each drifted employee, notifies the
  area's `avisos.receber_alertas` supervisors **plus all super admins** — a cross-cutting drift
  problem, so an area-only lookup would leave nobody able to act in some cases. The affected
  employee is never notified (they aren't the one who fixes it).
- **Anti-spam:** skips recipients who already have an unread aviso with the same title for that
  employee — the alert clears itself once the divergence is corrected, no separate resolution step.
- Returns `{"divergencias": int, "avisos_criados": int, "erros": list[str]}`.
- See "Integridade do Banco de Horas" in `docs/rh/README.md` for the full detector→corrector
  chain this job closes.

#### Background Jobs

**`verificar_atrasos() → dict`**
- **Trigger:** APScheduler every 5 minutes
- **Logic:**
  1. Reads timezone from `ConfiguracaoService.get("scheduler.timezone", "America/Sao_Paulo")`
  2. For each active employee with `horario_entrada_padrao` set:
     - Skip if today is holiday (via `GlobalEvent` + recurrence check)
     - Skip if `jornada_semanal[dia_semana].ativo == False`
     - Skip if still within 20-min tolerance window
     - Skip if approved `AusenciaJustificada` exists for today
     - Skip if employee already punched today (TimeEntry exists)
     - Skip if `auto_debit_aplicado=True` already set (multi-worker guard)
  3. Apply provisional debit:
     - Create/update TimeDay with `saldo_final_minutos = -expected_minutes`
     - Set `auto_debit_aplicado = True`
     - Update `Funcionario.banco_horas_acumulado` via delta
     - Log audit event
  4. Create notifications:
     - Aviso(ATRASO) to employee
     - Aviso(ATRASO) to each supervisor in employee's area (via `_buscar_supervisores()`)
- **Returns:** `{"processados": int, "erros": list[str]}`

**`finalizar_ausencias() → dict`**
- **Trigger:** APScheduler daily at 23:00 (11 PM)
- **Logic:**
  1. Find all TimeDay with `auto_debit_aplicado=True` for today
  2. For each:
     - Check if employee punched (TimeEntry exists for today)
     - If no punch: confirm as permanent absence, create Aviso(FALTA)
     - Notify employee + supervisors
- **Returns:** `{"confirmadas": int, "erros": list[str]}`

#### Helper Functions (Module-Private)

**`_criar_aviso_sem_commit(destinatario_id, titulo, mensagem, tipo) → None`**
- Adds Aviso to session without commit
- Used by scheduler jobs to batch-commit multiple notifications
- Logs audit create event per aviso

**`_buscar_supervisores(area_id, excluir_id) → list[int]`**
- Returns IDs of active employees in area who have:
  - `role.is_super_admin == True` OR
  - `RolePermission(role_id, "avisos", "receber_alertas")`
- Excludes `excluir_id` (employee who triggered alert)

### `AusenciaService`

**Location:** `app/services/ausencia_service.py`

**`criar(funcionario_id, data, tipo, criado_por_id, observacao=None, aprovado=False, ator_id=None) → AusenciaJustificada`**
- Creates justified absence record
- Enforces uniqueness: `(funcionario_id, data)`
- Validates employee exists and is active
- Logs audit create

**`aprovar(ausencia_id, ator_id) → AusenciaJustificada`**
- Approves pending absence
- **Critical:** If `TimeDay(shift_date=ausencia.data, auto_debit_aplicado=True)` exists:
  - Resets `saldo_final_minutos = 0`
  - Sets `auto_debit_aplicado = False`
  - Recalculates `Funcionario.banco_horas_acumulado` via delta
  - Logs separate audit event for ponto reversal
- Idempotent (already-approved returns silently)
- Logs audit update for ausencia approval

**`deletar(ausencia_id, ator_id) → None`**
- Prevents deletion if `aprovado=True` (raises ValueError)
- Logs audit delete

**`listar(funcionario_id, page=1, per_page=20) → Pagination`**
- Paginated list (most recent first)
- Used for administrative views

---

## Blueprint Routes

**Location:** `app/blueprints/avisos/routes.py`
**Prefix:** `/avisos`

### `GET /avisos/`

List notifications for current user — **grew into a 3-tab approval center**, not just a
notification feed.

**Auth:** `@login_required`
**Query Params:** `page` (default: 1, applies to the `notificacoes` tab), `tab` (`notificacoes`
default | `aprovacoes` | `minhas`)
**Response:** `200 text/html` — `avisos/index.html`

**Tabs:**
- `notificacoes` — the original personal `Aviso` feed, paginated.
- `aprovacoes` — pending items this user can act on: `AjustePontoRequest` (if
  `ajuste_ponto.aprovar`) and pending PDV shift closings with cash discrepancy (if
  `pdv.manage_shift`). Both permissions are optional and independent — a user may see one,
  both, or neither section.
- `minhas` — the current user's own `AjustePontoRequest` history (`AjustePontoService.listar_por_funcionario`), regardless of approval permission.

**Context variables:** `avisos` (Pagination), `destinos` (dict resolving each aviso's link
target), `page`, `tab`, `tem_permissao_aprovar`/`tem_permissao_ajuste`/`tem_permissao_pdv`,
`ajustes_pendentes`, `pdv_turnos_pendentes`, `minhas_solicitacoes`.

### `POST /avisos/ajuste/<ajuste_id>/aprovar`

Approves a `AjustePontoRequest` — requires `ajuste_ponto.aprovar`. Delegates to
`AjustePontoService.aprovar(ajuste_id, supervisor_id=fid)`, which creates/edits the
`TimeEntry` and reprocesses the `TimeDay`. See `docs/ponto/README.md`.

### `POST /avisos/ajuste/<ajuste_id>/rejeitar`

Rejects with a required `motivo_rejeicao` form field (blocked in the route before hitting the
service if empty). Requires `ajuste_ponto.aprovar`.

### `POST /avisos/pdv/turno/<shift_id>/aprovar`

Approves a PDV shift closing that had a cash discrepancy — requires `pdv.manage_shift`.
Delegates to `PdvService.aprovar_fechamento(shift_id, aprovador_id, ator_id)`. See
`docs/pdv/README.md`.

### `POST /avisos/<id>/marcar-lido`

Mark single notification as read.

**Auth:** `@login_required`
**Method:** POST (form or AJAX)
**Security:** IDOR guard — verifies `aviso.destinatario_id == current_user`
**Response:** Redirect to `/avisos/` with flash message

---

### `POST /avisos/marcar-todos-lidos`

Mark all notifications as read.

**Auth:** `@login_required`
**Method:** POST
**Response:** Redirect to `/avisos/` with flash message

---

### `POST /avisos/<id>/deletar`

Delete notification.

**Auth:** `@login_required`
**Method:** POST
**Security:** IDOR guard — verifies ownership
**Response:** Redirect to `/avisos/` with flash message

---

## Templates

### `_avisos_bell.html` (Reusable Include)

Notification bell icon for topbar, included in:
- `portal/dashboard.html`
- `ponto/index.html`
- `ponto/analise.html`
- `rh/index.html`

**Features:**
- Bell SVG icon (20×20, stroke-based)
- Red badge showing unread count (99+ max)
- Links to `/avisos/`
- Inline styles for portability
- 44px touch target

**Variables needed:**
- `avisos_nao_lidos` (int) — injected by context processor

### `avisos/index.html` (Notification Page)

Full notification management page.

**Features:**
- Sticky topbar with back link + title + bell icon
- Flash message area (sucesso, erro, aviso)
- "Mark all as read" button (visible if unread > 0)
- Notification cards:
  - Left border color by type (amber=ATRASO, red=FALTA, blue=SISTEMA)
  - Unread indicator dot
  - Type badge
  - Timestamp
  - "Mark read" + "Delete" buttons (POST forms, AJAX fallback)
- Empty state: "Nenhum aviso por enquanto."
- Pagination (prev/next) if > 20 items
- Mobile-first responsive (full-width at 375px)
- All buttons ≥44px

---

## Context Processor

**Location:** `app/__init__.py`

**Function:** `inject_avisos()`

Injects `avisos_nao_lidos` (int) into every template.

**Implementation:**
```python
@app.context_processor
def inject_avisos():
    try:
        from flask import session
        fid = session.get("funcionario_id")
        if fid:
            from app.services.aviso_service import AvisoService
            return {"avisos_nao_lidos": AvisoService.contar_nao_lidos(fid)}
    except Exception:
        pass
    return {"avisos_nao_lidos": 0}
```

- Called on **every request**
- Fails gracefully (returns 0 on error)
- Allows templates to reference `{{ avisos_nao_lidos }}` without explicit passing

---

## Background Scheduler

**Location:** `app/__init__.py`

**Framework:** Flask-APScheduler

**Configuration:**
```python
# In app/config.py (all non-test configs):
SCHEDULER_API_ENABLED = False
SCHEDULER_EXECUTORS = {"default": {"type": "threadpool", "max_workers": 1}}
SCHEDULER_JOB_DEFAULTS = {"coalesce": True, "max_instances": 1}
```

**Jobs:**

| Job ID | Type | Frequency | Handler |
|---|---|---|---|
| `verificar_atrasos` | interval | 5 min | `AvisoService.verificar_atrasos()` |
| `finalizar_ausencias` | cron | 23:00 daily | `AvisoService.finalizar_ausencias()` |

**Key Design Decisions:**
- `max_instances=1` + `coalesce=True` prevent duplicate execution in Gunicorn multi-worker environments
- `misfire_grace_time=60` (atrasos) / `300` (ausencias) allows scheduling tolerance
- Try/except ImportError in `app/__init__.py` allows graceful degradation if Flask-APScheduler not installed locally

---

## Database Migration

**Migration ID:** `a1b2c3d4e5f6`
**File:** `migrations/versions/a1b2c3d4e5f6_add_avisos_ausencias_horario_autodebit.py`

**Changes:**
1. Create `sistur_avisos` table (+ 3 indexes)
2. Create `sistur_ausencias_justificadas` table (+ 2 indexes + unique constraint)
3. Add `horario_entrada_padrao` column to `sistur_funcionarios`
4. Add `auto_debit_aplicado` column to `sistur_time_days`

**VPS Deployment:**
```bash
docker exec -it sistur-flask-app flask db upgrade
```

---

## Permissions (RBAC)

**New permission:** `avisos: ["view", "receber_alertas"]`

| Permission | Meaning |
|---|---|
| `avisos.view` | Can access `/avisos/` page (implicit for all employees) |
| `avisos.receber_alertas` | Receives supervisor alerts for tardiness/absences in own area |

**Assignment Logic:**
- Supervisors/managers: manually assigned `avisos.receber_alertas` via `/admin/configuracoes/roles/<id>/permissoes`
- Super admins: always receive alerts (implicit)

---

## Integration Points

### With `PontoService`

**`processar_dia()` method:**
- If `TimeDay.auto_debit_aplicado == True` AND `len(entries) > 0`:
  - Reset `auto_debit_aplicado = False`
  - Existing delta mechanism handles `banco_horas_acumulado` reversal

**`_get_expected_minutes(funcionario, shift_date)` function:**
- Called by `verificar_atrasos()` to calculate expected daily minutes
- Respects `jornada_semanal` overrides + `minutos_esperados_dia` fallback

### With `GlobalEvent` (Calendario)

**Holiday Detection:**
- `verificar_atrasos()` queries:
  ```python
  GlobalEvent.afeta_folha == True AND (
      GlobalEvent.data_evento == today OR (
          GlobalEvent.recorrente_anual == True AND
          extract("month", data_evento) == today.month AND
          extract("day", data_evento) == today.day
      )
  )
  ```

### With `AuditService`

**All mutations logged:**
- `criar()` → `AuditService.log_create("avisos", ...)`
- `marcar_lido()` → `AuditService.log_update("avisos", ...)`
- `marcar_todos_lidos()` → `AuditService.log_update("avisos", ...)` with count
- `deletar()` → `AuditService.log_delete("avisos", ...)`

---

## Testing

**Test file:** `tests/services/test_aviso_service.py`

**Coverage:**

| Test | Scenario |
|---|---|
| `test_criar_aviso_dispara_audit` | criar() → 1 AuditLog(create) |
| `test_criar_aviso_destinatario_inativo_levanta_erro` | criar() with inactive employee → ValueError |
| `test_marcar_todos_lidos_zera_contador` | contar_nao_lidos() == 0 after marcar_todos_lidos() |
| `test_cria_aviso_atraso_sem_batida` | verificar_atrasos() → Aviso(ATRASO) + TimeDay.auto_debit |
| `test_ignora_ausencia_justificada_aprovada` | verificar_atrasos() skips if approved absence exists |
| `test_ignora_quem_ja_bateu_ponto` | verificar_atrasos() skips if TimeEntry exists |
| `test_processar_dia_reverte_auto_debit` | processar_dia() resets auto_debit when punches arrive |
| `test_aprovar_ausencia_reverte_debito_provisorio` | AusenciaService.aprovar() reverses debit + resets balance |

**All tests passing:** ✅ (8/8)

---

## Edge Cases & Design Decisions

### Provisional Debit Reversal

**Problem:** When employee clocks in late, we apply a provisional debit immediately (for visibility). But if they later submit valid punches, the debit must be reversed.

**Solution:** Two mechanisms:
1. **Immediate reversal:** `processar_dia()` resets `auto_debit_aplicado=False` when real punches exist
2. **Approval reversal:** `AusenciaService.aprovar()` also reverses debit if approved absence exists

**Why delta handles it:** The existing `banco_horas_acumulado += (saldo_final - old_saldo)` arithmetic in `processar_dia()` naturally corrects the balance without extra logic.

### Multi-Worker Safety

**Problem:** In Gunicorn with N workers, APScheduler job fires N times simultaneously → duplicated notifications.

**Solutions:**
1. APScheduler config: `max_instances=1`, `coalesce=True`
2. Code guard: `if time_day.auto_debit_aplicado: continue` (skip already-debited days)

### Timezone Awareness

**Problem:** Servers run UTC; business operates in São Paulo timezone (UTC-3/-2).

**Solution:**
```python
tz_str = ConfiguracaoService.get("scheduler.timezone", "America/Sao_Paulo")
tz = ZoneInfo(tz_str)
agora_local = datetime.now(tz)
```

Runtime-configurable via SystemSetting. Defaults to America/Sao_Paulo.

---

## Deployment Checklist

- [ ] MySQL migrations applied: `flask db upgrade`
- [ ] Flask-APScheduler installed: `pip install Flask-APScheduler==1.13.1`
- [ ] Scheduler config present in `app/config.py`
- [ ] APScheduler initializes without error (check logs)
- [ ] Context processor `inject_avisos()` working (check template)
- [ ] New routes registered under `/avisos` blueprint
- [ ] Bell icon visible in topbars (portal, ponto, rh)
- [ ] `/avisos/` page loads and pagination works
- [ ] RBAC permission `avisos.receber_alertas` available in role editor
- [ ] Test suite passes: `pytest tests/services/test_aviso_service.py -v`

