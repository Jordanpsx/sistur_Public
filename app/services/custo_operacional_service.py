# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
CustoOperacionalService — Estimativa de custo com diaristas por período.

Arquitetura em camadas: sem imports do Flask — puro Python, compatível com Cython.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models.escala import EscalaEntrada
from app.models.funcionario import Funcionario
from app.services.base import BaseService


class CustoOperacionalService(BaseService):
    """Calcula o custo com diaristas a partir das entradas de escala registradas."""

    @staticmethod
    def custo_diaristas_periodo(
        data_inicio: date,
        data_fim: date,
        area_id: int | None = None,
    ) -> dict:
        """Calcula o custo total com diaristas em um período.

        Conta os dias de trabalho (tipo == NORMAL) registrados na escala para
        cada funcionário com `tipo_contrato == 'DIARISTA'` e multiplica pelo
        `valor_diaria` do funcionário.

        Args:
            data_inicio: Início do período (inclusivo).
            data_fim: Fim do período (inclusivo).
            area_id: Filtra por área. None = todas.

        Returns:
            Dicionário com:
            - data_inicio (date)
            - data_fim (date)
            - total_dias (int): dias corridos no período
            - linhas (list[dict]): uma entrada por diarista com escala no período
                - funcionario (Funcionario)
                - dias_trabalhados (int): entradas na escala com turno ≠ FOLGA
                - valor_diaria (Decimal)
                - subtotal (Decimal)
            - sem_valor (list[dict]): diaristas com escala no período mas sem
              `valor_diaria` cadastrado — não entram no total e precisam de
              atenção do RH (cada item: funcionario, dias_trabalhados)
            - total_geral (Decimal): soma dos subtotais
        """
        if data_fim < data_inicio:
            raise ValueError("data_fim deve ser igual ou posterior a data_inicio.")

        total_dias = (data_fim - data_inicio).days + 1

        query = (
            db.session.query(Funcionario)
            .filter(
                Funcionario.ativo == True,
                Funcionario.tipo_contrato == "DIARISTA",
            )
            .order_by(Funcionario.nome)
        )
        if area_id:
            query = query.filter(Funcionario.area_id == area_id)

        diaristas = query.all()
        fids = [f.id for f in diaristas]

        if not fids:
            return {
                "data_inicio": data_inicio,
                "data_fim": data_fim,
                "total_dias": total_dias,
                "linhas": [],
                "sem_valor": [],
                "total_geral": Decimal("0.00"),
            }

        # Entradas de escala no período, excluindo FOLGA
        entradas = (
            db.session.query(
                EscalaEntrada.funcionario_id,
                db.func.count(EscalaEntrada.id).label("dias_trab"),
            )
            .filter(
                EscalaEntrada.funcionario_id.in_(fids),
                EscalaEntrada.data >= data_inicio,
                EscalaEntrada.data <= data_fim,
                EscalaEntrada.tipo == "NORMAL",
            )
            .group_by(EscalaEntrada.funcionario_id)
            .all()
        )

        lookup_dias: dict[int, int] = {row.funcionario_id: row.dias_trab for row in entradas}

        linhas = []
        sem_valor = []
        total_geral = Decimal("0.00")

        for f in diaristas:
            dias = lookup_dias.get(f.id, 0)
            if dias == 0:
                continue  # diarista sem escala no período — não entra no relatório
            if f.valor_diaria is None:
                # Trabalhou no período mas não tem diária cadastrada: vira
                # pendência para o RH em vez de sumir silenciosamente do total.
                sem_valor.append({
                    "funcionario": f,
                    "dias_trabalhados": dias,
                })
                continue
            valor = Decimal(str(f.valor_diaria))
            subtotal = valor * dias
            total_geral += subtotal
            linhas.append({
                "funcionario": f,
                "dias_trabalhados": dias,
                "valor_diaria": valor,
                "subtotal": subtotal,
            })

        return {
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "total_dias": total_dias,
            "linhas": linhas,
            "sem_valor": sem_valor,
            "total_geral": total_geral,
        }

    @staticmethod
    def custo_diaristas_mes(
        ano: int,
        mes: int,
        area_id: int | None = None,
    ) -> dict:
        """Atalho para calcular o custo de um mês calendário completo.

        Args:
            ano: Ano (ex.: 2026).
            mes: Mês (1–12).
            area_id: Filtra por área. None = todas.

        Returns:
            Mesmo formato de `custo_diaristas_periodo`.
        """
        inicio = date(ano, mes, 1)
        dias_no_mes = calendar.monthrange(ano, mes)[1]
        fim = date(ano, mes, dias_no_mes)
        return CustoOperacionalService.custo_diaristas_periodo(inicio, fim, area_id)
