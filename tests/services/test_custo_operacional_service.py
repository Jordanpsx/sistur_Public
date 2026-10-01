# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes para CustoOperacionalService."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.services.custo_operacional_service import CustoOperacionalService
from app.services.escala_service import EscalaService


def _func_diarista(nome: str, cpf: str, valor_diaria: float) -> Funcionario:
    f = Funcionario(nome=nome, cpf=cpf, ativo=True, tipo_contrato="DIARISTA",
                    valor_diaria=Decimal(str(valor_diaria)))
    _db.session.add(f)
    _db.session.flush()
    return f


def _func_clt(nome: str = "CLT Worker", cpf: str = "52998224725") -> Funcionario:
    f = Funcionario(nome=nome, cpf=cpf, ativo=True, tipo_contrato="CLT")
    _db.session.add(f)
    _db.session.flush()
    return f


_INICIO = date(2026, 6, 1)
_FIM = date(2026, 6, 30)


class TestCustoDiaristasPeriodo:

    def test_sem_diaristas_retorna_zero(self, app, db):
        with app.app_context():
            _func_clt()
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["total_geral"] == Decimal("0.00")
        assert r["linhas"] == []

    def test_calcula_subtotal_corretamente(self, app, db):
        with app.app_context():
            f = _func_diarista("Ana", "52998224725", 120.00)
            # 3 dias de trabalho
            for d in [1, 2, 3]:
                EscalaService.registrar(f.id, date(2026, 6, d), "NORMAL", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
            linha = r["linhas"][0]
        assert linha["dias_trabalhados"] == 3
        assert linha["valor_diaria"] == Decimal("120.00")
        assert linha["subtotal"] == Decimal("360.00")
        assert r["total_geral"] == Decimal("360.00")

    def test_folga_nao_conta_como_dia_trabalhado(self, app, db):
        with app.app_context():
            f = _func_diarista("Ana", "52998224725", 100.00)
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            EscalaService.registrar(f.id, date(2026, 6, 2), "FOLGA", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["linhas"][0]["dias_trabalhados"] == 1

    def test_diarista_sem_escala_nao_entra(self, app, db):
        with app.app_context():
            _func_diarista("Sem Escala", "52998224725", 80.00)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["linhas"] == []
        assert r["total_geral"] == Decimal("0.00")

    def test_clt_ignorado(self, app, db):
        with app.app_context():
            f = _func_clt()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["linhas"] == []

    def test_multiplos_diaristas(self, app, db):
        with app.app_context():
            f1 = _func_diarista("Ana", "52998224725", 100.00)
            f2 = _func_diarista("Bia", "61098798085", 200.00)
            EscalaService.registrar(f1.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            EscalaService.registrar(f2.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            EscalaService.registrar(f2.id, date(2026, 6, 2), "NORMAL", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert len(r["linhas"]) == 2
        assert r["total_geral"] == Decimal("500.00")  # 100 + 400

    def test_data_fim_menor_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="posterior"):
                CustoOperacionalService.custo_diaristas_periodo(_FIM, _INICIO)

    def test_total_dias_calculado(self, app, db):
        with app.app_context():
            r = CustoOperacionalService.custo_diaristas_periodo(
                date(2026, 6, 1), date(2026, 6, 10)
            )
        assert r["total_dias"] == 10  # 1..10 inclusive

    def test_diarista_sem_valor_vira_pendencia(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Sem Diária", cpf="52998224725", ativo=True,
                            tipo_contrato="DIARISTA", valor_diaria=None)
            _db.session.add(f)
            _db.session.flush()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            EscalaService.registrar(f.id, date(2026, 6, 2), "NORMAL", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["linhas"] == []                 # não entra no cálculo
        assert r["total_geral"] == Decimal("0.00")
        assert len(r["sem_valor"]) == 1          # mas aparece como pendência
        assert r["sem_valor"][0]["dias_trabalhados"] == 2

    def test_sem_valor_so_aparece_com_escala(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Sem Diária Sem Escala", cpf="52998224725", ativo=True,
                            tipo_contrato="DIARISTA", valor_diaria=None)
            _db.session.add(f)
            _db.session.flush()
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["sem_valor"] == []

    def test_diarista_inativo_ignorado(self, app, db):
        with app.app_context():
            f = _func_diarista("Inativo", "52998224725", 100.00)
            f.ativo = False
            _db.session.flush()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_periodo(_INICIO, _FIM)
        assert r["linhas"] == []


class TestCustoDiaristasMes:

    def test_abrange_mes_completo(self, app, db):
        with app.app_context():
            f = _func_diarista("Ana", "52998224725", 50.00)
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            EscalaService.registrar(f.id, date(2026, 6, 30), "NORMAL", ator_id=None)
            r = CustoOperacionalService.custo_diaristas_mes(2026, 6)
        assert r["total_dias"] == 30
        assert r["linhas"][0]["dias_trabalhados"] == 2
        assert r["total_geral"] == Decimal("100.00")
