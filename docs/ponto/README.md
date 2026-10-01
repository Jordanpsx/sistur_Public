# Módulo: Ponto Eletrônico

## Visão Geral

O módulo **Ponto Eletrônico** registra, armazena e calcula o saldo diário de horas dos colaboradores do Portal do Colaborador. As regras de negócio do módulo estão descritas neste documento.

---

## Modelos ORM (`app/models/ponto.py`)

### `TimeEntry` → `sistur_time_entries`

Registro individual de cada batida de ponto.

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `id` | Integer PK | ✓ | |
| `funcionario_id` | FK `sistur_funcionarios.id` | ✓ | Colaborador |
| `punch_time` | DateTime (UTC) | ✓ | Timestamp exato da batida |
| `shift_date` | Date | ✓ | Data do turno (pode diferir de `punch_time.date()` em viragens de meia-noite) |
| `punch_type` | Enum | ✓ | `clock_in`, `lunch_start`, `lunch_end`, `clock_out`, `extra` |
| `source` | Enum | ✓ | `employee`, `admin`, `KIOSK`, `QR` |
| `processing_status` | Enum | ✓ | `PENDENTE` → `PROCESSADO` após `processar_dia()` |
| `admin_change_reason` | Text | — | Obrigatório quando `source = admin` (Rule #3) |
| `changed_by_user_id` | Integer | — | ID do admin que editou |
| `criado_em` | DateTime | ✓ | Timestamp de criação |

### `TimeDay` → `sistur_time_days`

Agregado diário por colaborador. Recalculado por `PontoService.processar_dia()`.

| Campo | Tipo | Descrição |
|---|---|---|
| `id` | Integer PK | |
| `funcionario_id` | FK | |
| `shift_date` | Date | `UNIQUE` por `(funcionario_id, shift_date)` |
| `minutos_trabalhados` | Integer | Soma bruta dos pares de batidas |
| `saldo_calculado_minutos` | Integer | Após tolerância de 10 min (Rule #4) |
| `saldo_final_minutos` | Integer | Após deduções manuais de banco de horas |
| `needs_review` | Boolean | `True` quando batidas ímpares no dia (Rule #5) |
| `expected_minutes_snapshot` | Integer | Jornada esperada no dia (imutável) |
| `criado_em` / `atualizado_em` | DateTime | |

---

## Service (`app/services/ponto_service.py`)

Todos os métodos são **estáticos** e **sem imports do Flask** (arquitetura em camadas).

### Métodos de Cálculo Puro (sem banco)

#### `calculate_daily_balance(punch_list, expected_minutes, almoco_minutes=60) → dict`

Calcula o saldo diário a partir de uma lista ordenada de batidas.

- **Rule #5**: número ímpar de batidas → retorna `needs_review=True`, saldo congelado em `0`.
- **Rule #4**: aplica tolerância de 10 min via `_apply_tolerance()`.
- **Almoço excluído implicitamente pelos pares**: com 4 batidas CLT (entrada, saída-almoço, retorno-almoço, saída), o intervalo de almoço fica entre os pares (0,1) e (2,3) e **nunca** é somado. Não há subtração adicional de `almoco_minutes`.

> ⚠️ **Bug histórico (corrigido em 2026-02-26):** versões anteriores subtraíam `almoco_minutes` fixos após somarem os pares, causando duplo desconto. Ex: 11:25→11:34 + 12:56→14:02 = 75 min, mas o código retornava 15 min.

#### `_apply_tolerance(saldo_bruto, tolerance) → int`

- `saldo_bruto >= 0` → retorna sem alteração (horas extras não são penalizadas).
- `abs(saldo_bruto) <= tolerance` → retorna `0` (atraso perdoado).
- Atraso acima da tolerância → penaliza apenas o excesso.

### Métodos de Mutação (com banco + AuditService)

#### `registrar_batida(funcionario_id, punch_time, source, ator_id) → TimeEntry`

- Valida colaborador ativo.
- Rejeita batidas duplicadas em janela de 5 segundos (anti-duplicata do legado).
- Infere `punch_type` automaticamente pela sequência do dia.
- Cria `TimeEntry`, registra `AuditLog(action=create, module='ponto')`.
- Chama `processar_dia()` para atualizar `TimeDay`.

#### `processar_dia(funcionario_id, shift_date) → TimeDay`

- Busca todas as `TimeEntry` do dia ordenadas por `punch_time`.
- Chama `calculate_daily_balance()`.
- Faz **upsert** de `TimeDay` (cria se não existe, atualiza se já existe).
- Marca todas as entradas do dia como `PROCESSADO`.

#### `editar_batida_admin(time_entry_id, novo_horario, motivo, ator_id) → dict`

- `motivo` não vazio é obrigatório (Rule #3).
- Seta `source = admin`, `admin_change_reason`, `changed_by_user_id`.
- Registra `AuditLog(action=update)` com snapshots antes/depois (Rule #1).
- Chama `processar_dia()` para recalcular o dia afetado.

#### `deletar_batida_admin(time_entry_id, motivo, ator_id) → dict`

Mesmas exigências de `editar_batida_admin` (Rule #3), mas remove a `TimeEntry` e reprocessa o
dia — pode fazer o número de batidas ficar ímpar e disparar `needs_review`.

#### `recalcular_banco_global(funcionario_id, ator_id=None) → int`

Refaz o saldo acumulado do zero: soma `TimeDay.saldo_final_minutos` de **todos** os dias do
funcionário, subtrai `TimeBankDeduction.minutos_abatidos`, sobrescreve
`Funcionario.banco_horas_acumulado`. Usado para reparo de inconsistência — ver
`RHService.divergencias_banco_horas` em `docs/rh/README.md`. **Este é o motor real do banco de
horas**, não `BancoDeHorasService` (ver `docs/banco_horas/README.md`).

#### `registrar_abatimento_horas(funcionario_id, deduction_type, minutos, data_registro, observacao, ator_id, pagamento_valor=None) → TimeBankDeduction`

Registra pagamento/desconto manual de banco de horas: cria `TimeBankDeduction` e subtrai
`minutos` de `Funcionario.banco_horas_acumulado` diretamente (cache, não recalcula os dias).

---

## Regras de Negócio

| Rule | Descrição |
|---|---|
| **#3** | Edições administrativas exigem `admin_change_reason` não vazio |
| **#4** | Tolerância de 10 min: atrasos ≤ 10 min são perdoados; apenas o excesso é penalizado |
| **#5** | Número ímpar de batidas no dia → `needs_review = True`, saldo congelado |

---

## Regra #2 — Geofencing por GPS (não por IP)

`registrar_batida` recebe `current_lat`/`current_lng` opcionais (enviados pelo JS via
`navigator.geolocation`). `PontoService._esta_dentro_do_geofence(lat, lng)` compara contra
`sistur_authorized_locations` usando `_haversine_metros` — fórmula de grande círculo, não
bounding box. Fora do raio autorizado → `GeofenceViolationError` (subclasse de `ValueError`,
capturada separadamente na rota para mensagem específica).

**Bloqueio por IP é proibido** (regra do módulo) — falsos positivos por VPN/rede móvel do
colaborador. Coordenadas ausentes (permissão negada no navegador) não bloqueiam o registro;
apenas pulam a checagem de geofence.

---

## Ajuste de Ponto (self-service) — `AjustePontoService`

`app/services/ajuste_ponto_service.py`. Fluxo para quando o colaborador esquece de bater ponto
ou bate no horário errado, sem depender de um admin editar `TimeEntry` diretamente.

| Método | Papel |
|---|---|
| `criar_solicitacao(funcionario_id, data_solicitada, horario_solicitado, tipo_ponto, motivo, time_entry_id=None)` | Cria `AjustePontoRequest`; `motivo` exige ≥10 caracteres; notifica via `AvisoService` todos com `ajuste_ponto.aprovar`. `time_entry_id=None` → pedido de inserção; presente → pedido de correção |
| `listar_pendentes()` | Fila de aprovação |
| `listar_por_funcionario(funcionario_id, limit=30)` | Histórico do colaborador |
| `contar_pendentes()` | Badge de contagem |
| `aprovar(ajuste_id, supervisor_id)` | Aplica o ajuste na `TimeEntry`/cria nova, reprocessa o dia |
| `rejeitar(ajuste_id, supervisor_id, motivo_rejeicao)` | Recusa com justificativa |

**Aprovação/rejeição são rotas do blueprint `avisos`**, não deste blueprint — ver
`docs/avisos/README.md` (`POST /avisos/ajuste/<id>/aprovar` e `/rejeitar`).

---

## Rotas (`app/blueprints/ponto/routes.py`)

| Método | URL | Permissão | Descrição |
|---|---|---|---|
| `GET` | `/ponto/` | `ponto.view` | Histórico mensal de batidas |
| `POST` | `/ponto/registrar` | `ponto.create` | Registrar nova batida (com geofence GPS) |
| `GET` | `/ponto/analise` | `ponto.view` | Análise detalhada: gráficos (Chart.js), tabela CLT expandida, cards de resumo |
| `POST` | `/ponto/solicitar-ajuste` | `ponto.view` | Abre uma solicitação de ajuste self-service (ver acima) |

Todas bloqueadas por `before_request` se `modulo.ponto` estiver desligado nas configurações
globais — exceto para `is_super_admin` (evita lockout).

### GET `/ponto/`

Query params opcionais: `mes` (1–12), `ano`. Padrão: mês e ano correntes.

Renderiza `ponto/index.html` com:
- `days` → lista de `TimeDay` do mês
- `entries_by_day` → dict `{date: [TimeEntry]}` agrupado por dia

### POST `/ponto/registrar`

Form fields: `punch_time` (ISO datetime, opcional — usa o servidor se ausente), `lat`/`lng`
(GPS, opcionais). `GeofenceViolationError` e `ValueError` viram flash de erro separadamente.

Redireciona para `/ponto/` com mensagem flash de sucesso ou erro.

### GET `/ponto/analise`

Query params: `mes`, `ano`. Monta séries para dois gráficos (horas trabalhadas/dia, saldo
acumulado no mês), tabela em layout CLT (entrada/saída-almoço/volta-almoço/saída por linha) e
cards agregados (dias OK, dias a revisar, maior crédito, maior débito).

### POST `/ponto/solicitar-ajuste`

Form fields: `data_solicitada` (YYYY-MM-DD), `horario_solicitado` (HH:MM), `tipo_ponto`,
`motivo`, `time_entry_id` (opcional). Delega toda validação de negócio para
`AjustePontoService.criar_solicitacao`.

---

## Templates

- `app/templates/ponto/index.html` — tabela mensal com navegação de mês, botão de bater ponto, badge de status (OK/Revisar), saldo colorido por sinal.
- `app/templates/ponto/analise.html` — gráficos Chart.js, tabela expandida, cards de resumo (ver `GET /ponto/analise`).

---

## Auditoria

Toda mutação registra exatamente **um** `AuditLog`:

| Operação | `action` | `previous_state` | `new_state` |
|---|---|---|---|
| Nova batida | `create` | `null` | `{funcionario_id, punch_time, punch_type, source}` |
| Edição admin | `update` | horário e source anteriores | horário e source novos + motivo |

---

## Testes

```bash
# Apenas o módulo
pytest tests/services/test_ponto_service.py tests/services/test_ajuste_ponto_service.py \
       tests/blueprints/test_ponto_routes.py -v

# Suíte completa
pytest --tb=short
```

`tests/services/test_ajuste_ponto_service.py` cobre `AjustePontoService` isoladamente
(criação, aprovação, rejeição, notificação de supervisores).

### Cobertura

| Classe de teste | Escopo |
|---|---|
| `TestApplyTolerance` | `_apply_tolerance` — 6 casos |
| `TestCalculateDailyBalance` | `calculate_daily_balance` — 9 casos |
| `TestRegistrarBatida` | mutação com DB — 5 casos |
| `TestProcessarDia` | agregação diária — 2 casos |
| `TestEditarBatidaAdmin` | edição admin — 4 casos |
| `TestHistoricoRoute` | GET /ponto/ — 3 casos |
| `TestRegistrarRoute` | POST /ponto/registrar — 3 casos |

---

## Importação do relógio de ponto externo

O aparelho instalado na portaria exporta um relatório `.xls` (Excel 97-2003 / BIFF).
Quem lê esse arquivo é o **programa externo** `externo/importador_ponto.py`, que roda
na máquina que administra o relógio. O SISTUR nunca vê o `.xls`: ele recebe as batidas
já resolvidas, em lote, por uma rota interna.

```
relógio  ──.xls──>  externo/importador_ponto.py  ──HTTP──>  SISTUR
                            │
                    funcionarios.csv
                (vínculo preenchido à mão)
```

Guia de uso do lado externo: `externo/README_IMPORTADOR.md`.

### Por que a leitura mora fora do SISTUR

O relatório identifica a pessoa apenas por um ID local do aparelho e um nome digitado
à mão. Resolver esse vínculo automaticamente exigiria comparar com a lista de
funcionários do SISTUR — e qualquer arquivo gerado com esse resultado carregaria nome
e ID do cadastro para fora do sistema, por e-mail e pendrive.

Com a leitura no programa externo, ele **não tem como** consultar o cadastro: o
vínculo é declarado à mão por quem já tem acesso legítimo ao SISTUR, e o CSV que fica
na máquina do relógio contém apenas dados que já vieram do próprio relógio.

O casamento automático também erraria: `"CARLOS PEREIRA"` casa parcialmente tanto com
*Carlos Almeida Exemplo* quanto com *Sílvio Pereira Exemplo*.

### Rota de importação em lote

```
POST /api/internal/ponto/importar-lote
Authorization: Bearer <QR_SERVICE_TOKEN>
```

```json
{
  "dispositivo": "REP-01",
  "periodo_inicio": "2026-08-01",
  "periodo_fim": "2026-08-16",
  "timezone": "America/Sao_Paulo",
  "batidas": [
    {"funcionario_id": 17, "punch_time": "2026-08-01T08:00:00"}
  ]
}
```

`punch_time` é **hora local do relógio**, sem fuso embutido; `timezone` declara qual é.
A conversão para UTC acontece no servidor — é ele quem manda no relógio do sistema, e
a máquina do aparelho não precisa carregar base de fusos.

| Status | Quando |
|---|---|
| `200` | Lote processado (mesmo que tudo tenha sido pulado por duplicidade) |
| `400` | Payload inválido, fuso desconhecido, ou funcionário inexistente/inativo — **nada é gravado** |
| `401` | Token de serviço inválido |

Resposta:

```json
{
  "importadas": 38,
  "duplicadas": 0,
  "dias_ignorados": 1,
  "batidas_ignoradas": 5,
  "dias_em_revisao": 6,
  "inconsistencias": [
    {"funcionario_id": 17, "funcionario_nome": "Sílvio Pereira Exemplo",
     "shift_date": "2026-08-08", "batidas_no_dia": 5, "ja_existiam": 0}
  ]
}
```

### Regras de negócio da importação

Implementadas em `PontoImportService.importar_lote()` (`app/services/ponto_import_service.py`).

#### 1. Deduplicação por `(funcionario_id, punch_time)`

Batida que já existe é pulada em silêncio. Reimportar o mesmo período é seguro, e é o
caminho normal depois de corrigir um vínculo no arquivo de importação.

> A `UniqueConstraint(funcionario_id, punch_time, punch_type)` **não** serve para isso:
> `punch_type` é inferido pela contagem de batidas do dia, então numa reimportação as
> repetidas entrariam como `extra`, não colidiriam com as originais, e a jornada de todo
> mundo dobraria sem nenhum erro na tela. `tests/services/test_ponto_import_service.py`
> tem uma guarda de regressão para isso (`test_dedup_ignora_o_punch_type`).

#### 2. Dia que passaria de 4 batidas é pulado inteiro

A jornada CLT tem 4 batidas. Um dia que terminaria com mais que isso é dado
inconsistente — leitura dupla no aparelho, vínculo trocado, arquivo do período errado.
**Nenhuma batida daquele dia é gravada**, nem as quatro primeiras, e um `Aviso` do tipo
`SISTEMA` vai para quem tem `folha_ponto.edit` e para os super admins.

Detalhes que importam:

- As batidas **já gravadas** contam para o limite. Três no banco mais duas novas dá
  cinco: o dia é pulado.
- A regra só é avaliada quando há batidas novas a gravar. Um dia que já estava com 5
  batidas antes da importação não gera aviso — não é obra dela.
- Um dia inconsistente não contamina os outros dias do mesmo lote.
- É **um aviso por destinatário, por importação**, listando os dias. Um relatório com
  vinte dias inconsistentes geraria sessenta notificações e ninguém leria nenhuma.

Isto é uma regra mais estrita que a Rule #5: paridade só pega contagem ímpar, e um dia
com 6 batidas passaria batido por ela.

#### 3. Funcionário inválido rejeita o lote inteiro

O `funcionario_id` vem digitado à mão num CSV, fora do sistema. Um ID inexistente ou
inativo significa vínculo errado — gravar o resto enquanto se descarta um funcionário em
silêncio é pior do que parar. A mensagem de erro lista todos os IDs problemáticos de uma
vez, para o operador corrigir numa passada só. Como a deduplicação torna a reimportação
segura, corrigir e rodar de novo não custa nada.

#### 4. Um único `AuditLog` por importação

No padrão de `PontoService.reprocess_interval()`. O `new_state` guarda dispositivo,
fuso, período, funcionários envolvidos, contagens e o detalhe das inconsistências.

#### Desempenho

A gravação acontece numa transação e `processar_dia()` roda **uma vez por par
(funcionário, dia)** afetado. `PontoService.registrar_batida()` não é usada aqui: ela faz
`commit()` e `processar_dia()` a cada batida, o que num lote típico de 296 batidas
significaria 296 commits e 296 recálculos. Sua janela anti-duplicata também não serve —
são 5 *segundos*, desenhados para o totem, não para lote histórico.

#### Origem da batida

As batidas entram com `source=PunchSource.IMPORT`. Achatar em `admin` apagaria a
distinção entre "o RH digitou" e "veio do relógio da portaria", que é justamente o que
uma auditoria trabalhista quer saber.

> **Sem migration.** A coluna `source` é `VARCHAR(8)` sem `CHECK` constraint
> (`native_enum=False` não cria constraint no SQLAlchemy 2.x), e `IMPORT` tem 6
> caracteres. Verificado com `INSERT` real no Postgres de staging.

### Inferência do tipo de batida

O tipo sai da posição cronológica da batida dentro do dia inteiro, contando as que já
estavam gravadas — 0→`clock_in`, 1→`lunch_start`, 2→`lunch_end`, 3→`clock_out`. As
entradas existentes não são reescritas.

### O que o relatório do relógio não traz

| Fato | Consequência |
|---|---|
| Horários sem segundos (`hh:mm`) | Toda batida importada tem `:00` nos segundos |
| Tipo da batida | Inferido pela ordem, via `PontoService._inferir_tipo_batida()` |
| CPF ou matrícula | Daí o arquivo de vínculo preenchido à mão |
| Célula vazia = sem batida | Não distingue falta de folga — isso é território de `AusenciaJustificada` |

Dias com número ímpar de batidas caem na Rule #5: `needs_review=True` e saldo congelado
em zero. No relatório real de 01–16/08/2026 isso atinge **38 de 105 dias-funcionário
(36%)** — é o dado real do aparelho, não um defeito da leitura. O comando `conferir`
do programa externo conta quantos são antes do envio.
