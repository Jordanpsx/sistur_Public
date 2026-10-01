# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
BancoDeHorasService — all hour-bank calculations for SISTUR.

All time values are handled in **minutes** (integers) to avoid floating-point
rounding errors in payroll calculations.  Conversion to human-readable strings
is a presentation concern handled by the `formatar_minutos` helper.

References:
    legacy/docs/BANCO_DE_HORAS/
    legacy/TROUBLESHOOTING-BANCO-HORAS.md
    legacy/sql/create-timebank-tables.sql

Arquitetura em camadas: NO Flask imports — pure Python, Cython-compatible.
"""

from __future__ import annotations

from app.services.base import BaseService


class BancoDeHorasService(BaseService):
    """
    Business rules and calculations for the Banco de Horas module.

    All calculation methods are pure functions (static) — they receive
    plain integers and return plain integers.  No ORM, no Flask, no I/O.
    This ensures they can be compiled via Cython without any API change.
    """

    # ------------------------------------------------------------------
    # Core calculations (pure Python — no I/O)
    # ------------------------------------------------------------------

    @staticmethod
    def calcular_saldo_dia(
        minutos_trabalhados: int,
        minutos_esperados: int,
        minutos_almoco_realizado: int,
        minutos_almoco_esperados: int = 60,
    ) -> int:
        """
        Calculate the hour-bank balance for a single workday, in minutes.

        Business rule (CLT / legacy behaviour):
            balance = (minutos_trabalhados - minutos_almoco_realizado)
                      - (minutos_esperados - minutos_almoco_esperados)

        A positive result means overtime credited to the bank.
        A negative result means a deficit debited from the bank.

        Args:
            minutos_trabalhados:        Total clock-in → clock-out span in minutes.
            minutos_esperados:          Employee's daily expected minutes (from
                                        Funcionario.minutos_esperados_dia, e.g. 480).
            minutos_almoco_realizado:   Actual lunch break taken in minutes.
            minutos_almoco_esperados:   Standard lunch break the employee is
                                        entitled to (default 60 min).

        Returns:
            Signed integer representing minutes of credit (+) or deficit (−).

        Examples:
            # Employee worked 9h, took 1h lunch, expected 8h work + 1h lunch
            >>> BancoDeHorasService.calcular_saldo_dia(540, 480, 60, 60)
            60   # +1h credit

            # Employee worked 7.5h, took 30min lunch, expected 8h work + 1h lunch
            >>> BancoDeHorasService.calcular_saldo_dia(450, 480, 30, 60)
            -30  # -30min deficit
        """
        horas_efetivas = minutos_trabalhados - minutos_almoco_realizado
        horas_esperadas_efetivas = minutos_esperados - minutos_almoco_esperados
        return horas_efetivas - horas_esperadas_efetivas

    @staticmethod
    def calcular_saldo_periodo(saldos_diarios: list[int]) -> int:
        """
        Aggregate a list of daily balances into a period total.

        Args:
            saldos_diarios: List of per-day balances in minutes
                            (output of calcular_saldo_dia for each day).

        Returns:
            Total signed balance in minutes for the period.
        """
        return sum(saldos_diarios)

    @staticmethod
    def aplicar_deducao(saldo_atual: int, minutos_deducao: int) -> int:
        """
        Apply a deduction (e.g. approved time-off) to the current balance.

        Args:
            saldo_atual:      Current hour-bank balance in minutes.
            minutos_deducao:  Minutes to deduct (must be positive).

        Returns:
            New balance after deduction.

        Raises:
            ValueError: If minutos_deducao is negative.
        """
        if minutos_deducao < 0:
            raise ValueError(
                "minutos_deducao deve ser positivo. "
                "Use calcular_saldo_dia para créditos/débitos de jornada."
            )
        return saldo_atual - minutos_deducao

    # ------------------------------------------------------------------
    # Presentation helper
    # ------------------------------------------------------------------

    @staticmethod
    def formatar_minutos(minutos: int) -> str:
        """
        Format a signed minute value as a human-readable string.

        Args:
            minutos: Signed integer (positive = credit, negative = deficit).

        Returns:
            String in format  "2h 30min"  or  "-1h 15min".

        Examples:
            >>> BancoDeHorasService.formatar_minutos(150)
            '2h 30min'
            >>> BancoDeHorasService.formatar_minutos(-75)
            '-1h 15min'
            >>> BancoDeHorasService.formatar_minutos(0)
            '0h 00min'
        """
        sinal = "-" if minutos < 0 else ""
        total = abs(minutos)
        horas = total // 60
        mins = total % 60
        return f"{sinal}{horas}h {mins:02d}min"

    # ------------------------------------------------------------------
    # DB-backed queries (stub — will be implemented when PontoEletronico
    # model is ported from legacy/includes/class-sistur-timebank-manager.php)
    # ------------------------------------------------------------------

    @staticmethod
    def obter_saldo_atual(funcionario_id: int) -> int:
        """
        Return the current hour-bank balance for an employee, in minutes.

        TODO: query sistur_timebank_deductions for the latest
              balance_after_minutes for this employee.
              Reference: legacy/sql/create-timebank-tables.sql

        Returns:
            Signed integer balance in minutes.  Returns 0 until the
            PontoEletronico module is ported.
        """
        # Stub — replace with real query after PontoEletronico port
        return 0
