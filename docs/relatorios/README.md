# Módulo Relatórios — Central de Relatórios do RH

Prefixo: `/rh/relatorios` · Blueprint: `app/blueprints/relatorios/routes.py` · Service:
`app/services/relatorio_service.py` · Templates: `app/templates/rh/relatorios/`

Catálogo resumido: `docs/api_catalog.md`.

---

## Arquitetura

**Somente leitura, sem migration.** `RelatorioService` não muta nada e não recalcula regra de
negócio: cada builder **reaproveita os services existentes** (`RHService`, `EscalaService`,
`OrganizacaoService`) e devolve um dict pronto para o template.

**Preview e PDF renderizam o mesmo template.** O que aparece na tela é exatamente o que sai no
PDF — não há dois caminhos de renderização para divergirem. Os templates são autocontidos
(CSS inline, sem fontes externas, sem JS, sem rede), exigência do WeasyPrint.

> Os templates de PDF são a **exceção** à regra de `var(--primary)` do design system: usam hex
> fixos porque são documentos impressos, não UI viva. Mesmo padrão do `folha_ponto_pdf.html`.

---

## Permissões (RBAC)

Grupo `relatorios`:

| Permissão | Uso |
|---|---|
| `relatorios.view` | Central + preview em tela |
| `relatorios.view_all` | Remove a restrição de escopo por área/equipe |
| `relatorios.imprimir` | Baixar o PDF (distinto de só pré-visualizar) |

**Escopo:** sem `view_all`, `area_id` fica restrito à própria área do ator e `equipe_id` às
equipes das quais ele é membro (`EquipeMembro`). Fora do escopo → `404`.

---

## Rotas

| Rota | Auth | Papel |
|---|---|---|
| `GET /rh/relatorios/` | `view` | Central: tipo + filtros + período → preview ou PDF |
| `GET /rh/relatorios/preview` | `view` | Renderiza em HTML (não audita — é só visualização) |
| `GET /rh/relatorios/pdf` | `imprimir` | Gera o PDF via WeasyPrint; audita `AuditAction.export` |

`tipo` inválido → `400`. Período inválido (fim antes do início, ou acima de **62 dias**) → flash +
redirect. O teto de 62 dias evita relatório gigante travando o WeasyPrint ou virando exportação
disfarçada.

---

## Tipos de relatório

| Tipo | Conteúdo | Params extras |
|---|---|---|
| `funcionarios_ativos` | Roster do dia: ativo / de folga / escalado | `data`, `area_id` |
| `folgas` | Folgas dia a dia no período | `data_inicio`, `data_fim`, `area_id` |
| `escala` | Escala publicada, agrupada por dia | `data_inicio`, `data_fim`, `area_id`, `equipe_id` |
| `equipe` | Agenda, composição e custo de diaristas da equipe | `equipe_id`, `data_inicio`, `data_fim` |

### `equipe` — nota sobre o custo de diaristas

Calcula por **membros da equipe** (dias `NORMAL` na escala × `valor_diaria`), que é um eixo de
partição diferente do `CustoOperacionalService` (por **área**, 2.5). Por isso é lógica própria
aqui, não reúso — as duas respondem perguntas distintas. Diarista sem `valor_diaria` cadastrado
aparece numa lista à parte, fora do total, em vez de ser silenciosamente somado como zero.

---

## Testes

| Arquivo | Cobre |
|---|---|
| `tests/services/test_relatorio_service.py` | Os quatro builders: happy path, validação de período, escopo por área/equipe, custo de diaristas |
| `tests/blueprints/test_relatorios_routes.py` | Gate de permissão, content-type do PDF, audit de export, guards de escopo |
