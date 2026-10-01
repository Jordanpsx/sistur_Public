# Módulo Organização — Áreas, Funções, Equipes e Timeline

Prefixo: `/rh/organizacao` · Blueprint: `app/blueprints/organizacao/routes.py` · Service:
`app/services/organizacao_service.py` · Models: `app/models/organizacao.py`, `Area` em
`app/core/models.py`

Planning: **2.1** (fundação organizacional) e **2.1b** (equipes dinâmicas + timeline).
Catálogo resumido: `docs/api_catalog.md`.

---

## Modelos

| Tabela | Papel | Migration |
|---|---|---|
| `sistur_areas` | Departamento/área. `color_index` (0–7, nullable) define a cor no design system | `969bbd4eb078` |
| `sistur_funcoes` | Papel operacional num turno/equipe ("Recepção", "Bar", "Salva-vidas") | `7c610bec3808` |
| `sistur_equipes` | Grupo operacional, opcionalmente vinculado a uma área | `7c610bec3808` |
| `sistur_equipe_membros` | N:N Equipe ↔ Funcionario, com `funcao_id` opcional | `7c610bec3808` |
| `sistur_equipe_agendamentos` | Vigência temporal: "esta equipe opera neste período" (2.1b) | `67d6053ca1f9` |

### Duas camadas, de propósito (D3)

- **`Equipe`** é o agrupamento **nomeado** — quem faz parte, com que função.
- **`EquipeAgendamento`** é a alocação **temporal** por cima — equipes são *dinâmicas*, agendadas
  por período (dia/semana/mês/custom), com soft-delete via `deleted_at` (cancelar preserva
  histórico).

A granularidade do agendamento é o **dia**. Turno/horário continua sendo assunto da Escala
(`EscalaEntrada`) — a timeline **cruza** as duas camadas para derivar disponibilidade, sem
duplicar fonte de verdade.

---

## Permissões (RBAC)

| Grupo | Ações |
|---|---|
| `equipes` | `view` · `create` · `edit` · `delete` |
| `funcoes` | `view` · `create` · `edit` · `delete` |

Áreas são administradas sob as mesmas permissões de `equipes` (mesma tela-hub).

---

## Rotas

| Rota | Papel |
|---|---|
| `GET /rh/organizacao/funcoes` · `/nova` · `/<id>/editar` · `/<id>/desativar` | CRUD de funções |
| `GET /rh/organizacao/equipes` · `/nova` · `/<id>/editar` · `/<id>/desativar` | CRUD de equipes + montagem de membros |
| `GET /rh/organizacao/areas` · `/nova` · `/<id>/editar` · `/<id>/desativar` | CRUD de áreas |
| `GET /rh/organizacao/equipes/timeline` | Timeline visual (visões Dia / Semana / Mês) |
| `POST /rh/organizacao/equipes/agendar` | Cria um `EquipeAgendamento` |
| `POST /rh/organizacao/agendamentos/<id>/cancelar` | Soft-delete do agendamento |

Todas usam **soft-delete** (`is_active=False` / `deleted_at`), nunca DELETE físico.

---

## Leave awareness — o que diferencia este módulo

`OrganizacaoService.composicao_no_periodo(equipe_id, inicio, fim)` cruza cada membro ativo com
`RHService.funcionarios_de_folga` em cada dia do intervalo. O resultado é o ponto todo do 2.1b:
um membro de folga/ausência aparece **"na equipe, mas indisponível"** nos dias afetados — não
some da lista.

Retorna `{equipe, dias, membros}`, onde cada membro traz
`indisponibilidades: dict[date, motivo]`.

**Teto de 62 dias por consulta** (`_MAX_DIAS_COMPOSICAO`) — o mesmo limite adotado depois nos
relatórios (2.6).

> **Desempenho (2.9a′):** originalmente esta função chamava `funcionarios_de_folga` **uma vez por
> dia** (3 queries × N dias). Passou a usar `RHService.folgas_no_periodo`, que responde o intervalo
> inteiro em **3 queries**. Se você for adicionar uma checagem por dia aqui, faça o mesmo — a
> equivalência do lote foi provada contra dados reais (61 dias, 121 folgas, zero divergências).

---

## Outros métodos do service que valem conhecer

| Método | Papel |
|---|---|
| `listar_agendamentos(inicio, fim, equipe_id=None)` | Agendamentos que **intersectam** o intervalo, com equipe/área pré-carregadas (joinedload) |
| `equipes_do_dia(data, folgas=None)` | Card "Equipes de hoje" no dashboard RH: disponíveis/total por equipe, com quem está de folga riscado (2.9k) |
| `alertas_disponibilidade(data, equipe_id=None)` | Ao montar equipe: sinaliza folga na data, já alocado em outra equipe ativa, e diarista sem `valor_diaria` (2.9l) |

---

## Cor por área (2.9m)

`Area.color_index` (0–7, nullable) escolhe a cor na paleta categórica do design system
(`.area-chip` / `.area-dot` + classes `.area-cN`). `NULL` = cor automática derivada do id
(`cor_indice` property, `id % 8`).

Ao exibir uma área em qualquer módulo, use `area-chip area-c{{ area.cor_indice }}` — é a linguagem
visual transversal, já adotada em funções, equipes, escala e timeline.

---

## Consumidores

Este módulo é fundação de vários outros — mudanças aqui repercutem:

- **Escala (2.3)** — `EscalaEntrada.equipe_id`
- **Relatórios (2.6)** — o relatório de equipe consome `composicao_no_periodo` e `listar_agendamentos`
- **Custo operacional (2.5)** — diaristas por área

---

## Testes

| Arquivo | Cobre |
|---|---|
| `tests/services/test_organizacao_service.py` | CRUD de funções/equipes/membros, composição no período, leave awareness, alertas de disponibilidade |
| `tests/blueprints/test_organizacao_timeline.py` | Acesso/permissão, as três visões da timeline, agendar e cancelar via POST, breakdown de composição |
