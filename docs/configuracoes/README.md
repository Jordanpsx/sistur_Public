# Módulo: configuracoes

Prefixo: `/admin/configuracoes` · Blueprint: `app/blueprints/configuracoes/routes.py` ·
Services: `app/services/configuracao_service.py` (`ConfiguracaoService`),
`app/services/calendar_service.py` (`CalendarService`), `app/services/informativo_service.py`
(`InformativoService`), `app/services/secret_service.py` (`SecretService`)

---

## Acesso — não é 100% super_admin exclusivo

A maioria das rotas exige `@_login_required` + `@_super_admin_required` (403 para qualquer
outro perfil). **Exceção real e testada:**
`GET /roles` e `GET+POST /roles/<id>/permissoes` exigem só `@_login_required` — a autorização
fina é feita por `RoleDelegationPolicy`/`ActorContext` (`app/core/access_policy.py`), permitindo
que **supervisores com delegação de domínio** (não super_admin) vejam/editem um subconjunto
restrito de roles/permissões, inclusive permissões de `reservas` escopadas por venue. Super
admin continua vendo o catálogo completo. Testado em
`tests/blueprints/test_configuracoes_role_delegation.py` (11 testes).

O blueprint **não tem `before_request` de master switch** — ao contrário de `ponto` e
`reservas`, que se auto-desligam via `ConfiguracaoService.is_module_enabled`.
Configurações em si nunca é desligável (faria sentido travar o próprio painel de configuração?).

---

## Rotas

### Painel principal / módulos / branding / empresa / fuso / Mercado Pago

| Rota | Propósito |
|---|---|
| `GET /` | Painel: módulos, nomes customizados, branding, dados da empresa, MP, fuso, geofence, calendário |
| `POST /modulos/<chave>/toggle` | Liga/desliga módulo (AJAX → JSON `{ativo, chave}`) |
| `POST /modulos/nomes` | Nome de exibição customizado por módulo (white-label interno) |
| `POST /branding` | `branding.empresa_nome` / `branding.empresa_logo` |
| `POST /dados-empresa` | Razão social/CNPJ/endereço — usados no PDF da Folha de Ponto (`docs/rh/README.md`) |
| `POST /mercadopago` | Access Token / Public Key do Mercado Pago |
| `POST /fuso-horario` | `system.timezone`, validado contra `zoneinfo.ZoneInfo` (identificador inválido → flash, sem crash) |

### Roles (RBAC)

| Rota | Propósito |
|---|---|
| `GET /roles` | Lista roles — super_admin vê tudo; supervisor delegado vê subconjunto (403 se nenhum) |
| `POST /roles/criar` | Cria role — só super_admin |
| `GET+POST /roles/<id>/permissoes` | Edita permissões — inclui campos `scoped_reservas_<acao>_<source_id>` para permissão de `reservas` por venue; marcar a permissão global de uma ação remove os grants escopados redundantes dela |
| `POST /roles/<id>/desativar` | Soft-delete — só super_admin; bloqueado para roles com `is_super_admin=True` (em `RoleService.desativar`, não no blueprint) |

### Geofencing

| Rota | Propósito |
|---|---|
| `POST /geofence/criar` | Cria `GeofenceLocation` |
| `POST /geofence/<id>/editar` | Edita |
| `POST /geofence/<id>/deletar` | Remove |

Todas auditadas em `module="geofence"`. Consumidas por `PontoService._esta_dentro_do_geofence`
(`docs/ponto/README.md`).

### Calendário Global

| Rota | Propósito |
|---|---|
| `POST /calendario/criar` | `CalendarService.criar` |
| `POST /calendario/<id>/editar` | `CalendarService.atualizar` |
| `POST /calendario/<id>/deletar` | `CalendarService.deletar` |

**Form fields:** `titulo`, `data_evento` (`YYYY-MM-DD`), `data_fim` (opcional — último dia
do intervalo, inclusive; vazio = evento de um dia), `tipo`, `afeta_precificacao`,
`afeta_folha`, `recorrente_anual` (checkboxes com valor `"1"`).

#### Esta tela não é mais a única porta de entrada

O admin do módulo de Reservas também cadastra datas especiais, pelo calendário de
`/reservas/admin/config/ajustes-preco` (permissão `reservas.configurar`). A tabela é a
mesma — `sistur_global_events` continua sendo fonte única — o que muda é a política:

| | Calendário Global (aqui) | Calendário de Reservas |
|---|---|---|
| Quem acessa | `Role.is_super_admin` | `reservas.configurar` |
| Tipos que pode criar | todos, inclusive `MAINTENANCE` | `NATIONAL_HOLIDAY`, `LOCAL_HOLIDAY`, `HIGH_SEASON` |
| `afeta_folha` | livre | sempre `False` na criação |
| `origem` gravada | `global` | `reservas` |
| Pode editar | qualquer evento | só `origem="reservas"` **e** `afeta_folha=False` |

A coluna **Origem** na tabela mostra de onde veio cada evento. Marcar "Afeta folha/ponto"
num evento de origem `reservas` é a **promoção**: o evento vira feriado do sistema
(conta para ponto e banco de horas) e passa a ser gerido exclusivamente aqui — no módulo
de Reservas ele fica somente-leitura, com cadeado.

#### `afeta_precificacao` manda mais que `tipo`

Qualquer evento com `afeta_precificacao=True` faz as reservas usarem a tarifa de **data
especial** do item (coluna `price_holiday`), independentemente do tipo — feriado e alta
temporada valem igual. Antes, só `NATIONAL_HOLIDAY`/`LOCAL_HOLIDAY` tinham efeito e
`HIGH_SEASON` era rótulo sem consequência.

`afeta_folha`, ao contrário, segue restrito a feriado nacional/local:
`CalendarService.eh_feriado()` — que alimenta ponto e banco de horas — ignora
`HIGH_SEASON` de propósito. Alta temporada muda preço, não jornada.

### Cofre de segredos

| Rota | Propósito |
|---|---|
| `GET /segredos` | Lista as integrações do catálogo com estado e máscara — o valor bruto nunca é exibido |
| `POST /segredos/salvar` | Grava/atualiza um segredo (`key` do catálogo + `value`), criptografado com `SISTUR_SECRETS_KEY` |
| `POST /segredos/<key>/remover` | Desativa o segredo (idempotente) |

O catálogo (`SECRETS_CATALOG`, no próprio blueprint) tem hoje uma entrada:
`mercadopago.access_token`. Quando presente no cofre, ele tem prioridade sobre o campo legado
`mercadopago.access_token` de `SystemSetting` e sobre a variável `MP_ACCESS_TOKEN`
(ver `MercadoPagoService._access_token`, em `docs/pagamentos/README.md`).

### Informativos

| Rota | Propósito |
|---|---|
| `GET /informativos` | Lista |
| `POST /informativos/criar` | Cria |
| `POST /informativos/<id>/editar` | Edita |
| `POST /informativos/<id>/deletar` | Remove |

`Informativo` (`sistur_informativos`): `title`, `slug` (unique), `content`, `sync_wordpress`.
`InformativoService._disparar_sidecars` envia o conteúdo para a página correspondente do
WordPress quando `sync_wordpress` está marcado. A coluna `sync_bot` continua na tabela por
compatibilidade com o banco, mas não é mais lida nem gravada. Sem testes de rota — gap conhecido.

---

## Modelos

| Model | Tabela | Papel |
|---|---|---|
| `SystemSetting` | `sistur_system_settings` | Key-value: `chave` (unique), `valor` (Text), `tipo` (bool/string/int) |
| `GlobalEvent` + `TipoEvento` | `sistur_global_events` | Calendário Global — `NATIONAL_HOLIDAY`/`LOCAL_HOLIDAY`/`MAINTENANCE`/`HIGH_SEASON` |
| `GeofenceLocation` | `sistur_geofence_locations` | `latitude`, `longitude`, `radius_meters` (default 200), `is_active` |
| `Informativo` | `sistur_informativos` | Ver acima |
| `Secret` | `sistur_secrets` | Segredos criptografados (Fernet); guarda só o valor cifrado e os 4 últimos caracteres para a máscara |

---

## `ConfiguracaoService` — métodos

Todos `@staticmethod`, zero imports Flask.

| Método | Papel |
|---|---|
| `get(chave, default=None)` | Leitura com fallback |
| `get_all()` | Todas as chaves |
| `is_module_enabled(modulo)` | Master switch — desconhecido = `True` |
| `get_nome_modulo(modulo, default=None)` / `get_nomes_modulos()` | Nome de exibição customizado |
| `set(chave, valor, ator_id=None)` | Upsert + audita `module="configuracoes"` |

`MODULOS = ["ponto", "reservas"]` — os módulos que têm master switch.

---

## Inventário de chaves `SystemSetting`

Com metadado registrado (`_METADADOS`, com tipo/default/descrição):

| Chave | Tipo | Default | Escrita por |
|---|---|---|---|
| `modulo.ponto` / `modulo.reservas` | bool | `True` | `toggle_modulo` |
| `modulo.<nome>.nome` | string | `None` | `salvar_nomes_modulos` — só o **rótulo** muda; a chave interna do módulo é fixa em permissões/rotas/auditoria |
| `branding.empresa_nome` / `branding.empresa_logo` | string | `None` | `salvar_branding` |
| `empresa.razao_social` / `empresa.cnpj` / `empresa.endereco` | string | `None` | `salvar_dados_empresa` |
| `mercadopago.access_token` / `mercadopago.public_key` | string | `None` | `salvar_mercadopago` — campo vazio no submit **remove** a config e cai no fallback (env var) |
| `system.timezone` | string | `America/Sao_Paulo` | `salvar_fuso_horario` |

**Chaves herdadas no banco:** instalações que vieram do Sistur completo ainda têm linhas
`bot.*`, `ia.*`, `modulo.estoque`, `modulo.restaurante`, `modulo.financeiro` e
`modulo.tarefas.nome` em `sistur_system_settings`. Nenhum código as lê; são inofensivas.

**Achado a observar:** `scheduler.timezone` é citado em `docs/api_catalog.md` como lido por
`AvisoService`, mas não existe nenhuma ocorrência real no código — doc desatualizada, não
chave ativa.

---

## `CalendarService` — métodos

| Método | Papel |
|---|---|
| `eh_feriado(data)` | Bool |
| `obter_eventos_mes(ano, mes)` | Lista do mês |
| `eh_alta_temporada(inicio, fim)` | Bool |
| `criar` / `atualizar` / `deletar` | Mutações, auditadas em `module="calendario"` |

---

## Testes

| Arquivo | Cobre |
|---|---|
| `tests/services/test_configuracao_service.py` | `get`/`set`, conversão de tipo, defaults, `is_module_enabled`, `get_all`, nomes de módulos |
| `tests/services/test_calendar_service.py` | `eh_feriado`, `obter_eventos_mes`, `eh_alta_temporada`, CRUD |
| `tests/blueprints/test_configuracoes_role_delegation.py` | Delegação de supervisor em `/roles` e `/roles/<id>/permissoes`, inclusive escopo por venue |

**Gaps conhecidos (sem teste de rota):** geofence CRUD, calendário CRUD (nível blueprint),
informativos CRUD, cofre de segredos.

---

## Documentação de contrato pendente

`docs/api_catalog.md` está desatualizado para este módulo: faltam `mercadopago`,
`fuso-horario`, as 3 rotas de `geofence/*`, as 3 de `calendario/*` e as de Informativos.
