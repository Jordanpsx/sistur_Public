# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

# API Catalog — SISTUR Portal do Colaborador

> **Source of truth** for all HTTP endpoints.
> Update this file whenever a route is added, changed, or removed.

---

## Conventions

| Item | Convention |
|---|---|
| Base URL | `/` (served via Nginx reverse proxy on port 80/443) |
| Content-Type | `application/x-www-form-urlencoded` for form POSTs; `application/json` for API responses |
| Auth | Session cookie (`funcionario_id` in server-side session) |
| Error format | Flash message (HTML pages) or `{"erro": "message"}` (JSON endpoints) |
| Timestamps | ISO 8601, UTC (`2026-02-24T18:30:00Z`) |

---

## Custom Error Codes

| Code | HTTP Status | Meaning |
|---|---|---|
| `CPF_INVALIDO` | 400 | CPF failed check-digit validation |
| `CPF_NAO_ENCONTRADO` | 401 | CPF not in DB or employee inactive |
| `SESSAO_EXPIRADA` | 401 | Session cookie expired or missing |
| `NAO_AUTORIZADO` | 403 | Authenticated but lacks permission for this resource |
| `NAO_ENCONTRADO` | 404 | Requested resource does not exist |
| `CPF_DUPLICADO` | 409 | CPF already registered |

---

## Module: Auth (`/portal`)

### `GET /portal/login`

Renders the login page.

**Auth required:** No
**Redirect:** If already authenticated → `GET /portal/dashboard`

**Response:** `200 text/html` — `auth/login.html`

---

### `POST /portal/login`

Authenticate via CPF.

**Auth required:** No
**Content-Type:** `application/x-www-form-urlencoded`

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `cpf` | string | Yes | CPF in any format (`000.000.000-00` or `00000000000`) |

**Success response:** `302` → `GET /portal/dashboard`

**Error responses:**

| Scenario | Status | Flash category | Message |
|---|---|---|---|
| CPF format invalid | 400 | `erro` | "CPF inválido." |
| CPF not found / inactive | 401 | `erro` | "CPF não encontrado ou funcionário inativo." |

**Audit:** `AuditLog(action=login, module=auth, new_state={"success": bool})`

---

### `POST /portal/logout`

End the authenticated session.

**Auth required:** No (safe to call even unauthenticated)
**Response:** `302` → `GET /portal/login`

**Audit:** `AuditLog(action=logout, module=auth)` — only if session was active

---

## Module: Portal (`/portal`)

### `GET /portal/dashboard`

Employee dashboard page.

**Auth required:** Yes → redirects to `/portal/login` if unauthenticated
**Response:** `200 text/html` — `portal/dashboard.html`

**Template variables:**

| Variable | Type | Description |
|---|---|---|
| `funcionario` | `Funcionario` | Authenticated employee ORM object |
| `company_name` | string | From `COMPANY_NAME` env var (via context processor) |
| `company_logo` | string | From `COMPANY_LOGO` env var (empty = SVG fallback) |

---

## Module: Funcionários (planned — `/admin/funcionarios`)

> Not yet implemented. Planned for next sprint.

| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/funcionarios` | List all active employees |
| `POST` | `/admin/funcionarios` | Create employee (calls `FuncionarioService.criar`) |
| `GET` | `/admin/funcionarios/<id>` | Employee detail |
| `POST` | `/admin/funcionarios/<id>/editar` | Update employee (calls `FuncionarioService.atualizar`) |
| `POST` | `/admin/funcionarios/<id>/desativar` | Soft-delete (calls `FuncionarioService.desativar`) |

---

## Module: Banco de Horas (planned — `/portal/banco-horas`)

> Not yet implemented. Blocked by Ponto Eletrônico port.

| Method | Path | Description |
|---|---|---|
| `GET` | `/portal/banco-horas` | Employee's current balance and history |

---

## Module: Ponto Eletrônico (`/ponto`)

### `GET /ponto/`

Exibe o histórico mensal de batidas do colaborador autenticado.

**Auth required:** Yes → redirects to `/portal/login`
**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `mes` | int | mês corrente | Mês a exibir (1–12) |
| `ano` | int | ano corrente | Ano a exibir |

**Response:** `200 text/html` — `ponto/index.html`

**Template variables:** `funcionario`, `days` (list[TimeDay]), `entries_by_day` (dict), `mes`, `ano`, `hoje`

---

### `POST /ponto/registrar`

Registra uma nova batida de ponto para o colaborador autenticado.

**Auth required:** Yes
**Content-Type:** `application/x-www-form-urlencoded`

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `punch_time` | string (ISO 8601) | No | Timestamp local do cliente. Se ausente, usa o servidor. |
| `lat` | float | Conditional | Latitude GPS (graus decimais). **Obrigatório** se existirem zonas de geofence ativas. |
| `lng` | float | Conditional | Longitude GPS (graus decimais). **Obrigatório** se existirem zonas de geofence ativas. |

**Success response:** `302` → `GET /ponto/`

**Error responses:**

| Scenario | Flash category | Message |
|---|---|---|
| Batida duplicada (< 5s) | `erro` | "Batida duplicada detectada. Aguarde N segundos…" |
| Funcionário inativo | `erro` | "Funcionário X não encontrado ou inativo." |

**Audit:** `AuditLog(action=create, module='ponto', entity_id=TimeEntry.id)`

---

## Module: Admin — Audit Log Viewer (`/admin`)

### `GET /admin/audit-logs`

Visualizador de logs de auditoria com filtros dinâmicos e paginação server-side.

**Auth required:** Yes → `302` to `/portal/login` if unauthenticated  
**Permission required:** `audit.view` OR `audit.view_all` (super_admin bypasses automatically)

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `user_id` | int | — | Filtra pelo ID do ator (Funcionario) |
| `module` | str | — | Filtra pelo módulo (e.g. `auth`, `ponto`, `funcionarios`) |
| `page` | int | 1 | Número da página (50 registros por página) |

**Access levels:**

| Role | Permissão | Acesso |
|---|---|---|
| `super_admin` | `is_super_admin=True` | Todos os logs |
| Gerente | `audit.view_all` | Todos os logs |
| Supervisor | `audit.view` | Logs filtrados pelo `area_id` do Funcionario |
| Sem permissão | — | HTTP 403 |

**Response:** `200 text/html` — `admin/audit_logs.html`

**Template variables:**

| Variable | Type | Description |
|---|---|---|
| `pagination` | `Pagination` | Objeto Flask-SQLAlchemy com `.items`, `.total`, `.pages` |
| `logs` | `list[AuditLog]` | Registros da página atual |
| `modulos` | `list[str]` | Módulos distintos disponíveis para filtro |
| `funcionarios_map` | `dict[int, str]` | Mapa id → nome dos atores |
| `filtro_user_id` | int? | Filtro ativo de ator |
| `filtro_module` | str? | Filtro ativo de módulo |
| `acesso_total` | bool | True se gerente/super_admin |
| `ator` | `Funcionario` | Funcionário autenticado |

---

## Module: Configurações (`/admin/configuracoes`)

> **Access:** Configurações globais são exclusivas para `Role.is_super_admin = True`.
> O editor de permissões de roles também aceita supervisores delegados por domínio.

### `GET /admin/configuracoes/`

Exibe o painel de controle com quatro abas: Módulos Ativos, Regras Globais, Identidade Visual, Dados da Empresa.

**Auth required:** Yes (super_admin)
**Response:** `200 text/html` — `admin/configuracoes/index.html`

**Template variables:**

| Variable | Type | Description |
|---|---|---|
| `modulos_info` | `list[dict]` | Lista de módulos com chave, nome, label, ativo |
| `empresa_nome` | str | Nome principal do branding (do banco ou "") |
| `empresa_logo` | str | URL do logo (do banco ou "") |
| `empresa_razao_social` | str | Razão Social da empresa (do banco ou "") |
| `empresa_cnpj` | str | CNPJ da empresa (do banco ou "") |
| `empresa_endereco` | str | Endereço completo (do banco ou "") |

---

### `POST /admin/configuracoes/modulos/<chave>/toggle`

Inverte o estado ativo/inativo de um módulo via AJAX.

**Auth required:** Yes (super_admin)
**Path param:** `chave` — deve ser `modulo.ponto` ou `modulo.reservas`
**Response:** `200 application/json` `{"ativo": bool, "chave": str}`
**Error:** `400 application/json` `{"erro": "Chave inválida."}` se a chave não for válida

---

### `POST /admin/configuracoes/branding`

Salva configurações de identidade visual.

**Auth required:** Yes (super_admin)
**Form fields:** `empresa_nome` (str), `empresa_logo` (str URL)
**Response:** Redirect `GET /admin/configuracoes/` com flash de sucesso

---

### `GET /admin/configuracoes/roles`

Lista roles do sistema.

**Auth required:** Yes (`super_admin` ou supervisor delegado)
**Response:** `200 text/html` — `admin/configuracoes/roles.html`

**Super admin:** vê todos os roles ativos e inativos, com criação/desativação disponíveis.

**Supervisor delegado:** vê roles comuns administráveis e pode abrir o editor apenas para alterar
permissões do domínio delegado. Não vê formulário de criação nem ação de desativação.

---

### `POST /admin/configuracoes/roles/criar`

Cria um novo role.

**Auth required:** Yes (super_admin)
**Form fields:** `nome` (str, snake_case), `descricao` (str), `is_super_admin` (checkbox)
**Response:** Redirect `GET /admin/configuracoes/roles` com flash de resultado
**Error:** Flash `"erro"` se nome duplicado ou vazio

---

### `GET /admin/configuracoes/roles/<id>/permissoes`

Exibe o editor de permissões de um role específico.

**Auth required:** Yes (`super_admin` ou supervisor delegado com permissão editável no role)
**Path param:** `id` (int) — PK do role
**Response:** `200 text/html` — `admin/configuracoes/role_permissoes.html`
**Error:** HTTP 403 se o ator não puder administrar nenhuma permissão do role; HTTP 404 se role não encontrado

**Template variables:**

| Variable | Type | Description |
|---|---|---|
| `role` | `Role` | Instância do role a editar |
| `permissoes_sistema` | `dict[str, list[str]]` | Mapa módulo → lista de ações |
| `perms_atuais` | `set[tuple[str,str]]` | Pares (modulo, acao) atualmente concedidos |
| `labels_modulo` | `dict[str,str]` | Nomes legíveis por módulo |
| `labels_acao` | `dict[str,str]` | Nomes legíveis por ação |
| `is_delegated_editor` | `bool` | True quando o ator não é super_admin |

**Permission source:** `app/core/permission_catalog.py`. Para supervisores, a lista é filtrada por
`RoleDelegationPolicy`.

---

### `POST /admin/configuracoes/roles/<id>/permissoes`

Salva permissões de um role por diff.

**Auth required:** Yes (`super_admin` ou supervisor delegado com permissão editável no role)
**Path param:** `id` (int)
**Form fields:** Checkboxes `perm_<modulo>_<acao>` = "on" para cada permissão habilitada
**Response:** Redirect `GET /admin/configuracoes/roles/<id>/permissoes` com flash de sucesso

**Delegated mode:** supervisores de domínio alteram somente permissões delegáveis do domínio que
administram. O POST preserva permissões fora do escopo do supervisor, bloqueia edição de
`super_admin`, bloqueia autoedição e impede conceder permissões que o supervisor não possui.

---

### `POST /admin/configuracoes/roles/<id>/desativar`

Desativa um role (soft-delete).

**Auth required:** Yes (super_admin)
**Path param:** `id` (int)
**Response:** Redirect `GET /admin/configuracoes/roles` com flash de resultado
**Error:** Flash `"erro"` se o role for `is_super_admin=True` (proteção contra lockout)

---

### `POST /admin/configuracoes/dados-empresa`

Salva dados cadastrais da empresa para uso no cabeçalho do PDF da Folha de Ponto.

**Auth required:** Yes (super_admin)
**Form fields:** `empresa_razao_social` (str), `empresa_cnpj` (str), `empresa_endereco` (str)
**Response:** Redirect `GET /admin/configuracoes/` com flash de sucesso
**Storage:** `sistur_system_settings` — chaves `empresa.razao_social`, `empresa.cnpj` e `empresa.endereco`
**Audit:** `AuditLog` via `ConfiguracaoService.set`

### `GET /admin/configuracoes/segredos`

Cofre de segredos/integrações: lista cada integração do catálogo com o estado
(configurado ou não) e apenas a máscara do valor — o valor bruto nunca é exibido.
Hoje o catálogo tem uma entrada: `mercadopago.access_token`.

**Auth required:** Sessão ativa + `Role.is_super_admin = True`

### `POST /admin/configuracoes/segredos/salvar`

Grava ou atualiza um segredo, criptografado com a chave mestra `SISTUR_SECRETS_KEY`.

**Form fields:** `key` (str, deve pertencer ao catálogo), `value` (str, obrigatório).

**Resposta:** Redirect para a tela de segredos com flash. Chave fora do catálogo ou
valor vazio são recusados.

### `POST /admin/configuracoes/segredos/<key>/remover`

Desativa um segredo do catálogo (idempotente). Redirect com flash.

---

## Module: Avisos — Proactive Absence Monitoring (`/avisos`)

> **Access:** Todas as rotas requerem autenticação (`@login_required`).
> Supervisores recebem notificações via RBAC: permissão `avisos.receber_alertas`.
> Ver documentação completa em `docs/avisos/README.md`.

### `GET /avisos/`

Lista notificações do usuário autenticado com paginação.

**Auth required:** Yes
**Query params:** `page` (int, default: 1)
**Response:** `200 text/html` — `avisos/index.html`

**Template variables:**

| Variable | Type | Description |
|---|---|---|
| `pagination` | `Pagination` | Flask-SQLAlchemy pagination object |
| `avisos` | `list[Aviso]` | Avisos para a página atual |
| `avisos_nao_lidos` | int | Contagem total de não lidos (injected by context processor) |

**Features:**
- Cards with color-coded left border by tipo (amber=ATRASO, red=FALTA, blue=SISTEMA)
- Unread indicator dot
- "Mark as read" + "Delete" buttons per card
- "Mark all as read" button (visible if unread > 0)
- Empty state: "Nenhum aviso por enquanto."
- Pagination (prev/next)
- Mobile-first responsive design

---

### `POST /avisos/<id>/marcar-lido`

Marca uma notificação como lida.

**Auth required:** Yes
**Path param:** `id` (int) — PK do Aviso
**Security:** IDOR guard — verifica `aviso.destinatario_id == current_user`
**Form/AJAX:** Both supported (form fallback)
**Response (Form):** Redirect `GET /avisos/` com flash de sucesso
**Response (AJAX):** `200 application/json` `{"status": "success", "marcados": 1}`
**Error:** HTTP 403 se IDOR falhar; HTTP 404 se aviso não encontrado

---

### `POST /avisos/marcar-todos-lidos`

Marca todas as notificações não lidas como lidas.

**Auth required:** Yes
**Form fields:** None required
**Response:** Redirect `GET /avisos/` com flash `"Todas as notificações marcadas como lidas."`
**Audit:** `AuditLog` via `AvisoService.marcar_todos_lidos` (logs count)

---

### `POST /avisos/<id>/deletar`

Remove uma notificação.

**Auth required:** Yes
**Path param:** `id` (int) — PK do Aviso
**Security:** IDOR guard — verifica `aviso.destinatario_id == current_user`
**Response:** Redirect `GET /avisos/` com flash de sucesso
**Audit:** `AuditLog` via `AvisoService.deletar`
**Error:** HTTP 403 se IDOR falhar; HTTP 404 se aviso não encontrado

---

## Background Scheduler Jobs

> Jobs executados via **Flask-APScheduler**. Configuração em `app/config.py`.

### Job: `verificar_atrasos` (Tardy Detection)

**Trigger:** Interval — every 5 minutes
**Handler:** `AvisoService.verificar_atrasos()`

**Logic:**
1. Reads timezone from `ConfiguracaoService.get("scheduler.timezone", "America/Sao_Paulo")`
2. Converts UTC now to local time
3. For each active employee with `horario_entrada_padrao` set:
   - Skip if today is holiday (via `GlobalEvent.afeta_folha`)
   - Skip if day inactive in `jornada_semanal`
   - Skip if within 20-min tolerance window (entrada_padrao + 20 min)
   - Skip if approved `AusenciaJustificada` exists for today
   - Skip if `TimeEntry` exists (already punched)
   - Skip if `TimeDay.auto_debit_aplicado=True` (guard against duplicates)
4. Apply provisional debit:
   - Create/update `TimeDay` with `saldo_final_minutos = -expected`
   - Set `auto_debit_aplicado = True`
   - Adjust `banco_horas_acumulado` via delta
   - Log audit event
5. Create notifications:
   - `Aviso(tipo=ATRASO)` to employee
   - `Aviso(tipo=ATRASO)` to each supervisor in employee's area (RBAC: `avisos.receber_alertas` or super_admin)

**Error handling:** try/except per employee — isolated failures

**Returns:** `{"processados": int, "erros": list[str]}`

---

### Job: `finalizar_ausencias` (Absence Confirmation)

**Trigger:** Cron — daily at 23:00 (11 PM)
**Handler:** `AvisoService.finalizar_ausencias()`

**Logic:**
1. Find all `TimeDay` with `auto_debit_aplicado=True` for today
2. For each:
   - Check if `TimeEntry` exists (real punches)
   - If no punches: confirm as permanent absence
     - Create `Aviso(tipo=FALTA)` to employee
     - Create `Aviso(tipo=FALTA)` to each supervisor in area
     - Log audit events

**Returns:** `{"confirmadas": int, "erros": list[str]}`

---

### Job: `integridade_banco_horas` (Hour-Bank Drift Detection — 2.9c)

**Trigger:** Cron — daily at 03:30
**Handler:** `AvisoService.verificar_integridade_banco_horas()`

`Funcionario.banco_horas_acumulado` is a **materialized sum**: every writer
(`processar_dia`, provisional absence debit, its reversal, deductions) adjusts it
by *delta*. A failure mid-transaction — or a direct DB edit — makes the cache
drift from the truth with no signal. This job is the alarm; the repair
(`PontoService.recalcular_banco_global`) already existed but nobody knew when to run it.

**Logic:**
1. `RHService.divergencias_banco_horas()` checks, in **3 queries total**, the invariant
   `banco_horas_acumulado == Σ TimeDay.saldo_final_minutos − Σ TimeBankDeduction.minutos_abatidos`
2. For each divergent employee, notify the area's supervisors (same
   `avisos.receber_alertas` rule as tardiness alerts) **plus all super admins** — drift is a
   cross-cutting financial issue, so area-scoped recipients alone would miss whoever can act
3. The affected employee is never notified (they are not who fixes it)
4. **Anti-spam:** skips a recipient who already has an **unread** notice about the same
   employee; the alert stops on its own once the divergence is corrected

**Returns:** `{"divergencias": int, "avisos_criados": int, "erros": list[str]}`

**Surfaces:**
- Dashboard panel at `/rh/#integridade-banco` — requires `folha_ponto.view`; rendered
  **only when there is drift** (exception condition, not a routine metric)
- `POST /rh/funcionarios/<id>/recalcular-banco` — requires `ponto.edit_admin`; rebuilds the
  value from the ponto records and redirects back to the panel. Audited by `PontoService`
  as `AuditAction.update` on module `ponto`

---

## Context Processor: `inject_avisos()`

> Injected into every template request via Flask `@app.context_processor`.

**Variable:** `avisos_nao_lidos` (int)

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

**Usage:** Templates can reference `{{ avisos_nao_lidos }}` without explicit passing.

**Topbar bell:** `_avisos_bell.html` include displays red badge with count (99+ max).

---

## Models & Database

### `Aviso` Table

```sql
CREATE TABLE sistur_avisos (
    id INTEGER PRIMARY KEY,
    destinatario_id INTEGER NOT NULL,
    remetente_id INTEGER,
    titulo VARCHAR(200) NOT NULL,
    mensagem TEXT,
    tipo ENUM('ATRASO', 'FALTA', 'SISTEMA') NOT NULL DEFAULT 'SISTEMA',
    is_lido BOOLEAN NOT NULL DEFAULT FALSE,
    criado_em DATETIME NOT NULL,
    FOREIGN KEY (destinatario_id) REFERENCES sistur_funcionarios(id) ON DELETE CASCADE,
    FOREIGN KEY (remetente_id) REFERENCES sistur_funcionarios(id) ON DELETE SET NULL,
    INDEX (destinatario_id),
    INDEX (is_lido),
    INDEX (criado_em)
);
```

### `AusenciaJustificada` Table

```sql
CREATE TABLE sistur_ausencias_justificadas (
    id INTEGER PRIMARY KEY,
    funcionario_id INTEGER NOT NULL,
    data DATE NOT NULL,
    tipo ENUM('FOLGA', 'ATESTADO', 'FERIAS') NOT NULL,
    aprovado BOOLEAN NOT NULL DEFAULT FALSE,
    criado_por_id INTEGER,
    observacao TEXT,
    criado_em DATETIME NOT NULL,
    FOREIGN KEY (funcionario_id) REFERENCES sistur_funcionarios(id) ON DELETE CASCADE,
    FOREIGN KEY (criado_por_id) REFERENCES sistur_funcionarios(id) ON DELETE SET NULL,
    UNIQUE KEY uq_ausencia_funcionario_data (funcionario_id, data),
    INDEX (funcionario_id),
    INDEX (data)
);
```

### Model Extensions

**`Funcionario` column:**
```sql
ALTER TABLE sistur_funcionarios ADD COLUMN horario_entrada_padrao TIME;
```

**`TimeDay` column:**
```sql
ALTER TABLE sistur_time_days ADD COLUMN auto_debit_aplicado BOOLEAN NOT NULL DEFAULT FALSE;
```

---

## RBAC Permissions

### New Permission Group: `avisos`

| Permission | Scope | Meaning |
|---|---|---|
| `avisos.view` | Employee | Access `/avisos/` page (implicit for all active employees) |
| `avisos.receber_alertas` | Supervisor | Receive notifications for tardiness/absences in own area |

**Assignment:** Via `/admin/configuracoes/roles/<id>/permissoes` interface

**Query Logic:** `_buscar_supervisores(area_id)` filters employees with this permission OR `role.is_super_admin=True`

---

## Integration Points

### With `PontoService`

**`processar_dia()` method:**
- When `len(entries) > 0` and `auto_debit_aplicado=True`:
  - Reset `auto_debit_aplicado = False`
  - Existing delta mechanism (`banco_horas_acumulado += delta`) handles reversal
  - No extra arithmetic needed

**`_get_expected_minutes(funcionario, shift_date)` function:**
- Called by `verificar_atrasos()` to calculate expected minutes
- Respects `jornada_semanal` overrides + `minutos_esperados_dia` fallback

### With `CalendarService` (GlobalEvent)

**Holiday Detection:**
```python
GlobalEvent.afeta_folha == True AND (
    GlobalEvent.data_evento == today OR (
        GlobalEvent.recorrente_anual == True AND
        EXTRACT(MONTH FROM data_evento) == today.month AND
        EXTRACT(DAY FROM data_evento) == today.day
    )
)
```

---

## Migration

**File:** `migrations/versions/a1b2c3d4e5f6_add_avisos_ausencias_horario_autodebit.py`

**Changes:**
1. Create `sistur_avisos` table (+ 3 indexes)
2. Create `sistur_ausencias_justificadas` table (+ 2 indexes, unique constraint)
3. Add `horario_entrada_padrao` column to `sistur_funcionarios`
4. Add `auto_debit_aplicado` column to `sistur_time_days` (default=FALSE)

**VPS Deployment:**
```bash
docker exec -it sistur-flask-app flask db upgrade
```

---

## Module: RH — Folha de Ponto (`/rh`)

> **Access:** Requer permissão `folha_ponto.*` por ação. Super admins têm acesso total.
> Ver documentação completa em `docs/rh/README.md`.

### `GET /rh/folha-ponto/<funcionario_id>`

Exibe a Folha de Ponto CLT de 10 colunas do funcionário para o mês/ano selecionado.

**Permission:** `folha_ponto.view`
**Query params:** `mes` (int), `ano` (int)
**Response:** `200 text/html` — `rh/folha_ponto.html`

---

### `POST /rh/folha-ponto/<funcionario_id>/batida/adicionar`

Adiciona uma batida administrativa com motivo auditado.

**Permission:** `folha_ponto.edit`
**Form fields:** `novo_horario` (datetime-local), `motivo` (str), `mes`, `ano`
**Response:** Redirect `GET /rh/folha-ponto/<id>` com flash de resultado
**Audit:** `AuditLog` via `PontoService.registrar_batida(source="admin")`

---

### `POST /rh/folha-ponto/<funcionario_id>/batida/<entry_id>/editar`

Edita o horário de uma batida existente.

**Permission:** `folha_ponto.edit`
**Form fields:** `novo_horario`, `motivo`, `mes`, `ano`
**Response:** Redirect com flash
**Audit:** `PontoService.editar_batida_admin` — snapshot before/after

---

### `POST /rh/folha-ponto/<funcionario_id>/batida/<entry_id>/deletar`

Remove uma batida com auditoria completa.

**Permission:** `folha_ponto.edit`
**Form fields:** `motivo` (obrigatório), `mes`, `ano`
**Response:** Redirect com flash
**Audit:** `PontoService.deletar_batida_admin` — snapshot antes da exclusão

---

### `POST /rh/folha-ponto/<funcionario_id>/deducao`

Abate saldo do banco de horas — folga compensatória ou pagamento em dinheiro.

**Permission:** `folha_ponto.deducao`
**Form fields:** `deduction_type` (`folga` \| `pagamento`), `minutos`, `data_registro`,
`observacao`, `pagamento_valor`, `mes`, `ano`

> `deduction_type` aceita **apenas** os valores de `DeductionType`. Ausências
> (férias, atestado, licença, falta) não são abatimento e pertencem ao fluxo de
> escala/ausência — lançá-las aqui debitaria crédito do colaborador.

**Regras (`deduction_type=pagamento`):** `pagamento_valor` e `observacao` são
obrigatórios. `pagamento_valor` aceita vírgula decimal. `minutos` não pode exceder
o saldo atual do colaborador.
**Regras (`deduction_type=folga`):** `data_registro` é o dia da folga, que deixa de
cobrar jornada e é reprocessado na hora. `minutos` é **ignorado** — abate-se a
jornada prevista do dia. Recusado se o dia não tem jornada prevista ou já tem folga.
A trava de saldo considera o dia já neutralizado. O estorno devolve a jornada ao dia.
**Visibilidade:** `observacao` é exibida ao colaborador no extrato do portal.
**Response:** Redirect com flash
**Delegates to:** `PontoService.registrar_abatimento_horas`

---

### `POST /rh/folha-ponto/<funcionario_id>/deducao/<deducao_id>/estornar`

Estorna um lançamento por contra-lançamento: cria a linha inversa apontando para
a original via `estorno_de_id` e devolve o saldo. Nada é apagado — as duas linhas
seguem visíveis ao colaborador.

**Permission:** `folha_ponto.deducao`
**Form fields:** `motivo` (obrigatório, exibido ao colaborador), `mes`, `ano`
**Guards:** o lançamento precisa pertencer ao `funcionario_id` da URL (IDOR);
não se estorna um estorno nem um lançamento já estornado.
**Response:** Redirect com flash
**Delegates to:** `PontoService.estornar_abatimento`

---

### `GET /rh/folha-ponto/<funcionario_id>/pdf`

Gera PDF da Folha de Ponto via WeasyPrint.

**Permission:** `folha_ponto.imprimir`
**Query params:** `mes`, `ano`
**Response:** `application/pdf` inline — filename `folha_ponto_{cpf}_{ano}-{MM}.pdf`
**Template:** `rh/folha_ponto_pdf.html` (CSS inline, A4 landscape, sem JS)

---

## Module: Escala (`/rh/escala`)

Quadro mensal de escala por colaborador × dia. Serviço: `EscalaService`
(`app/services/escala_service.py`, sem imports de Flask). Modelo:
`EscalaEntrada` (`app/models/escala.py`), tabela `sistur_escala_entradas`,
uma linha por `(funcionario_id, data)`.

**Permissões:** grupo `escala` — `escala.view` (leitura) / `escala.edit`
(escrita). Todas as rotas exigem `@login_required`.

**`tipo`** (estado base do dia): `NORMAL` (trabalha) | `FOLGA` (descanso) |
`FALTA_JUSTIFICADA` (ausência com justificativa por escrito). `FOLGA` e
`FALTA_JUSTIFICADA` zeram `labels` e os minutos esperados no banco de horas
(`PontoService._get_expected_minutes` — ver `docs/rh/README.md`); a
diferença entre os dois é semântica e de auditoria, não de efeito no saldo.

### `GET /rh/escala/`

Renderiza o calendário mensal. **Permission:** `escala.view`.
**Query params:** `mes`, `ano` (padrão: mês/ano atual), `area_id`, `equipe_id` (opcionais).
**Response:** `200 text/html` — `escala/calendario.html`.

### `POST /rh/escala/entrada`

Cria/atualiza (upsert) a entrada de um funcionário numa data — usado pelo
clique de célula e pelo pincel (AJAX). **Permission:** `escala.edit`.

**JSON body:** `funcionario_id` (int, obrigatório), `data` (YYYY-MM-DD,
obrigatório), `tipo` (obrigatório), `labels` (list[str], opcional —
MANHA/TARDE/NOITE/PLANTAO, só para NORMAL), `equipe_id` (int, opcional),
`justificativa` (str) — **obrigatória quando `tipo == "FALTA_JUSTIFICADA"`**,
opcional nos demais. Pode conter URLs; a UI reconhece como link clicável,
mas o texto chega e é tratado como não-confiável (sempre escapado antes de
qualquer link ser extraído — nunca HTML confiável vindo do input).

**Response:** `200 {"ok": true, "id", "tipo", "labels"}` · `400 {"ok": false, "erro"}`
(inclui `tipo` inválido, rótulo inválido, funcionário não encontrado, ou
`FALTA_JUSTIFICADA` sem justificativa).

### `POST /rh/escala/entrada/remover`

Remove a entrada de um funcionário numa data. **Permission:** `escala.edit`.
**JSON body:** `funcionario_id`, `data`. **Response:** `200 {"ok": true}` · `400`.

### `POST /rh/escala/lote`

Aplica o mesmo estado a múltiplos funcionários × intervalo de datas (form
submit, não AJAX). **Permission:** `escala.edit`. **Form fields:**
`funcionario_ids[]`, `data_inicio`, `data_fim`, `tipo`, `labels[]` (opcional),
`equipe_id` (opcional), `justificativa` (obrigatória se `tipo ==
"FALTA_JUSTIFICADA"` — aplicada a **todas** as combinações do lote, ex.: um
atestado que cobre vários dias), `mes`/`ano` (para o redirect).
**Response:** `302` → `GET /rh/escala/` com flash.

**Nota de UI:** `FALTA_JUSTIFICADA` não está disponível no pincel de pintura
instantânea (exige diálogo/justificativa) — só no clique de célula (modal)
ou neste formulário de lote.

### `POST /rh/escala/gerar`

Gera a escala do mês a partir da jornada semanal (CLT ativos): dias ativos
viram `NORMAL`, dias inativos viram `FOLGA`. Nunca gera `FALTA_JUSTIFICADA`
— é sempre lançamento manual. **Permission:** `escala.edit`.

---

## Module: Reservas (`/reservas`) — Headless API

> **NOTE:** For detailed request/response schemas, permissions, and business logic,
> see `docs/reservas/README.md`. This section lists endpoints only.

### `GET /reservas/mapa-ativos`

Visualização operacional em tempo real da ocupação dos recursos físicos (Mapa de Ativos). Suporta agrupamento hierárquico por zonas.

**Auth required:** Yes
**Permission required:** `reservas` → `view`
**Query params:** `date` (string YYYY-MM-DD, opcional) — Visualiza ocupação para uma data específica. Padrão: hoje.
**Response:** `200 text/html` — `reservas/mapa_ativos.html`
**Context:** Passes `status_config_map` (dict `internal_name → ReservaStatusConfig`) for dynamic slot colors. Blocking statuses determined at runtime via `ReservaStatusConfig.blocks_resource`.

---

### `GET /reservas/admin/config/status`

Painel de administração para configuração dinâmica de status de reserva (rótulos, cores, regras de negócio).

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Response:** `200 text/html` — `reservas/admin/status_config.html`

---

### `POST /reservas/admin/config/status/<int:config_id>/editar`

Salva alterações (label, color_hex, blocks_resource, show_on_dashboard) em uma entrada de `ReservaStatusConfig`.

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Form fields:** `label` (str), `color_hex` (str `#RRGGBB`), `blocks_resource` (checkbox `1`), `show_on_dashboard` (checkbox `1`)
**Response:** `302 Redirect` → `GET /reservas/admin/config/status`

---

### `POST /reservas/admin/config/status/<int:config_id>/deletar`

Remove uma entrada de `ReservaStatusConfig` não reservada pelo sistema (`is_system_reserved=False`).

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Response:** `302 Redirect` → `GET /reservas/admin/config/status`; `400` se `is_system_reserved=True`

---

### `POST /reservas/admin/config/status/novo`

Cria nova entrada de `ReservaStatusConfig` personalizada (ex: `DEPOSIT` para indicadores visuais extras).

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Form fields:** `internal_name` (str), `label` (str), `color_hex` (str `#RRGGBB`), `blocks_resource` (checkbox), `show_on_dashboard` (checkbox)
**Response:** `302 Redirect` → `GET /reservas/admin/config/status`

---

### `GET /reservas/`

Dashboard operacional de reservas. Suporta `view=semanal` e `view=lista`.

**Auth required:** Yes
**Permission required:** `reservas` → `view`
**Query params:** `view`, `q`, `status`, `categoria_id`, `sort`, `dir`, `week_offset`
**Response:** `200 text/html` — `reservas/dashboard.html`

**Granular permissions:**

| Permissão | Efeito |
|---|---|
| `reservas.view_contact` | Exibe telefone/contato na lista e nos cards semanais. |
| `reservas.view_financial` | Exibe totais financeiros da lista e da semana. |

Quando uma permissão de Reservas tem `RolePermission.source_id`, ela vale apenas para aquela
`ReservaSource`. Sem `source_id`, continua global para o módulo.

Na visão semanal, a query passa por `ReservaAccessPolicy.filter_query()`.

---

### `GET /reservas/sources`

List all active booking venues (ReservaSource).

**Auth required:** Yes
**Permission required:** `reservas` → `view`
**Response:** `200 application/json` — list of venues with nested categories
**Status:** 501 Not Implemented (placeholder)

---

### `GET /reservas/<int:reserva_id>`

Exibe a página de detalhes de uma reserva (`detalhe.html`). Mostra dados da reserva, itens, preços e — quando aplicável — o recurso físico atribuído a cada item (ex: "📌 Churrasqueira A1").

**Auth required:** Yes
**Permission required:** `reservas` → `view`
**Path params:** `reserva_id` (int)
**Response:** `200 text/html` — renderiza `reservas/detalhe.html`
**Errors:** `403` se a policy negar visibilidade; `404` se reserva não encontrada ou soft-deleted

**Granular permissions:**

| Permissão | Efeito |
|---|---|
| `reservas.view_contact` | Exibe CPF/CNPJ e telefone. |
| `reservas.view_financial` | Exibe valores, subtotais, descontos, taxa e saldo. |
| `reservas.view_payments` | Exibe transações, PIX, dados de pagamento e cauções. |

Permissões escopadas por `source_id` só liberam esses dados quando a reserva pertence à venue
correspondente.

**For full template context, see `docs/reservas/README.md` → `GET /reservas/<int:reserva_id>`.**

---

### `GET /reservas/api/public/catalogo`

Retorna catálogo público de fontes, categorias e itens disponíveis. Não requer autenticação.

**Auth required:** No
**Permission required:** None
**Query params:** `source_id` (int, opcional) — filtra por fonte específica
**Response:** `200 application/json` — lista de fontes com categorias e itens aninhados
**Errors:** `400` se `source_id` não for inteiro; `500` em erro inesperado
**Notes:** Itens com `resource.stock_quantity = 0` são excluídos automaticamente. Quantidades de estoque nunca são expostas.
**For full schema, see `docs/reservas/README.md` → `GET /reservas/api/public/catalogo`.**

---

### `POST /reservas/api/public/simular`

Simula cálculo de preço de reserva em tempo real. Não requer autenticação.

**Auth required:** No
**Permission required:** None
**Response:** `200 application/json` — breakdown de preços (subtotal, desconto, taxa de serviço, total)

> ⚠️ **`category_id` é opcional e omiti-lo devolve o preço errado.** As regras de
> desconto ficam presas à categoria; sem ela a requisição responde 200 com um
> total acima do que será cobrado (medido: 70,00 em vez de 49,00). Sempre envie.

**Guia de integração:** `docs/reservas/GUIA_FRONTEND.md`
**For full schema, see `docs/reservas/README.md` → `POST /reservas/api/public/simular`.**

---

### `POST /api/public/reservas/criar`

Cria reserva a partir do site público. Chamado pelo **servidor** do Next.js, nunca pelo navegador.

**Auth required:** Sim — header `X-Web-Api-Key` (env `WEB_API_KEY`)
**Permission required:** None (chave de serviço)
**Request:** `source_id`, `category_id`, `customer_name`, `customer_document` (CPF, obrigatório),
`email`, `telefone`, `check_in_date`, `check_out_date`, `items[]`, `observacoes` (opcional)
**Response:** `201` com `group_id`, `reserva_id`, `total`, `expires_at`

| Código | Quando |
|---|---|
| 400 | Validação, divergência de preço, ou sem recurso físico disponível |
| 401 | Chave ausente ou incorreta |
| 404 | `source_id`/`category_id` inexistente, ou categoria de outro venue |
| 503 | `WEB_API_KEY` não configurada no servidor |

> ⚠️ **`price_override` precisa somar o TOTAL final**, não o subtotal que
> `items_breakdown` reporta. Com qualquer desconto ativo, mandar o subtotal é
> recusado por divergência. Ver o rateio em `docs/reservas/GUIA_FRONTEND.md` §5.

Resolve fonte e categoria por **ID**, ao contrário de `criar()`, que aceita nomes e
cria o venue se não achar — comportamento adequado ao balcão e perigoso aqui.

**Guia de integração:** `docs/reservas/GUIA_FRONTEND.md`

---

### `stock` em `/disponibilidade`

Ao lado de `available` (quantas sobraram), a resposta traz `stock` — a
capacidade total do recurso. É o campo que diz se o recurso é uma **unidade**
(`stock` 1, escolhida pelo nome e pela foto: Churrasqueira A4) ou um **pool**
intercambiável (`stock` > 1: oito barracas pequenas, onde só a quantidade
importa). `available` não serve para isso: um pool com uma sobrando parece uma
unidade.

---

### Janela de horário da estadia (camping)

`ReservaCategory` carrega quatro campos opcionais que decidem se a modalidade
tem dimensão de horário:

| Campo | Significado |
|---|---|
| `check_in_from` / `check_in_until` | Janela em que a portaria aceita a entrada |
| `check_out_until` | Limite da saída |
| `min_stay_hours` | Permanência mínima |

Nulos (Day use) o site pede só a data. Preenchidos (Camping: 08:00–17:00, saída
até 17:00, mínimo 24h) o site passa a pedir hora de entrada e de saída, e manda
`YYYY-MM-DDTHH:MM` para `/simular`, `/disponibilidade` e `criar`.

> ⚠️ **A hora é preço.** Itens `DAILY_PRORATED_HOURLY` cobram a primeira diária
> cheia mais pró-rata das horas excedentes. Das 08:00 às 17:00 do dia seguinte
> são **33 horas**, não 24 — R$ 123,75 contra R$ 90,00 nas mesmas duas Inteiras.
> Mandar só a data faz o site anunciar um preço e a portaria cobrar outro.

Os três endpoints aceitam data pura **ou** data com hora. `/disponibilidade`
aceita a hora e a descarta (disponibilidade é por dia); `/simular` e `criar` a
usam. Editável em `/reservas/admin/.../categoria`.

---

### `POST /api/public/reservas/<group_id>/cancelar`

Desiste de uma reserva do site que ainda não foi paga. Devolve o recurso físico
ao estoque na hora, em vez de deixar o soft lock segurá-lo por 15 minutos.

**Auth required:** Sim — header `X-Web-Api-Key` (env `WEB_API_KEY`)
**Permission required:** None (chave de serviço)
**Request:** sem corpo — o `group_id` vai na URL
**Response:** `200` com `{"cancelada": true}`

| Código | Quando |
|---|---|
| 401 | Chave ausente ou incorreta |
| 404 | `group_id` inexistente |
| 409 | Reserva paga, já cancelada, ou de origem `BALCAO` |

Só alcança `PENDING` de origem `WEB`. Reserva paga é caso de reembolso, decidido
por gente; reserva de balcão não responde à chave do site nem com o `group_id`
em mãos. Cancelar **versiona** (nova linha `CANCELED`, a anterior vira
`ARCHIVED_VERSION`) — nada é apagado, e a taxa de conversão sobrevive.

Também é o que permite à suíte de integração do site desfazer as reservas de
verdade que ela cria para testar o funil.

---

### `POST /api/internal/reservas/ingest`

Cria reserva via token Bearer (`QR_SERVICE_TOKEN`) — endpoint machine-to-machine para integração com WordPress plugin, PDV e sistemas legados. Suporta item único (Formato 1) e múltiplos itens (Formato 2). Aplica soft lock de 15 min + validação anti-fraude de preço server-side.

**Auth required:** `Authorization: Bearer {QR_SERVICE_TOKEN}` (não usa sessão)
**Permission required:** None (autenticação por token de serviço)
**Response:** `201 application/json` — `{sucesso, reserva_id, group_id, expires_at}`
**Errors:** `401` (token inválido), `400` (validação de campos ou divergência de preço), `403` (`CLIENT_BANNED`), `500` (erro inesperado — JSON, não HTML)
**Observabilidade:** toda falha (400/403/500) grava um `IntegrationLog` com `error_source=SISTUR_SYNC` e código `INGEST_*`, visível em `/admin/logs`.
**For full schema, see `docs/reservas/README.md` → `POST /api/internal/reservas/ingest`.**

---

### `POST /api/internal/ponto/importar-lote`

Importa em lote as batidas do relógio de ponto externo — endpoint machine-to-machine consumido por `externo/importador_ponto.py`, que lê o relatório `.xls` do aparelho e resolve o vínculo com o funcionário antes de enviar.

**Auth required:** `Authorization: Bearer {QR_SERVICE_TOKEN}` (não usa sessão)
**Permission required:** None (autenticação por token de serviço)
**Body:** `{batidas: [{funcionario_id, punch_time}], timezone, dispositivo, periodo_inicio, periodo_fim}` — `punch_time` é hora local do relógio em ISO 8601, sem offset; `timezone` declara o fuso e a conversão para UTC é feita no servidor.
**Response:** `200 application/json` — `{importadas, duplicadas, dias_ignorados, batidas_ignoradas, dias_em_revisao, inconsistencias}`
**Errors:** `401` (token inválido), `400` (payload inválido, fuso desconhecido, ou funcionário inexistente/inativo — nada é gravado), `500` (erro inesperado)
**Regras:** batida já existente é pulada (dedup por `funcionario_id` + `punch_time`, ignorando `punch_type`); dia que passaria de 4 batidas é descartado inteiro e gera `Aviso` para quem tem `folha_ponto.edit`; um único `AuditLog` por importação.
**For full schema, see `docs/ponto/README.md` → "Rota de importação em lote".**

---

### `GET /reservas/admin/config/banimentos`

Painel administrativo de Banimentos para cadastrar, editar e remover bloqueios por cliente scoped por local (`ReservaSource`).

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Response:** `200 text/html` — renderiza `reservas/admin/lista_negra.html`
**Compatibility:** `GET /reservas/admin/config/lista-negra` redireciona para este endpoint.

### `POST /reservas/admin/config/banimentos/criar`

Cria um banimento.

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Form fields:** `name`, `cpf`, `phone`, `email`, `block_by_name`, `block_by_cpf`, `block_by_phone`, `block_by_email`, `source_ids[]`
**Response:** `302 Redirect` → `GET /reservas/admin/config/banimentos`

### `POST /reservas/admin/config/banimentos/<int:entry_id>/editar`

Atualiza um banimento.

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Response:** `302 Redirect` → `GET /reservas/admin/config/banimentos`

### `POST /reservas/admin/config/banimentos/<int:entry_id>/deletar`

Remove logicamente um banimento.

**Auth required:** Yes
**Permission required:** `reservas` → `edit`
**Response:** `302 Redirect` → `GET /reservas/admin/config/banimentos`

---

### Planned endpoints (not yet implemented)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/reservas/` | Create new booking |
| `POST` | `/reservas/<id>/editar` | Edit booking (creates new version) |
| `POST` | `/reservas/<id>/cancelar` | Cancel booking |

**For detailed schemas and business logic, see `docs/reservas/README.md`.**

---

## Module: Pagamentos (`/api/v1/payments`) — Integração Mercado Pago

> **Autenticação:**
> - Rotas `/process` e `/generate-manual-pix`: Bearer token (`QR_SERVICE_TOKEN`) — mesmo mecanismo dos endpoints internos.
> - Rota `/webhook`: pública — chamada diretamente pelo Mercado Pago.
> - Rota `/status/<payment_id>` e `/config`: públicas — consultadas pelo WordPress.

### `POST /api/v1/payments/process`

Processa pagamento via Mercado Pago (cartão ou PIX) enviado pelo WordPress via proxy.

**Auth required:** `Authorization: Bearer {QR_SERVICE_TOKEN}`

**Request body (JSON):**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `reserva_id` | int | Sim | ID da Reserva no Sistur |
| `transaction_amount` | float | Sim | Valor total do pagamento |
| `payment_method_id` | string | Sim | `"pix"`, `"visa"`, `"master"`, etc. |
| `payer` | object | Sim | `{ email, first_name, last_name, identification: { type: "CPF", number } }` |
| `token` | string | Cartão | Token gerado pelo MP Bricks |
| `installments` | int | Cartão | Número de parcelas |
| `issuer_id` | string | Não | ID da bandeira emissora |

**Respostas de sucesso (HTTP 200):**

```json
{
  "success": true,
  "payment_id": "123456789",
  "status": "pending",
  "status_detail": "pending_waiting_transfer",
  "payment_method": "pix",
  "message": "pending_waiting_transfer",
  "qr_code": "00020101...",
  "qr_code_base64": "base64...",
  "ticket_url": "https://mercadopago.com/cobro/..."
}
```

> `qr_code`, `qr_code_base64` e `ticket_url` aparecem apenas para PIX (`payment_method_id == "pix"`).

**Resposta de rejeição (HTTP 422):**

```json
{ "success": false, "payment_id": "...", "status": "rejected", "status_detail": "...", "payment_method": "card", "message": "..." }
```

**Erros:**

| Status | Corpo | Situação |
|---|---|---|
| 400 | `{ "erro": "Campo 'reserva_id' é obrigatório." }` | Campo ausente |
| 400 | `{ "erro": "..." }` | ValueError do serviço (Reserva não encontrada, valor inválido) |
| 500 | `{ "erro": "..." }` | MP_ACCESS_TOKEN não configurado ou erro inesperado |

**Notas:**
- O campo `_transaction_id` (UUID interno da PaymentTransaction) é removido do response antes de retornar ao WordPress.
- A PaymentTransaction é criada com status `PENDING` antes da chamada ao MP.
- O UUID da transação é enviado como `external_reference` para identificação no webhook.

---

### `POST /api/v1/payments/generate-manual-pix`

Gera um pagamento PIX manualmente. Destinado a uso interno (ex: balcão via WordPress admin).

**Auth required:** `Authorization: Bearer {QR_SERVICE_TOKEN}`

**Request body (JSON):**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `reserva_id` | int | Sim | ID da Reserva no Sistur |
| `payer_email` | string | Não | E-mail do pagador. Padrão: `"<id>@sistur.local"` |

**Resposta de sucesso (HTTP 200):**

```json
{
  "success": true,
  "payment_id": "123456789",
  "qr_code": "00020101...",
  "qr_code_base64": "base64...",
  "ticket_url": "https://mercadopago.com/cobro/...",
  "status": "pending"
}
```

**Erros:**

| Status | Corpo | Situação |
|---|---|---|
| 400 | `{ "erro": "Campo 'reserva_id' é obrigatório." }` | Campo ausente |
| 404 | `{ "erro": "Reserva X não encontrada." }` | Reserva inexistente ou deletada |
| 400 | `{ "erro": "Reserva não possui valor total calculado..." }` | `total_amount` zerado |
| 422 | `{ "erro": "Mercado Pago não retornou QR Code.", "mp_status": "...", "mp_detail": "..." }` | MP não gerou QR |
| 500 | `{ "erro": "..." }` | Configuração ausente ou erro inesperado |

---

### `GET /api/v1/payments/status/<payment_id>`

Retorna o status atual de um pagamento consultando a Reserva no banco. Usado pelo WordPress para polling de PIX sem chamar a API do MP diretamente.

**Auth required:** Não

**Path param:** `payment_id` (string) — ID do pagamento no Mercado Pago.

**Resposta de sucesso (HTTP 200):**

```json
{ "payment_id": "123456789", "status": "approved" }
```

| `status` | Situação |
|---|---|
| `"approved"` | Reserva está `PAID` |
| `"rejected"` | Reserva está `CANCELED` |
| `"pending"` | Qualquer outro status, incluindo `PARTIALLY_PAID` (WordPress continua polling) |

**Erro (HTTP 404):**

```json
{ "erro": "Pagamento 123456789 não encontrado." }
```

> Retorna 404 enquanto o webhook não chegar — o WordPress deve continuar o polling ao receber 404.

---

### `GET /api/v1/payments/config`

Retorna configurações públicas de pagamento para o frontend WordPress.

**Auth required:** Não

**Resposta (HTTP 200):**

```json
{ "enabled": true, "public_key": "APP_USR-abc..." }
```

> `enabled: false` quando a Public Key não está configurada. O `access_token` **nunca** é exposto.

---

### `POST /api/v1/payments/webhook`

Recebe notificações assíncronas do Mercado Pago quando o status de um pagamento muda.

**Auth required:** Não (chamado diretamente pelo Mercado Pago)

**Request body (JSON):**

```json
{ "type": "payment", "data": { "id": "123456789" } }
```

> O MP também pode enviar `action: "payment.updated"` ou query params `?data.id=X`. Todos os formatos são aceitos.

**Resposta (HTTP 200):**

```json
{ "msg": "PaymentTransaction abc12345… aprovada." }
```

> O MP exige HTTP 200 para considerar a notificação entregue com sucesso. Erros internos também retornam 200 em casos que não envolvem falha de configuração.

**Roteamento dual-path interno:**

| Situação | Caminho | Ação |
|---|---|---|
| `external_reference` é UUID v4 | **Novo** | Atualiza `PaymentTransaction` → `_recalcular_saldo_reserva()` |
| `external_reference` ausente ou não-UUID | **Legado** | Busca `Reserva` por `payment_id`, atualiza status diretamente |

**Erros:**

| Status | Situação |
|---|---|
| 400 | Payload completamente ausente (sem body e sem query params) |
| 500 | `MP_ACCESS_TOKEN` não configurado |

---

### `POST /reservas/admin/reservas/<int:reserva_id>/gerar-pix`

Gera um PIX Mercado Pago manualmente para uma reserva via painel admin do Sistur.

**Auth required:** Sessão ativa (`@login_required`) + permissão `reservas.edit`

**Request body (JSON, opcional):**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `payer_email` | string | Não | E-mail do pagador. Padrão: `"<id>@sistur.local"` |
| `partial_amount` | float | Não | Valor parcial a cobrar. Se ausente, usa o **saldo devedor** (`total_amount - já pago`) |

> **PIX Integral:** quando `partial_amount` está ausente, o sistema cobra apenas o saldo devedor (`total_amount - soma de transações APPROVED`), não o valor total da reserva. Isso garante que pagamentos parciais anteriores sejam considerados.

**Resposta de sucesso (HTTP 200):**

```json
{
  "success": true,
  "payment_id": "123456789",
  "transaction_id": "uuid-da-payment-transaction",
  "qr_code": "00020101...",
  "qr_code_base64": "base64...",
  "ticket_url": "https://mercadopago.com/cobro/...",
  "status": "pending"
}
```

**Erros:**

| Status | Corpo | Situação |
|---|---|---|
| 404 | `{ "erro": "Reserva X não encontrada." }` | Reserva inexistente ou deletada |
| 400 | `{ "erro": "Reserva sem valor total calculado..." }` | `total_amount` zerado |
| 400 | `{ "erro": "partial_amount deve ser maior que zero." }` | Valor parcial inválido |
| 422 | `{ "erro": "Mercado Pago não retornou QR Code.", ... }` | MP não gerou QR |
| 500 | `{ "erro": "..." }` | Configuração ausente ou erro inesperado |

---

### `POST /reservas/admin/reservas/<int:reserva_id>/gerar-link-cartao`

Gera um link de pagamento por cartão de crédito via Mercado Pago Preferences. O cliente acessa o link e paga diretamente no checkout hosted do Mercado Pago. Quando o pagamento é confirmado, o MP envia um webhook e a reserva é atualizada automaticamente para `PAID` — o mesmo fluxo do PIX.

**Auth required:** Sessão ativa (`@login_required`) + permissão `reservas.edit`

**Request body (JSON, opcional):**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `payer_email` | string | Não | E-mail do pagador para pré-preencher no checkout. Padrão: `"<id>@sistur.local"` |

**Resposta de sucesso (HTTP 200):**

```json
{
  "success": true,
  "checkout_url": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=...",
  "transaction_id": "uuid-da-payment-transaction",
  "preference_id": "pref-id-do-mp",
  "expires_at": "2026-05-19T10:00:00.000-00:00",
  "amount": 200.00
}
```

> **Fluxo pós-geração:** O UUID `transaction_id` é enviado ao MP como `external_reference` da Preference. Quando o cliente paga, o MP chama `POST /api/v1/payments/webhook` com `type=payment` e o pagamento carrega o mesmo `external_reference`. O `_processar_webhook_novo()` identifica a transação pelo UUID e atualiza a reserva — sem nenhuma ação manual necessária.

> **PIX e boleto excluídos:** A Preference exclui `bank_transfer` e `ticket` dos métodos permitidos; aceita apenas cartão de crédito em até 12 parcelas.

> **Expiração:** O link expira em 24 horas a partir da geração.

**Erros:**

| Status | Corpo | Situação |
|---|---|---|
| 404 | `{ "erro": "Reserva X não encontrada." }` | Reserva inexistente ou deletada |
| 400 | `{ "erro": "Reserva sem valor total calculado..." }` | `total_amount` zerado ou nulo |
| 500 | `{ "erro": "Mercado Pago não retornou ID da Preference..." }` | MP retornou resposta inválida |
| 500 | `{ "erro": "..." }` | Configuração ausente ou erro inesperado |

---

### `POST /reservas/admin/reservas/<int:reserva_id>/verificar-pagamento`

Sincroniza manualmente o status de pagamento de uma reserva com o Mercado Pago. Útil quando o webhook não foi entregue.

**Auth required:** Sessão ativa + permissão `reservas.edit`

**Request body:** Nenhum.

**Resposta de sucesso (HTTP 200):**

```json
{ "status": "approved", "mp_status": "approved", "msg": "Reserva 42 atualizada para PAID (legado)." }
```

| `status` | Significado |
|---|---|
| `"approved"` | Reserva agora está `PAID` |
| `"partially_paid"` | Reserva está `PARTIALLY_PAID` |
| `"rejected"` | Reserva está `CANCELED` |
| `"pending"` | Qualquer outro status |

**Erros:**

| Status | Situação |
|---|---|
| 404 | Reserva não encontrada |
| 400 | Reserva sem `payment_id` (PIX ainda não foi gerado) |
| 500 | `MP_ACCESS_TOKEN` não configurado ou erro inesperado |

---

## PaymentTransaction — Modelo de Dados

```sql
CREATE TABLE sistur_payment_transactions (
    id            VARCHAR(36)    PRIMARY KEY,          -- UUID gerado pela aplicação
    reserva_group_id VARCHAR(36) NOT NULL,             -- group_id da Reserva (cross-version)
    reserva_id    INTEGER        REFERENCES sistur_reservas(id) ON DELETE SET NULL,
    status        ENUM('PENDING','APPROVED','REJECTED','CANCELLED','REFUNDED') NOT NULL DEFAULT 'PENDING',
    amount        NUMERIC(10,2)  NOT NULL,
    payment_method VARCHAR(50),
    mp_payment_id VARCHAR(100),                        -- ID retornado pelo Mercado Pago
    mp_status     VARCHAR(50),
    mp_status_detail VARCHAR(100),
    qr_code       TEXT,
    qr_code_base64 TEXT,
    ticket_url    VARCHAR(512),
    created_at    DATETIME NOT NULL,
    approved_at   DATETIME,
    mp_response_snapshot JSON,
    INDEX ix_sistur_payment_transactions_reserva_group_id (reserva_group_id),
    INDEX ix_sistur_payment_transactions_reserva_id (reserva_id),
    INDEX ix_sistur_payment_transactions_status (status),
    INDEX ix_sistur_payment_transactions_mp_payment_id (mp_payment_id)
);
```

**Lógica de recálculo de status (cascade):**

Após cada transação aprovada, `_recalcular_saldo_reserva(group_id)` soma todas as `PaymentTransaction` com `status = APPROVED` para o `reserva_group_id` e aplica:

| Condição | Novo status da Reserva |
|---|---|
| `sum_approved >= total_amount - 0.01` | `PAID` |
| `0 < sum_approved < total_amount` | `PARTIALLY_PAID` |
| `sum_approved == 0` | `PENDING` |

**Compatibilidade retroativa:** Pagamentos gerados antes da refatoração multi-transação têm `external_reference` ausente ou não-UUID. O webhook os roteia pelo caminho legado (`Reserva.payment_id`) sem criar `PaymentTransaction`.

---

## Module: Reservas — Soft Delete

### `POST /reservas/api/<int:id>/deletar`

Soft-deleta uma reserva. Define `deleted_at` em **todas as versões** do mesmo `group_id` e registra `AuditLog` com snapshot da versão ativa.

**Auth required:** Sessão ativa
**Permission:** `reservas.delete`

**Request body:** Nenhum.

**Resposta de sucesso (HTTP 200):**

```json
{ "mensagem": "Reserva removida com sucesso." }
```

**Erros:**

| Status | Situação |
|---|---|
| 404 | Reserva não encontrada ou já deletada |

> Soft delete nunca remove linhas do banco. Registros com `deleted_at IS NOT NULL` são invisíveis para todas as queries de listagem e disponibilidade, mas preservados para auditoria.

---

## Module: Logs de Integração (`/api/logs`)

> Captura erros originados em frontends externos (WordPress/JS, Mercado Pago Bricks).
> Documentação completa: `docs/logs/README.md`

### `POST /api/logs/payment-error`

Persiste um erro de integração de pagamento vindo do frontend WordPress.

**Auth required:** Nenhuma — endpoint público (write-only, CORS restrito ao domínio)

**Request body (JSON):**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `error_source` | string | Sim | `MP_BRICKS` \| `WP_INTERNAL` \| `SISTUR_SYNC` \| `SISTUR_QR` |
| `error_message` | string | Sim | Descrição do erro (max 2000 chars) |
| `reservation_id` | int | Não | ID da reserva no SISTUR, se disponível |
| `customer_name` | string | Não | Nome do cliente afetado (max 255 chars) |
| `error_code` | string | Não | Código de erro do MP ou sistema (max 100 chars) |
| `mp_status_detail` | string | Não | `status_detail` do MP (também lido de `raw_data.status_detail`) |
| `raw_data` | object | Não | Objeto bruto do erro (ignorado se não for dict) |

> ⚠️ **Chamado pelo servidor WordPress, não pelo browser.** A base URL do Sistur é
> DNS interno do Docker; o browser do cliente não a alcança. O plugin expõe o proxy
> `admin-ajax.php?action=rcg_log_client_error` para os erros de origem no browser.

**Resposta de sucesso (HTTP 201):**

```json
{ "logged": true, "id": 17 }
```

**Erros:**

| Status | Situação |
|---|---|
| 400 | Payload JSON ausente ou inválido |
| 400 | `error_source` não é um dos valores aceitos |
| 400 | `error_message` vazio ou ausente |
| 500 | Falha ao persistir o log |

---

### `GET /admin/logs`

Interface administrativa para visualizar logs de integração por dia.

**Auth required:** Sessão ativa + `Role.is_super_admin = True` ou `audit.view_all`

**Query parameters:**

| Param | Tipo | Default | Descrição |
|---|---|---|---|
| `data` | string | hoje | Dia a visualizar (`YYYY-MM-DD`) |
| `source` | string | `` | Filtrar por `error_source` |
| `page` | int | 1 | Paginação (25 por página) |

**Resposta:** `200 text/html` — `admin/integration_logs.html`

**Erros:**

| Status | Situação |
|---|---|
| 403 | Sem permissão `audit.view_all` e não é super admin |


---

## Module: Organização — Timeline de Equipes (`/rh/organizacao`)

> Equipes dinâmicas (2.1b): agendamento de equipes por período + timeline visual.
> Todas as rotas exigem sessão ativa.

### `GET /rh/organizacao/equipes/timeline`

Linha do tempo de equipes agendadas, com visões Dia / Semana / Mês.

**Auth required:** `equipes.view`

**Query params:**

| Param | Tipo | Descrição |
|---|---|---|
| `visao` | string | `dia` \| `semana` \| `mes`. Padrão: `semana`. |
| `data` | string (ISO) | Âncora `YYYY-MM-DD` dentro do intervalo. Padrão: hoje. |
| `detalhe` | int | PK de `EquipeAgendamento` — abre o breakdown membro×dia com disponibilidade (folga/férias/atestado cruzados automaticamente). |

**Resposta:** HTML (grade de barras coloridas por área; visão dia vira lista de cards).

### `POST /rh/organizacao/equipes/agendar`

Agenda uma equipe ativa para um período. Períodos da mesma equipe não podem se sobrepor.
Membros da equipe são notificados via Aviso.

**Auth required:** `equipes.edit`

**Form fields:**

| Campo | Tipo | Descrição |
|---|---|---|
| `equipe_id` | int | PK da equipe (ativa). Obrigatório. |
| `data_inicio` | string (ISO) | Primeiro dia. Obrigatório. |
| `data_fim` | string (ISO) | Último dia (inclusive). Obrigatório. |
| `observacao` | string | Opcional. |

**Resposta:** Redirect para a timeline com flash de resultado.

**Erros (via flash):** equipe não encontrada/inativa, datas inválidas, período invertido, sobreposição com agendamento existente.

### `POST /rh/organizacao/agendamentos/<id>/cancelar`

Cancela (soft-delete) um agendamento. Membros são notificados via Aviso.

**Auth required:** `equipes.edit`

**Form fields:** `data` (string ISO, opcional — âncora para retornar à timeline).

**Resposta:** Redirect para a timeline com flash de resultado.

---

## Module: PDV — Ponto de Venda de Balcão (`/api/v1/pdv`)

API JSON da SPA de caixa. Contratos completos: `docs/pdv/README.md`.
Todas exigem sessão + permissão do grupo `pdv`.

### `POST /api/v1/pdv/shifts/open`
Abre turno de caixa (operador = sessão). Body: `source_id`, `opening_float_amount?`, `opening_float_pix?`.
**Auth:** `pdv.manage_shift`. 201 · 400.

### `POST /api/v1/pdv/shifts/<id>/close`
Fecha turno com conferência de duas fontes: gaveta física e fundo PIX de troco. Body: `closing_counted_amount`, `closing_counted_pix?`, `discrepancy_reason?`. Divergência sem motivo retorna 409 com detalhe; divergência com motivo fica `PENDING_APPROVAL`.
**Auth:** `pdv.manage_shift`. 200 · 400 · 409.

### `POST /api/v1/pdv/shifts/<id>/approve-close`
Aprova fechamento com quebra de caixa (`PENDING_APPROVAL` → `CLOSED`); o operador do turno não pode aprovar a própria quebra.
**Auth:** `pdv.manage_shift`. 200 · 400.

### `GET /api/v1/pdv/shifts/pending-approval`
Lista turnos com quebra de caixa aguardando aprovação.
**Auth:** `pdv.view_reports`. 200.

### `GET /api/v1/pdv/shifts/<id>/report`
Vendas do turno agrupadas por método de pagamento.
**Auth:** `pdv.view_reports`. 200 · 404.

### `GET /api/v1/pdv/sync-logs?outcome=&limit=`
Lista tentativas recentes da fila offline (`sistur_pdv_offline_sync_log`) para reconciliação gerencial. Retorna status, terminal, UUID da venda e resumo operacional do payload, sem expor o JSON bruto.
**Auth:** `pdv.view_reports`. 200 · 400.

### `GET /api/v1/pdv/catalog?source_id=`
Catálogo do venue com flag `pdv_offline_allowed` por item (🟢 consumível / 🔴 recurso finito). Cacheável via ETag/`If-None-Match` → 304.
**Auth:** `pdv.operate`. 200 · 304 · 400 · 404.

### `POST /api/v1/pdv/quote`
Cotação autoritativa server-side (ReservaPricingService). Body: `source_id`, `items`, `manual_discount_percent?`, `check_in?`, `check_out?`, `category_id?`.
**Auth:** `pdv.operate`. 200 · 400.

### `POST /api/v1/pdv/manager-override/authorize`
Valida CPF/senha de funcionário com `pdv.manage_shift` e motivo obrigatório para uso pontual em `POST /sale.manager_override`.
**Auth:** `pdv.operate`. 200 · 403.

### `POST /api/v1/pdv/sale`
Venda avulsa de balcão — **idempotente por `client_sale_uuid`**. `payment_method`: CASH | EXTERNAL_CARD_TERMINAL | EXTERNAL_PIX | MP_PIX | MP_CARD. Os três primeiros = dinheiro/pix/cartão já recebido pelo operador fora do sistema → Reserva nasce PAID direto; os dois últimos = o sistema gera a cobrança (QR/link) → PENDING (webhook MP). CASH aceita `change_source` (`CASH` ou `PIX`) para indicar de onde saiu o troco. Suporta `synced_offline`+`device_id` (trilha em `sistur_pdv_offline_sync_log`), `force_overbooking` apenas com `manager_override` auditado (ignora o lock de estoque), e `offline_override_note` ({cpf, reason} — declaração não verificada por senha para item 🔴 adicionado offline; **não** ignora o lock, pode ser rejeitada com 409 no sync).
**Auth:** `pdv.operate`. 201 · 200 (duplicata) · 400 · 409 (OverbookingError com detalhe de conflito).

### `POST /api/v1/pdv/sale/comanda-append`
Anexa itens à comanda de reserva ativa (nova versão via atualizar; PAID→PENDING se total mudou). Idempotente.
**Auth:** `pdv.operate`. 201 · 200 · 400 · 409.

### `GET /api/v1/pdv/customer-lookup?q=&source_id=?`
Busca reservas ativas por CPF/CNPJ ou nome (≥ 3 chars, máx. 20).
**Auth:** `pdv.operate`. 200 · 400.

### `GET /api/v1/pdv/held-carts?shift_id=` · `POST /api/v1/pdv/held-carts` · `POST /api/v1/pdv/held-carts/<id>/resolve`
Espelho servidor de carrinhos suspensos (primário é localStorage na SPA).
**Auth:** `pdv.operate`. 200/201 · 400 · 404.

### `GET /api/v1/pdv/shifts/current`
Turno OPEN do operador em sessão (restauração da SPA pós-F5). `{"shift": {...}|null}`.
**Auth:** `pdv.operate`. 200.

### `GET /api/v1/pdv/sources`
Venues ativos para abertura de turno. `{"sources": [{"id","name"}]}`.
**Auth:** `pdv.operate`. 200.

### `GET /reservas/pdv`
SPA do caixa (Fase 2) — página única com turno, busca 0ms, carrinho anti-F5, venda suspensa, pagamento com troco e comanda. Ver `docs/pdv/README.md`.
**Auth:** `pdv.operate`. 200.

### `GET /reservas/pdv/reconciliacao`
Tela gerencial de triagem dos syncs offline aceitos/rejeitados, com filtro por outcome.
**Auth:** `pdv.view_reports`. 200.

### `POST /api/v1/pdv/movimentacao-caixa/sangria`
Retirada de dinheiro físico da gaveta (sangria). `tipo_movimentacao`: `TRANSFERENCIA_INTERNA` (exige `conta_destino_id`) | `RECOLHIMENTO_COFRE` | `PAGAMENTO_DESPESA` (exige `centro_de_custo_id` — despesa fica no centro de custo, nunca na margem do PDV). Valor ≥ R$ 500 exige `manager_override` (mesmo mecanismo de `/manager-override/authorize`).
**Auth:** `pdv.manage_shift`. 201 · 400.

### `POST /api/v1/pdv/movimentacao-caixa/suprimento`
Reforço de dinheiro físico na gaveta. Mesma regra de `manager_override` acima de R$ 500.
**Auth:** `pdv.manage_shift`. 201 · 400.

### `GET /api/v1/pdv/movimentacao-caixa?shift_id=`
Lista movimentações de caixa de um turno, mais recente primeiro (inclui turno já fechado).
**Auth:** `pdv.manage_shift`. 200 · 400.

### `GET /api/v1/pdv/areas`
Áreas ativas para o seletor de centro de custo em `PAGAMENTO_DESPESA`.
**Auth:** `pdv.manage_shift`. 200.

### `POST /admin/configuracoes/modulos/nomes`

Salva os nomes de exibição dos módulos (white-label interno, 2.12). A chave
interna do módulo (permissões, rotas, auditoria) nunca muda — só o rótulo em
cards, títulos e na tela de permissões.

**Auth required:** Sessão ativa + `Role.is_super_admin = True`

**Form fields:** `nome_<modulo>` (str) para cada módulo renomeável
(`rh`, `ponto`, `reservas`, `avisos`).
Vazio volta ao nome padrão. Persistido em `SystemSetting`
(`modulo.<chave>.nome`), auditado.

**Resposta:** Redirect para `/admin/configuracoes/` com flash de resultado.

## Module: Relatórios — Central de Relatórios do RH (`/rh/relatorios`)

> Caminho A do 2.6: relatórios estruturados somente leitura, montados por
> `RelatorioService` a partir de dados já existentes (RH, Escala,
> Organização) — sem nova migration. Preview e PDF renderizam o
> mesmo template Jinja2 (WeasyPrint), então o que aparece na tela é
> exatamente o que sai no PDF. Todas as rotas exigem sessão ativa +
> `relatorios.view` (preview) ou `relatorios.imprimir` (PDF).

### `GET /rh/relatorios/`

Tela única de seleção: tipo de relatório + filtros (data/período/área/equipe)
→ botões "Pré-visualizar" e "Baixar PDF". Campos exibidos/ocultados via JS
conforme o tipo escolhido. Sem `relatorios.view_all`, a lista de equipes é
restrita às equipes das quais o ator é membro.

### `GET /rh/relatorios/preview`

Renderiza o relatório em HTML na tela (não gera PDF, não audita — é apenas
visualização). Requer `relatorios.view`.

**Query params:**
- `tipo` (obrigatório): `funcionarios_ativos` | `folgas` | `escala` | `equipe`
- `data` — usado por `funcionarios_ativos` (padrão: hoje)
- `data_inicio`, `data_fim` — usados por `folgas`, `escala`, `equipe`
- `area_id` — filtro opcional (`funcionarios_ativos`, `folgas`, `escala`)
- `equipe_id` — filtro (`escala`) ou obrigatório (`equipe`)

**Escopo:** sem `relatorios.view_all`, `area_id` fica restrito à própria área
do ator e `equipe_id` às equipes das quais ele é membro (`EquipeMembro`) —
tentativa fora do escopo retorna `404`. `tipo` inválido retorna `400`.
Período inválido (fim antes do início, ou acima de 62 dias) faz *flash* +
redirect para a central.

### `GET /rh/relatorios/pdf`

Mesmos query params e regras de escopo de `preview`, mas requer
`relatorios.imprimir` e gera o PDF via WeasyPrint a partir do mesmo template.
Cada download é auditado como `AuditAction.export` no módulo `relatorios`
(sem `entity_id` — é uma consulta agregada, não uma entidade única),
guardando `tipo` e os filtros aplicados em `new_state`.

**Resposta:** `application/pdf`, `Content-Disposition: inline`.

### Tipos de relatório

| Tipo | Dados | Fonte |
|---|---|---|
| `funcionarios_ativos` | Roster do dia: quem está ativo, de folga ou escalado | `RHService.funcionarios_de_folga` + `EscalaService.presenca_dia` |
| `folgas` | Folgas dia a dia num período | `RHService.funcionarios_de_folga` |
| `escala` | Escala publicada num período, por dia | `EscalaEntrada` |
| `equipe` | Agenda, composição e custo de diaristas da equipe no período | `OrganizacaoService.composicao_no_periodo`/`listar_agendamentos` |
### RBAC

`relatorios: ["view", "view_all", "imprimir"]` — `view_all` remove a
restrição de escopo por área/equipe (mesmo padrão de `audit.view_all`);
`imprimir` é exigido apenas para o download em PDF,
não para o preview em tela.

---

## Module: Reservas — Configuração do Módulo (`/reservas/admin/config`)

Todas as rotas exigem `@login_required` + `@require_permission("reservas", "configurar")`.

> **Mudança de permissão.** As telas de `/reservas/admin/config/*` saíram de
> `reservas.edit` para a ação nova `reservas.configurar`. `reservas.edit` continua
> significando "editar a reserva de um cliente"; `configurar` é "administrar o módulo"
> (tabela de preços, status, banimentos, calendário). A migration
> `54fb7d898c6d` concede `configurar` a toda role que já tinha `reservas.edit`
> global, então nenhum perfil existente perde acesso no deploy.

### `GET /reservas/admin/config/`

Índice das telas de configuração do módulo.

**Response:** `200 text/html` — `reservas/admin/config_hub.html`

---

### `GET /reservas/admin/config/ajustes-preco/calendar/api`

Feed do calendário da tela de Ajustes de Preço.

**Query params:** `source_id` (int, obrigatório), `year` (int, padrão: ano atual),
`month` (int 1–12, padrão: mês atual)

**⚠️ Breaking change.** A resposta deixou de ser um array plano de descontos e passou a
ser um envelope com as duas camadas do calendário. Consumidor único é o JS inline de
`reservas/admin/ajustes_preco.html`.

```jsonc
{
  "discounts": [                       // formato anterior, inalterado
    {
      "id": 12, "date": "2027-11-26", "label": "Black Friday",
      "is_active": true, "category_id": null, "category_name": null,
      "tiers": [{"id": 5, "min_days_in_advance": 30, "min_stay_days": 3, "pct": 25.0}]
    }
  ],
  "eventos": [                         // datas especiais do Calendário Global
    {
      "id": 7,
      "titulo": "Alta Temporada Julho",
      "data_inicio": "2027-07-01",
      "data_fim": "2027-07-31",        // = data_inicio quando é evento de um dia
      "tipo": "HIGH_SEASON",           // NATIONAL_HOLIDAY | LOCAL_HOLIDAY | MAINTENANCE | HIGH_SEASON
      "origem": "reservas",            // "reservas" | "global"
      "afeta_precificacao": true,
      "afeta_folha": false,
      "recorrente_anual": false,
      "editavel": true                 // política aplicada no servidor (ver abaixo)
    }
  ]
}
```

**Erros:** `400` `{"erro": "source_id obrigatório"}` · `400` `{"erro": "month deve estar entre 1 e 12"}`

---

### `POST /reservas/admin/config/ajustes-preco/calendar/evento/criar`
### `POST /reservas/admin/config/ajustes-preco/calendar/evento/<event_id>/editar`
### `POST /reservas/admin/config/ajustes-preco/calendar/evento/<event_id>/deletar`

CRUD de **data especial** (feriado / alta temporada) pelo módulo de Reservas.
Gravam em `sistur_global_events` — a mesma tabela do Calendário Global, que é a fonte
única de datas especiais. Não existe tabela de feriado por módulo.

**Form fields** (criar/editar):

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `titulo` | str (≤150) | ✅ | Nome do evento |
| `data_evento` | `YYYY-MM-DD` | ✅ | Primeiro dia |
| `data_fim` | `YYYY-MM-DD` | ❌ | Último dia (inclusive). Vazio = evento de um dia |
| `tipo` | enum | ✅ | `NATIONAL_HOLIDAY`, `LOCAL_HOLIDAY` ou `HIGH_SEASON` |
| `recorrente_anual` | `"1"` | ❌ | Repete todo ano (mês+dia) |
| `cal_source`, `open_year`, `open_month` | — | ❌ | Só para reabrir o calendário no mesmo ponto |

`MAINTENANCE` **não** é aceito aqui — é evento operacional, exclusivo do Calendário Global.

**Response:** `302` → `GET /reservas/admin/config/ajustes-preco` com flash.

#### Política de fronteira entre os dois calendários

Na criação a rota **força** os valores, ignorando o que vier no formulário:

```
afeta_precificacao = True
afeta_folha        = False
origem             = "reservas"
```

Na edição e na remoção vale `CalendarService.editavel_por_reservas(evento)`:

```
editável ⟺ origem == "reservas"  E  afeta_folha == False
```

Ou seja, o módulo mantém o que ele mesmo criou **enquanto o evento for só de preço**.
Quando o super admin marca "Afeta folha/ponto" em `/admin/configuracoes/`, o evento é
promovido a feriado do sistema e fica somente-leitura no módulo — continua visível no
calendário, em cinza. Tentativas de editar/remover fora da política não alteram nada e
retornam `302` com flash de erro.

---

### `POST /reservas/admin/config/ajustes-preco/config/<source_id>`

Salva taxa de serviço e desconto linear de uma fonte (`ReservaConfig`).

**Form fields:** `service_fee_percent` (0–100), `flat_discount_percent` (0–100).
Aceitam vírgula decimal. **Campo vazio remove o override da fonte**, que volta a herdar
a configuração global (`source_id IS NULL`).

**Response:** `302` → `GET /reservas/admin/config/ajustes-preco` com flash.

---

## Module: Reservas — Analytics (`/reservas/analytics`)

Dashboard analítico do módulo de Reservas. Serviço: `AnalyticsService`
(`app/services/analytics_service.py`, sem imports de Flask). Todas as rotas
exigem `@login_required` + `@require_permission("reservas", "view")`.

**Query params comuns:** `data_inicio`/`data_fim` (YYYY-MM-DD, default: hoje
− 30 dias → hoje), `source_id` (int, opcional), `status` (repetível, lista de
`internal_name` — default: todos exceto `CANCELED`/`ARCHIVED_VERSION`).

### `GET /reservas/analytics/`

Renderiza o dashboard HTML — KPIs de reservas (total, receita, ticket,
antecedência, conversão), gráficos de origem/categoria/dia da semana/itens, e
o bloco **PDV & Caixa**: vendas de balcão por método de pagamento e
movimentações de caixa (sangria/suprimento) por tipo e por centro de custo.

**⚠️ O bloco PDV & Caixa ignora o filtro de `status`.** `PdvSale` e
`PdvMovimentacaoCaixa` não têm o conceito de status de `Reserva` — aplicar o
filtro misturaria dois modelos de dado diferentes sob o mesmo controle de UI
(mesmo raciocínio de `taxa_conversao_inteligente` e do CSV de fluxo de caixa,
que também ignoram esse filtro). Datas e `source_id` são respeitados
normalmente.

**Response:** `200 text/html` — `reservas/analytics.html`

### `GET /reservas/analytics/api/dados`

Mesmos filtros do dashboard HTML, retorno em JSON — para AJAX ou integração externa.

**Response `200 application/json`:**
```json
{
  "resumo": {"total_reservas": 0, "receita_total": 0.0, "ticket_medio": 0.0, "growth_...": null},
  "por_origem": [{"origem": "WEB", "count": 0, "receita": 0.0}],
  "por_categoria": [{"categoria": "...", "count": 0, "receita": 0.0}],
  "top_itens": [...],
  "antecedencia": 0.0,
  "antecedencia_stats": {"media": 0.0, "mediana": 0.0, "p90": 0.0, "n": 0},
  "conversao": {"total_intents": 0, "converted_intents": 0, "taxa": 0.0},
  "serie_temporal": [{"dia": "YYYY-MM-DD", "count": 0, "receita": 0.0}],
  "conversao_origem": [{"origem": "WEB", "total_intents": 0, "converted_intents": 0, "taxa": 0.0}],
  "pdv_resumo": {
    "total_vendas": 0, "receita_pdv": 0.0, "ticket_medio_pdv": 0.0,
    "prev_total_vendas": 0, "prev_receita_pdv": 0.0, "prev_ticket_medio_pdv": 0.0,
    "growth_vendas_pdv": null, "growth_receita_pdv": null, "growth_ticket_pdv": null
  },
  "pdv_por_metodo": [{"metodo": "CASH", "count": 0, "receita": 0.0}],
  "movimentacoes_resumo": {
    "total_sangria": 0.0, "total_suprimento": 0.0,
    "saldo_liquido": 0.0, "count_movimentacoes": 0
  },
  "movimentacoes_por_tipo": [{"tipo": "RECOLHIMENTO_COFRE", "count": 0, "valor": 0.0}],
  "movimentacoes_por_centro_custo": [{"centro_custo": "Administrativo", "count": 0, "valor": 0.0}]
}
```

`pdv_resumo.receita_pdv` soma `PdvSale.amount_total` ancorado pela data da
venda (`criado_em`) — inclui vendas `MP_PIX`/`MP_CARD` ainda `PENDING`, ao
contrário de `resumo.receita_recebida` (que só conta `PaymentTransaction`
`APPROVED`). `movimentacoes_por_centro_custo` só lista `PAGAMENTO_DESPESA` —
é o único tipo de movimentação com `centro_de_custo_id` (ver
`TipoMovimentacaoCaixa` em `docs/pdv/README.md`).

### `GET /reservas/analytics/export/fluxo-caixa.csv`

Exporta o fluxo de caixa do Mercado Pago (uma linha por transação — não
inclui PDV/sangria, que não passam pelo ledger `PaymentTransaction`).

**Response:** `200 text/csv`
**Erros (via flash):** percentual não numérico, fora de 0–100, ou fonte inexistente.
