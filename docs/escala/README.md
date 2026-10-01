# Módulo Escala — grade mensal de turnos e folgas

Prefixo: `/rh/escala` · Blueprint: `app/blueprints/escala/routes.py` · Service:
`app/services/escala_service.py` · Model: `app/models/escala.py`

Planning: **2.3** (escala visual + geração automática + presença). Catálogo resumido:
`docs/api_catalog.md`.

---

## Modelo — `EscalaEntrada`

Tabela `sistur_escala_entradas`. Migrations: `418e3488f25b` (criação),
`9e1cd1afe63d` (troca de `turno` por `tipo` + `labels`).

**Uma linha por (funcionario_id, data).** `UniqueConstraint` garante idempotência: upsert via
delete+insert ou update.

| Coluna | Papel |
|---|---|
| `tipo` | **Estado base do dia**: `NORMAL` (trabalha) ou `FOLGA` (descansa). Obrigatório. |
| `labels` | Rótulos de turno **multivalor** (JSON): `MANHA`, `TARDE`, `NOITE`, `PLANTAO`. Um dia pode ter mais de um (quem dobra). Só se aplica a `NORMAL`. |
| `equipe_id` | Equipe do dia (opcional) |
| `observacao` | Texto curto (opcional) |

> **Ausência de linha ≠ folga.** Significa apenas "dia não definido". Só `tipo=FOLGA` é folga
> explícita — a distinção importa em `funcionarios_de_folga` e nos relatórios.

### Divergência deliberada do plano original

O previsto era `Escala` (cabeçalho com status "publicada") + `EscalaTurno` (com
`hora_inicio`/`hora_fim`). O implementado é mais enxuto: **uma linha por funcionário×dia**, sem
horário e sem status de publicação. A meta de presença e o "publicar→notificar" foram construídos
**sobre este modelo**, não sobre o original.

---

## Permissões (RBAC)

Grupo `escala`: `view` · `edit`.

---

## Rotas

| Rota | Auth | Papel |
|---|---|---|
| `GET /rh/escala/` | `view` | Grade mensal (calendário visual) |
| `POST /rh/escala/entrada` | `edit` | Registra/atualiza uma célula |
| `POST /rh/escala/entrada/remover` | `edit` | Remove a entrada (volta a "não definido") |
| `POST /rh/escala/lote` | `edit` | Registro em lote |
| `POST /rh/escala/gerar` | `edit` | Geração automática a partir da `jornada_semanal` |
| `GET /rh/escala/custo` | `view` | Custo operacional do período (2.5) |

---

## Geração automática (`gerar_a_partir_do_padrao`)

Lê a `jornada_semanal` de cada CLT e escala o mês inteiro:

- **dia ativo** na jornada → `NORMAL`, com o rótulo derivado do `horario_entrada_padrao`;
- **dia inativo** → `FOLGA`.

Diaristas não têm jornada recorrente, então não entram na geração — aparecem na escala só por
registro manual.

**Publicar → notificar:** as alterações disparam `AvisoService` — aviso por célula alterada no
registro individual, e **resumo único por funcionário** na geração mensal/lote (senão um mês
gerado viraria 30 avisos por pessoa).

---

## Integração com Ponto e Banco de Horas

A escala **sobrepõe** a jornada contratual nos cálculos de ponto:

- **D7 — escala como override:** `PontoService._get_expected_minutes` consulta a escala antes da
  `jornada_semanal`. Escalado como `FOLGA` num dia útil ⇒ `expected = 0`.
- **D18 — feriado:** feriado do Calendário Global também zera o esperado, e `processar_dia`
  aplica **fator 2×** sobre as horas trabalhadas no feriado.

> Ao mexer em qualquer cálculo de saldo, verifique estes dois caminhos — eles são a razão de
> `expected_minutes_snapshot` existir no `TimeDay`.

---

## Métodos do service que valem conhecer

| Método | Papel |
|---|---|
| `calendario_mes(...)` | Monta a grade visual do mês |
| `registrar(...)` / `registrar_lote(...)` / `remover(...)` | Mutações, todas auditadas |
| `gerar_a_partir_do_padrao(...)` | Geração automática (acima) |
| `presenca_dia(data, area_id=None)` | Cruza escala `NORMAL` × `TimeDay` × primeira batida → status operacional por pessoa: `presente` · `atrasado` · `ausente` · `revisar` · `pendente`. Alimenta o card "Presença de hoje" do dashboard RH |
| `funcionarios_de_folga(data)` | IDs com `tipo=FOLGA` na data — **fonte parcial**; a visão completa é `RHService.funcionarios_de_folga`, que combina escala + ausência justificada + jornada |
| `resumo_mes(funcionario_id, ano, mes)` | Utilitário sem consumo em tela hoje (gap conhecido) |

---

## Consumidores

- **Relatórios (2.6)** — `escala_periodo` e o custo de diaristas do relatório de equipe leem
  `EscalaEntrada` diretamente (dias `NORMAL` × `valor_diaria`).
- **Organização (2.1b)** — a timeline cruza agendamento de equipe com disponibilidade real.
- **RH** — `funcionarios_de_folga` e o painel "Folgas de hoje".

---

## Testes

`tests/services/test_escala_service.py` — registro/remoção idempotentes, geração a partir da
jornada, presença do dia, override D7/D18 no cálculo de esperado.
