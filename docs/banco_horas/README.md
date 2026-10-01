# Módulo: banco_horas

> **Este README documentava um plano inicial que não é o que está em produção.**
> O motor real do Banco de Horas é o `PontoService` (ver `docs/ponto/README.md` e
> `docs/rh/README.md`) — este arquivo existe só porque `BancoDeHorasService` ainda está no
> repositório como utilitário de cálculo puro, sem uso pelo motor real.

---

## Status real (2026)

`BancoDeHorasService` (`app/services/banco_horas_service.py`) **nunca é chamado fora dos seus
próprios docstrings/testes**. Foi a primeira tentativa de modelar o cálculo, anterior ao
`PontoService`. O motor que efetivamente roda em produção é outro:

| Pergunta | Onde a resposta mora de verdade |
|---|---|
| Saldo diário (trabalhado × esperado × tolerância) | `PontoService.calculate_daily_balance` + `PontoService._apply_tolerance` — `docs/ponto/README.md` |
| Saldo acumulado do colaborador | `Funcionario.banco_horas_acumulado` (coluna cacheada, não recalculada a cada leitura) |
| Recontagem total do zero | `PontoService.recalcular_banco_global(funcionario_id, ator_id)` — soma `TimeDay.saldo_final_minutos` de todos os dias menos `TimeBankDeduction.minutos_abatidos`, sobrescreve `banco_horas_acumulado` |
| Abatimento manual (pagamento/desconto) | `PontoService.registrar_abatimento_horas(...)` — cria `TimeBankDeduction` e subtrai de `banco_horas_acumulado` diretamente |
| Auditoria de divergência entre dias e o total cacheado | `RHService.divergencias_banco_horas` (2.9c) — `docs/rh/README.md` |

`Funcionario.saldo_banco_horas()` (`app/models/funcionario.py:200`) **não chama
`BancoDeHorasService`** — é uma property trivial que lê `self.banco_horas_acumulado`
diretamente.

---

## O que `BancoDeHorasService` ainda oferece

Funções `@staticmethod` puras (sem I/O, sem Flask) que replicam em isolamento a mesma
matemática que `PontoService` já implementa internamente:

| Método | Papel |
|---|---|
| `calcular_saldo_dia(minutos_trabalhados, minutos_esperados, minutos_almoco_realizado, minutos_almoco_esperados=60)` | Saldo do dia. **Duplica** `PontoService.calculate_daily_balance` — não é a fonte usada em produção |
| `calcular_saldo_periodo(saldos_diarios)` | Soma uma lista de saldos |
| `aplicar_deducao(saldo_atual, minutos_deducao)` | Dedução simples, `ValueError` se negativo |
| `formatar_minutos(minutos)` | `"2h 30min"` / `"-1h 15min"` |
| `obter_saldo_atual(funcionario_id)` | **Stub morto** — sempre retorna `0`, nunca é chamado |

Se for reaproveitar algo daqui, confirme primeiro que não é redundante com o que
`PontoService` já calcula — os dois têm a mesma regra de tolerância de 10 min (ver `docs/ponto/README.md`)
implementada duas vezes, de forma independente.

---

## Por que não foi apagado

`calcular_saldo_dia`/`formatar_minutos` são simples, testados e podem servir de referência de
leitura para quem não conhece o `PontoService`. Mas **qualquer feature nova de banco de horas
deve integrar com `PontoService`/`Funcionario.banco_horas_acumulado`**, não com este service —
escrever contra `BancoDeHorasService` produziria um segundo saldo que diverge do real.

---

## Testes

`tests/services/test_banco_horas_service.py` testa só as funções puras acima, isoladas — não
tem cobertura de integração porque não há integração.
