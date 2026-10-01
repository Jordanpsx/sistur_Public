# Módulo: admin

Prefixo: `/admin` · Blueprint: `app/blueprints/admin/routes.py`

> **Atenção de escopo:** `/admin/configuracoes/*` é um blueprint **separado** (`configuracoes`,
> registrado com prefixo aninhado) — não vive em `app/blueprints/admin/routes.py`. Ver
> `docs/configuracoes/README.md`.
> Este arquivo cobre só as 4 rotas que de fato pertencem ao blueprint `admin`.

---

## Rotas

| Rota | Permissão | Resposta | Propósito |
|---|---|---|---|
| `GET /admin/audit-logs` | `audit.view_all` **ou** `audit.view` | HTML | Visualizador de auditoria — níveis de acesso e filtros em `docs/api_catalog.md` ("Admin — Audit Log Viewer") |
| `GET /admin/logs` | `audit.view_all` (não aceita `audit.view`) | HTML | Log de erros de integração (frontends externos — WP/MP Bricks) |
| `GET /admin/reports/items-consumption` | `reservas.view` **ou** `audit.view_all` | HTML | Consumo agregado de itens de reserva num período |
| `GET /admin/reports/items-consumption/<item_id>/reservations` | `reservas.view` **ou** `audit.view_all` | **JSON** | Drill-down: quais reservas consumiram um item específico |

Todas exigem sessão ativa. O decorator `@_login_required` redireciona para `portal.login`
(sem sessão); dentro de cada rota, `_get_ator_sessao()` também aborta com `401` se a sessão
apontar para um `funcionario_id` inválido/inativo — as duas checagens coexistem de propósito
(o decorator cobre o caso comum com redirect amigável; o abort cobre sessão obsoleta).

**Inconsistência de propósito a conhecer:** as três primeiras rotas retornam `abort(403)` (página
de erro HTML) quando falta permissão; a última (`items-consumption/<item_id>/reservations`)
retorna `jsonify({"erro": ...}), 403` porque é um endpoint AJAX puro — quem consumir esta API não
deve esperar o mesmo formato de erro das demais.

### `GET /admin/logs`

Visualizador do log de erros capturados dos frontends externos (WordPress/Mercado Pago Bricks) —
ver `POST /api/logs/payment-error` (pública) em `docs/api_catalog.md`.

Query params: `data` (dia único), `data_fim` (fim do intervalo — **não documentado antes** no
`api_catalog.md`, que só citava `data`), `source` (uppercased — `MP_BRICKS`/`WP_INTERNAL`/
`SISTUR_QR`/`SISTUR_SYNC`), `category` (lowercased — **também ausente do `api_catalog.md`** —
ex.: `system`/`client_bank`/`invalid_data`), `page` (25/página). Se `data_fim < data`, os dois são
trocados silenciosamente — mesmo padrão de swap usado nas rotas de consumo abaixo.

### `GET /admin/reports/items-consumption` e `/<item_id>/reservations`

Soma `ReservaItemLink.quantity` por `ReservaItem`, no intervalo `Reserva.check_in_date` (não a
data de criação da reserva). Padrão do intervalo: início do mês corrente até hoje.

**Filtro triplo de soft-delete:** exige `deleted_at IS NULL` simultaneamente em
`ReservaItemLink`, `Reserva` **e** `ReservaItem` — os três precisam estar ativos.

**Status considerados "consumo":** `PENDING`, `PARTIALLY_PAID`, `PAID`, `COMPLETED`. Exclui
`CANCELED` e `ARCHIVED_VERSION` (linhas de histórico do versionamento) — cancelamento e versão
antiga nunca contam como consumo real.

O drill-down (`/<item_id>/reservations`) exige `start_date`/`end_date` (400 se ausentes ou
formato ISO inválido — ao contrário da tela principal, que cai em silêncio para o padrão). Cada
linha retornada inclui `detail_url` via `url_for("reservas.detalhe", ...)` — acopla este módulo
à rota de detalhe do blueprint `reservas`. `customer_name` vazio vira `"Balcão"` na exibição
(mesma convenção do `ReservaOrigin.BALCAO`).

---

## Acesso — nuance do bootstrap mode

`has_permission()` (`app/core/permissions.py`) tem um **modo de bootstrap**: se um funcionário
não tem `role_id` **e** não existe nenhum `Role` com `is_super_admin=True` no sistema, a função
retorna `True` incondicionalmente para ele — super-admin implícito, para não travar a
configuração inicial. Isso vale para **todas** as rotas deste blueprint (e do sistema em geral),
não só `/admin/audit-logs`. Os testes existentes sempre criam um `Role` super_admin primeiro
como pré-requisito, exatamente para não cair nesse modo sem querer.

Nuance adicional em `/admin/audit-logs`: um supervisor com `audit.view` mas `Funcionario.area_id
= NULL` não recebe 403 — passa no guard de permissão, mas a query fica deliberadamente
insatisfazível (`AuditLog.id == None`), então a tabela vem vazia em silêncio.

---

## Testes

`tests/blueprints/test_admin_audit_routes.py` — cobre **só `/admin/audit-logs`**: guard de
sessão, guard de permissão, os três níveis de acesso (supervisor escopado por área / gerente com
`view_all` / super_admin), filtros `module`/`user_id`/`page`.

**Gap conhecido:** `/admin/logs` e as duas rotas de `items-consumption` não têm teste de rota —
candidato a um próximo P3 de cobertura, no mesmo espírito do que foi feito para `banco-horas`/
`folha-ponto`/`qrcode` em `docs/rh/README.md` (item 2.8 P3).

---

## Documentação de contrato pendente

`/admin/reports/items-consumption` e `/admin/reports/items-consumption/<item_id>/reservations`
não aparecem em `docs/api_catalog.md` — só o resumo de `/admin/logs` existe lá, e está
desatualizado (falta `data_fim` e `category`). Ação futura: adicionar as duas entradas e
corrigir a existente.
