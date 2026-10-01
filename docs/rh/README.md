# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

# Módulo: RH — Recursos Humanos

> Blueprint: `rh` — prefixo `/rh`  
> Serviço principal: `app/services/rh_service.py`

---

## Abas do Dashboard

| Aba | URL Param (`aba=`) | Descrição |
|---|---|---|
| Visão Geral | `visao-geral` | Planilha de batidas (matriz) + alertas de revisão |
| Colaboradores | `colaboradores` | Tabela filtrável + ações por funcionário |
| Folha de Ponto | `folha-ponto` | Redireciona para seleção de colaborador |

---

## Permissões RBAC — Módulo `folha_ponto`

| Ação | Descrição |
|---|---|
| `view` | Visualizar a Folha de Ponto de qualquer funcionário |
| `edit` | Adicionar, editar, ou deletar batidas administrativamente |
| `deducao` | Registrar deduções de banco de horas |
| `imprimir` | Gerar e baixar o PDF da Folha de Ponto |

Super admins têm todas as permissões automaticamente.

---

## Folha de Ponto Individual — Rotas

### `GET /rh/folha-ponto/<funcionario_id>`

Exibe a tabela CLT de 10 colunas do funcionário para o mês/ano selecionado.

**Permissão:** `folha_ponto.view`  
**Query params:** `mes` (int, default: mês corrente), `ano` (int, default: ano corrente)  
**Template:** `rh/folha_ponto.html`

---

### `POST /rh/folha-ponto/<funcionario_id>/batida/adicionar`

Adiciona uma nova batida administrativa.

**Permissão:** `folha_ponto.edit`  
**Form fields:**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `novo_horario` | `datetime-local` | ✅ | Data e hora da batida |
| `motivo` | string | ✅ | Justificativa auditada |
| `mes` | int | ✅ | Mês de retorno |
| `ano` | int | ✅ | Ano de retorno |

**Audit:** Cria `AuditLog` via `PontoService.registrar_batida(source="admin")`

---

### `POST /rh/folha-ponto/<funcionario_id>/batida/<entry_id>/editar`

Edita o horário de uma batida existente.

**Permissão:** `folha_ponto.edit`  
**Form fields:** `novo_horario`, `motivo`, `mes`, `ano`  
**Audit:** `PontoService.editar_batida_admin` — registra snapshot before/after.

---

### `POST /rh/folha-ponto/<funcionario_id>/batida/<entry_id>/deletar`

Remove uma batida com auditoria.

**Permissão:** `folha_ponto.edit`  
**Form fields:** `motivo` (obrigatório), `mes`, `ano`  
**Audit:** `PontoService.deletar_batida_admin` — snapshot completo antes da exclusão.

---

### `POST /rh/folha-ponto/<funcionario_id>/deducao`

Registra uma dedução de banco de horas.

**Permissão:** `folha_ponto.deducao`  
**Form fields:**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `deduction_type` | string | ✅ | Enum: `ferias`, `falta_justificada`, `falta_injustificada`, `atestado`, `licenca`, `outro` |
| `minutos` | int | ✅ | Quantidade de minutos a deduzir |
| `data_registro` | date | ✅ | Data da dedução (`YYYY-MM-DD`) |
| `observacao` | string | ❌ | Observação opcional |

**Delegates to:** `PontoService.registrar_abatimento_horas`

---

### `GET /rh/folha-ponto/<funcionario_id>/pdf`

Gera e retorna o PDF da Folha de Ponto via WeasyPrint.

**Permissão:** `folha_ponto.imprimir`  
**Query params:** `mes`, `ano`  
**Response:** `application/pdf` inline, filename `folha_ponto_{cpf}_{ano}-{MM}.pdf`  
**Template:** `rh/folha_ponto_pdf.html` (CSS puro, sem JS, sem fontes externas)  
**Dados da empresa:** Lidos de `sistema_settings` (CNPJ, endereço, nome) — configurados em  
`GET /admin/configuracoes/ → aba Dados da Empresa`

---

## Serviço: `RHService.folha_ponto_mes`

```python
RHService.folha_ponto_mes(funcionario_id: int, ano: int, mes: int) -> dict
```

**Retorna:**

| Chave | Tipo | Descrição |
|---|---|---|
| `funcionario` | `Funcionario` | Instância ORM |
| `linhas` | `list[dict]` | Uma linha por `TimeDay` registrado (10 colunas CLT) |
| `total_minutos_trabalhados` | int | Soma do mês |
| `total_saldo_minutos` | int | Saldo líquido do mês (positivo = banco acumulado) |
| `total_expected_minutos` | int | Soma dos minutos esperados |
| `dias_com_revisao` | int | Contagem de `TimeDay.needs_review = True` |

Usa **2 queries separadas** (TimeDay + TimeEntry) para evitar produto cartesiano  
— cada linha do dict mapeia `clock_in`, `lunch_start`, `lunch_end`, `clock_out` para  
instâncias `TimeEntry | None`.

---

## Dados da Empresa (para PDF)

Configurados em `/admin/configuracoes/ → Dados da Empresa`:

| Chave | System Settings | Descrição |
|---|---|---|
| `empresa.razao_social` | `sistur_system_settings` | Nome oficial exibido no cabeçalho do PDF |
| `empresa.cnpj` | `sistur_system_settings` | CNPJ exibido no cabeçalho do PDF |
| `empresa.endereco` | `sistur_system_settings` | Endereço completo |

Se a razão social não estiver configurada, o template faz fallback para `branding.empresa_nome`.

---

## Folgas em lote — `folgas_no_periodo` (2.9a′)

`RHService.funcionarios_de_folga(data)` combina quatro fontes, na ordem de prioridade do motivo
exibido:

1. **Ausência justificada aprovada** na data (`Atestado` / `Férias` / `Folga justificada`) —
   modelo `AusenciaJustificada`, com fluxo de aprovação próprio (ver Avisos);
2. **Escala** com `tipo=FOLGA` (`Folga (escala)`);
3. **Escala** com `tipo=FALTA_JUSTIFICADA` (`Falta justificada (escala)`) — ausência com
   justificativa por escrito lançada diretamente no quadro de escala (`/rh/escala/`), sem
   passar pelo fluxo de aprovação de `AusenciaJustificada`. As duas tabelas são independentes
   hoje — não há FK entre elas;
4. **Jornada semanal** do CLT inativa naquele dia (`Folga (jornada)`).

Quem tem escala `NORMAL` na data está trabalhando e **nunca aparece** — a escala publicada
sobrepõe a jornada. Diaristas só aparecem por ausência justificada ou folga/falta de escala.

**FALTA_JUSTIFICADA e banco de horas:** `PontoService._get_expected_minutes()` trata
`FOLGA` e `FALTA_JUSTIFICADA` da mesma forma — minutos esperados = 0, então sem
batida o saldo do dia fica zero (sem débito). A diferença entre os dois é só semântica
(descanso planejado vs. ausência não-planejada porém justificada) e de auditoria: registrar
`FALTA_JUSTIFICADA` exige uma `justificativa` por escrito (`EscalaEntrada.justificativa`,
`Text`, pode conter URLs — a UI do calendário reconhece como link clicável).

> **Use `folgas_no_periodo(inicio, fim)` para intervalos.** Custa **3 queries no total**, contra
> 3 **por dia** do laço ingênuo — um relatório de 30 dias caía de 90 para 3 queries. Mesma regra
> de negócio, mesmo resultado (equivalência provada contra dados reais: 61 dias, 121 folgas, zero
> divergências, inclusive na ordem). Consumido por `RelatorioService.folgas_periodo` e
> `OrganizacaoService.composicao_no_periodo`.

---

## Integridade do Banco de Horas (2.9c)

`Funcionario.banco_horas_acumulado` é um **somatório materializado**: toda escrita
(`processar_dia`, débito provisório de ausência, sua reversão, abatimento manual) o ajusta por
**delta**. Uma falha no meio de uma transação — ou uma edição direta no banco — faz o cache
divergir da verdade **sem nenhum sinal**.

**Invariante:**

```
banco_horas_acumulado == Σ TimeDay.saldo_final_minutos − Σ TimeBankDeduction.minutos_abatidos
```

| Peça | Papel |
|---|---|
| `RHService.divergencias_banco_horas(apenas_ativos=True)` | **Detector.** Verifica o invariante para todos os funcionários em **3 queries**, independentemente da quantidade; devolve só quem divergiu, ordenado pela maior diferença absoluta |
| `PontoService.recalcular_banco_global(funcionario_id, ator_id)` | **Corretor** (já existia). Reconstrói o valor do zero e audita |
| Job `integridade_banco_horas` | Cron diário 03h30 → `AvisoService.verificar_integridade_banco_horas()` |
| Painel `/rh/#integridade-banco` | Visível só com `folha_ponto.view`; **só aparece quando há divergência** — condição de exceção, não métrica de rotina |
| `POST /rh/funcionarios/<id>/recalcular-banco` | Correção pela UI. Exige `ponto.edit_admin` — mais restritivo que o `funcionarios.edit` do `reprocessar_ponto`, por reescrever um número financeiro |

**Quem é notificado:** supervisores da área do afetado (regra `avisos.receber_alertas`) **+ todos
os super admins** (drift é problema transversal; o lookup por área sozinho deixaria de fora quem
pode agir). O próprio afetado nunca é notificado — não é ele quem corrige.

**Anti-spam:** se o destinatário já tem um aviso **não lido** sobre o mesmo funcionário, o job não
cria outro. O alerta some sozinho quando a divergência é corrigida.

---

## Desempenho da listagem (2.9a/2.9b — verificado)

Medido por contagem de queries, não por leitura de código:

- `listar_com_filtros` → **2 queries** (já usa `joinedload(area, role)` + `.paginate()`).
- `matriz_batidas_mes` → **3 queries** (pagina por funcionário e agrega as batidas com um único
  `GROUP BY` escopado à página).

Ambos já estavam otimizados; o N+1 real do módulo estava nas folgas dia-a-dia (acima).

---

## Regras de arquitetura aplicadas

- **Auditoria obrigatória** — Toda operação de escrita (add/edit/delete batida, dedução) registra `AuditLog`.
- **Arquitetura em camadas** — Queries de banco ficam em `RHService` e `PontoService`, nunca nos blueprints.
- **Auditoria de ponto** — Edições e exclusões de ponto sempre exigem `motivo` não vazio e salvam snapshot before/after.
