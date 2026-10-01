# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

# Auth, Roles e Permissões

Este módulo documenta autenticação, sessão e RBAC do Sistur.

## Visão geral

O controle de acesso principal do portal usa:

```text
Funcionario -> Role -> RolePermission(modulo, acao)
```

Arquivos principais:

- `app/models/funcionario.py` — funcionário autenticável no portal.
- `app/models/role.py` — `Role` e `RolePermission`.
- `app/core/permissions.py` — `has_permission()` e `require_permission()`.
- `app/core/permission_catalog.py` — catálogo central das permissões conhecidas.
- `app/core/access_policy.py` — `ActorContext`, policies de domínio e delegação.
- `app/services/role_service.py` — CRUD de roles, permissões e atribuição com auditoria.
- `app/blueprints/configuracoes/routes.py` — UI de gerenciamento de roles.

`sistur_users` e `UserAreaPermission` ainda existem para fluxos administrativos/legados de supervisão
por área; código novo deve usar o modelo baseado em `Funcionario`.

## Autenticação

O login do portal usa o CPF do funcionário. Rotas protegidas usam `@login_required`, que exige
`session["funcionario_id"]`.

Fluxo:

1. Operador informa CPF/senha no portal.
2. A sessão grava `funcionario_id`.
3. Rotas protegidas carregam o `Funcionario` ativo.
4. Decorators e services avaliam permissões do role associado.

Auditoria:

- login bem-sucedido/falho gera `AuditLog(action=login, module=auth)`;
- logout gera `AuditLog(action=logout, module=auth)` quando havia sessão ativa.

## Roles

`Role` representa um papel operacional do portal.

Campos principais:

| Campo | Descrição |
|---|---|
| `nome` | Slug único do papel, ex.: `super_admin`, `funcionario`, `supervisor_reservas`. |
| `descricao` | Texto legível para UI. |
| `is_super_admin` | Se `true`, bypassa todas as permissões. |
| `ativo` | Soft-disable do role. |

`Funcionario.role_id` aponta para `sistur_roles.id`. Funcionário sem role não possui acesso, exceto no
modo bootstrap descrito abaixo.

## Permissões

`RolePermission` armazena permissões como par:

```text
modulo.acao
```

Exemplos:

```text
reservas.view
reservas.view_contact
reservas.view_financial
folha_ponto.imprimir
roles.manage_reservas
```

O catálogo oficial fica em `app/core/permission_catalog.py`. Ele alimenta:

- editor de roles;
- dashboard do portal;
- seed/listagem CLI.

Não criar listas paralelas de permissões dentro de blueprints.

## Regras de `has_permission()`

`has_permission(funcionario_id, modulo, acao)` segue esta ordem:

1. Funcionário inexistente/inativo: `False`.
2. Funcionário sem role:
   - se não existir nenhum `super_admin` ativo, entra em bootstrap mode e retorna `True`;
   - se já existir `super_admin`, retorna `False`.
3. Role inativo/inexistente: `False`.
4. Role `is_super_admin=True`: `True`.
5. Caso contrário, busca `RolePermission(role_id, modulo, acao)`.

Bootstrap mode existe apenas para inicialização do sistema. Depois do primeiro super admin, ele deixa de
dar acesso a funcionários sem role.

## Decorator de rota

Rotas HTTP usam:

```python
@login_required
@require_permission("reservas", "view")
def index():
    ...
```

`require_permission()` lê `session["funcionario_id"]` e aborta:

- `401` sem sessão;
- `403` sem permissão.

Services não devem usar decorators Flask. Devem receber `ActorContext` explicitamente.

## ActorContext

`ActorContext` representa o operador logado fora da camada HTTP.

Uso esperado em services/tools:

```python
actor = ActorContext.from_funcionario_id(funcionario_id)
actor.has("reservas", "view")
```

Campos:

| Campo | Descrição |
|---|---|
| `funcionario_id` | Operador logado. |
| `role_id` | Role atual, se houver. |
| `is_super_admin` | Bypass de permissões explícitas. |
| `permissions` | Conjunto de pares globais `(modulo, acao)`. |
| `scoped_permissions` | Conjunto de triplas `(modulo, acao, source_id)` para permissões por venue. |
| `area_id` | Área do funcionário, quando existir. |

## Permissões por domínio

O catálogo já possui permissões granulares por domínio. Reservas é o domínio mais detalhado:

| Permissão | Uso |
|---|---|
| `reservas.view` | Ver reservas. |
| `reservas.view_contact` | Ver CPF, telefone, e-mail e contato do cliente. |
| `reservas.view_financial` | Ver valores, descontos, taxas e totais. |
| `reservas.view_payments` | Ver dados/transações de pagamento. |
| `reservas.create` | Criar reservas. |
| `reservas.edit` | Editar reservas. |
| `reservas.delete` | Excluir/soft-delete reservas. |
| `reservas.export` | Exportar/listar em massa. |

Permissões de Reservas podem ser globais ou escopadas por venue:

| Forma | Regra |
|---|---|
| `source_id = NULL` | A permissão vale para todas as venues do módulo Reservas. |
| `source_id = <ReservaSource.id>` | A permissão vale somente naquela venue. |

Exemplo: um role pode ter `reservas.view` global, mas `reservas.view_financial` apenas para a
venue Cachoeira. Nesse caso ele vê reservas de todas as venues, mas só vê valores financeiros da
Cachoeira.

Na UI de Configurações > Roles > Permissões:

- Os toggles principais continuam representando permissões globais (`source_id = NULL`).
- A seção "Reservas por venue" permite marcar a mesma ação em venues específicas.
- Para cada ação de Reservas, global e escopado são alternativas operacionais: ao salvar uma ação como
  global, o sistema remove escopos redundantes daquela ação; ao deixar global desmarcado, as venues
  marcadas definem onde a permissão vale.
- Permissões escopadas de Reservas não aparecem como permissões globais no editor, evitando confusão
  entre "vale em todo o módulo" e "vale apenas nesta venue".
- Supervisores delegados com permissão escopada só conseguem conceder/remover essa mesma permissão nas
  venues que já estão dentro do próprio escopo. Isso preserva a regra "mesmo nível ou menor".

RH possui permissões já existentes:

| Permissão | Uso |
|---|---|
| `funcionarios.*` | Cadastro e listagem de funcionários. |
| `rh.view` / `rh.edit` | Operações específicas de RH ainda em consolidação. |
| `folha_ponto.view` | Ver folha de ponto. |
| `folha_ponto.edit` | Editar batidas administrativas. |
| `folha_ponto.deducao` | Registrar dedução de banco de horas. |
| `folha_ponto.imprimir` | Gerar PDF/exportar folha. |
| `ajuste_ponto.aprovar` | Aprovar ajustes de ponto. |

## Escopo e redação de Reservas

`ReservaAccessPolicy` em `app/core/access_policy.py` implementa a primeira policy por domínio:

- `can_view(actor, reserva)` — valida acesso à reserva.
- `visible_source_ids(actor)` — resolve fontes visíveis.
- `filter_query(query, actor)` — aplica filtro de fonte em queries de reserva.
- `redact_dict(actor, data)` — remove campos sensíveis conforme permissão.

Redação atual:

| Sem permissão | Campos removidos |
|---|---|
| `reservas.view_contact` | CPF/documento, telefone, e-mail em campos diretos e `metadata_info`. |
| `reservas.view_financial` | Totais, subtotais, descontos, taxa de serviço, caução. |
| `reservas.view_payments` | `payment_id`, `payment_data`, `pix_data`, transações e cauções. |

Estado atual em Reservas:

- `GET /reservas/api/<id>` e `GET /reservas/api/` serializam reservas com `ReservaAccessPolicy.redact_dict()`.
- `GET /reservas/<id>` oculta contato, financeiro e pagamentos conforme as permissões granulares.
- `GET /reservas/` oculta telefone e totais na lista/semana quando o ator não possui a permissão granular.
- A visão semanal já aplica `ReservaAccessPolicy.filter_query()` sobre a query de reservas.
- `RolePermission.source_id` permite limitar `reservas.view`, `view_contact`, `view_financial`,
  `view_payments` e demais ações de Reservas a uma venue específica.

## Administração delegada por domínio

Além de `super_admin`, o sistema suporta o conceito de supervisores de domínio:

```text
supervisor_reservas
supervisor_rh
```

Permissões de delegação:

| Permissão | Domínio |
|---|---|
| `roles.manage_reservas` | Reservas |
| `roles.manage_rh` | RH |

`RoleDelegationPolicy` define as regras:

1. `super_admin` pode tudo.
2. Supervisor não pode editar role `is_super_admin`.
3. Supervisor não pode editar o próprio role.
4. Supervisor só gerencia permissões do domínio que administra.
5. Supervisor só concede/remove permissões que ele próprio possui.
6. Supervisor não concede `roles.manage_*` para outros roles.

Isso evita escalada de privilégio e reduz dependência operacional do super admin.

Estado atual:

- A policy existe e está testada.
- `GET /admin/configuracoes/roles` já aceita supervisores delegados e lista roles comuns administráveis.
- `GET/POST /admin/configuracoes/roles/<id>/permissoes` já aceita supervisores delegados.
- Para supervisores, o editor mostra e altera somente permissões do domínio administrável.
- Permissões fora do escopo delegado são preservadas no POST, mesmo se não aparecerem na tela.
- Criação, desativação e demais configurações globais continuam exclusivas de `super_admin`.

## Cofre de segredos

Credenciais sensíveis não devem ser salvas como `SystemSetting` comum. O padrão novo é
`SecretService` em `app/services/secret_service.py`, com tabela `sistur_secrets`.

Regras:

- Valor bruto nunca é salvo em texto puro; `value_encrypted` usa `cryptography.fernet`.
- A chave mestra fica fora do banco em `SISTUR_SECRETS_KEY`.
- UI/services só devem exibir máscara (`********abcd`), nunca o segredo completo.
- Auditoria registra criação/alteração/remoção sem valor bruto e sem ciphertext.
- Mercado Pago lê `mercadopago.access_token` pelo `SecretService` antes do
  `SystemSetting` legado e do `MP_ACCESS_TOKEN`.

## Auditoria

Toda mutação de role/permissão deve passar por `RoleService`, que registra `AuditLog`:

- criação/edição/desativação de role;
- adição/remoção de `RolePermission`;
- atribuição/removal de role em funcionário.

A ação usada para mudanças de permissão de usuário é `AuditAction.permission_change`.

## Regras para código novo

1. Não criar permissões fora de `app/core/permission_catalog.py`.
2. Blueprints usam `@require_permission()`; services usam `ActorContext`.
3. Permissões sensíveis devem ser separadas por campo, não agrupadas em `view`.
4. Qualquer alteração de role/permissão passa por `RoleService` e gera auditoria.
5. Supervisores de domínio nunca podem conceder mais permissão do que possuem.

## Testes

Cobertura principal:

- `tests/services/test_role_service.py`
- `tests/services/test_access_policy.py`
- `tests/blueprints/test_configuracoes_role_delegation.py`

Casos cobertos:

- super admin bypass;
- bootstrap mode;
- role inativo/sem role;
- catálogo de permissões;
- `ActorContext`;
- escopo e redação de Reservas;
- bloqueios de administração delegada contra escalada de privilégio.
