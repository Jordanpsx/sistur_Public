# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes de RelatorioService (2.6, Caminho A) — somente leitura."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db as _db
from app.models.escala import EscalaEntrada
from app.models.funcionario import Funcionario
from app.models.organizacao import Equipe
from app.services.organizacao_service import OrganizacaoService
from app.services.relatorio_service import RelatorioService


def _cpf_unico(seed: int) -> str:
    base = str(10000000000 + seed)
    return base[:11]


class TestFuncionariosAtivosDia:

    def test_happy_path_classifica_escalado_e_sem_escala(self, app, db):
        with app.app_context():
            f1 = Funcionario(nome="Ana", cpf=_cpf_unico(1), ativo=True)
            f2 = Funcionario(nome="Bruno", cpf=_cpf_unico(2), ativo=True)
            _db.session.add_all([f1, f2])
            _db.session.flush()
            hoje = date(2026, 3, 10)
            _db.session.add(EscalaEntrada(funcionario_id=f1.id, data=hoje, tipo="NORMAL"))
            _db.session.commit()

            resultado = RelatorioService.funcionarios_ativos_dia(hoje)

            assert resultado["total_ativos"] == 2
            situacoes = {l["funcionario"].id: l["situacao"] for l in resultado["linhas"]}
            assert situacoes[f1.id] == "Escalado"
            assert situacoes[f2.id] == "Sem escala"

    def test_filtra_por_area(self, app, db):
        with app.app_context():
            from app.core.models import Area

            area = Area(name="Cozinha", slug="cozinha-rel")
            _db.session.add(area)
            _db.session.flush()
            f1 = Funcionario(nome="Ana", cpf=_cpf_unico(3), ativo=True, area_id=area.id)
            f2 = Funcionario(nome="Bruno", cpf=_cpf_unico(4), ativo=True)
            _db.session.add_all([f1, f2])
            _db.session.commit()

            resultado = RelatorioService.funcionarios_ativos_dia(date(2026, 3, 10), area_id=area.id)

            assert resultado["total_ativos"] == 1
            assert resultado["linhas"][0]["funcionario"].id == f1.id


class TestFolgasPeriodo:

    def test_periodo_invalido_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError):
                RelatorioService.folgas_periodo(date(2026, 3, 10), date(2026, 3, 5))

    def test_periodo_acima_do_limite_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="Período máximo"):
                RelatorioService.folgas_periodo(date(2026, 1, 1), date(2026, 12, 31))

    def test_happy_path_agrega_por_dia(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Ana", cpf=_cpf_unico(5), ativo=True)
            _db.session.add(f)
            _db.session.flush()
            dia = date(2026, 3, 10)
            _db.session.add(EscalaEntrada(funcionario_id=f.id, data=dia, tipo="FOLGA"))
            _db.session.commit()

            resultado = RelatorioService.folgas_periodo(dia, dia + timedelta(days=1))

            assert len(resultado["dias"]) == 2
            assert resultado["total_folgas"] == 1
            assert resultado["dias"][0]["folgas"][0]["funcionario"].id == f.id


class TestEscalaPeriodo:

    def test_equipe_nao_encontrada_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrad"):
                RelatorioService.escala_periodo(date(2026, 3, 1), date(2026, 3, 2), equipe_id=99999)

    def test_happy_path_agrupa_entradas_por_dia(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Ana", cpf=_cpf_unico(6), ativo=True)
            _db.session.add(f)
            _db.session.flush()
            dia = date(2026, 3, 10)
            _db.session.add(EscalaEntrada(funcionario_id=f.id, data=dia, tipo="NORMAL", labels=["MANHA"]))
            _db.session.commit()

            resultado = RelatorioService.escala_periodo(dia, dia)

            assert len(resultado["dias"]) == 1
            assert resultado["dias"][0]["entradas"][0]["funcionario"].id == f.id
            assert resultado["dias"][0]["entradas"][0]["labels"] == ["MANHA"]

    def test_filtra_por_equipe(self, app, db):
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe Praia", ator_id=None)
            f_dentro = Funcionario(nome="Ana", cpf=_cpf_unico(7), ativo=True)
            f_fora = Funcionario(nome="Bruno", cpf=_cpf_unico(8), ativo=True)
            _db.session.add_all([f_dentro, f_fora])
            _db.session.flush()
            OrganizacaoService.adicionar_membro(eq.id, f_dentro.id, ator_id=None)
            dia = date(2026, 3, 10)
            _db.session.add(EscalaEntrada(funcionario_id=f_dentro.id, data=dia, tipo="NORMAL"))
            _db.session.add(EscalaEntrada(funcionario_id=f_fora.id, data=dia, tipo="NORMAL"))
            _db.session.commit()

            resultado = RelatorioService.escala_periodo(dia, dia, equipe_id=eq.id)

            ids = {e["funcionario"].id for e in resultado["dias"][0]["entradas"]}
            assert ids == {f_dentro.id}


class TestRelatorioEquipe:

    def test_equipe_nao_encontrada_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrad"):
                RelatorioService.relatorio_equipe(99999, date(2026, 3, 1), date(2026, 3, 2))

    def test_periodo_invalido_levanta_erro(self, app, db):
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe X", ator_id=None)
            with pytest.raises(ValueError):
                RelatorioService.relatorio_equipe(eq.id, date(2026, 3, 10), date(2026, 3, 5))

    def test_happy_path_composicao_e_custo(self, app, db):
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe Diaristas", ator_id=None)
            diarista = Funcionario(
                nome="Carlos", cpf=_cpf_unico(9), ativo=True,
                tipo_contrato="DIARISTA", valor_diaria=Decimal("150.00"),
            )
            sem_valor = Funcionario(
                nome="Duda", cpf=_cpf_unico(10), ativo=True,
                tipo_contrato="DIARISTA", valor_diaria=None,
            )
            _db.session.add_all([diarista, sem_valor])
            _db.session.flush()
            OrganizacaoService.adicionar_membro(eq.id, diarista.id, ator_id=None)
            OrganizacaoService.adicionar_membro(eq.id, sem_valor.id, ator_id=None)

            inicio = date(2026, 3, 1)
            fim = date(2026, 3, 3)
            _db.session.add(EscalaEntrada(funcionario_id=diarista.id, data=inicio, tipo="NORMAL"))
            _db.session.add(EscalaEntrada(funcionario_id=diarista.id, data=inicio + timedelta(days=1), tipo="NORMAL"))
            _db.session.add(EscalaEntrada(funcionario_id=sem_valor.id, data=inicio, tipo="NORMAL"))
            _db.session.commit()

            resultado = RelatorioService.relatorio_equipe(eq.id, inicio, fim)

            assert resultado["composicao"]["equipe"].id == eq.id
            assert len(resultado["composicao"]["membros"]) == 2

            custo = resultado["custo_diaristas"]
            assert len(custo["linhas"]) == 1
            assert custo["linhas"][0]["funcionario"].id == diarista.id
            assert custo["linhas"][0]["dias_trabalhados"] == 2
            assert custo["total_geral"] == Decimal("300.00")
            assert len(custo["sem_valor"]) == 1
            assert custo["sem_valor"][0]["funcionario"].id == sem_valor.id


# ---------------------------------------------------------------------------
# 2.13d — Histórico por colaborador (fecha o D23: "e o que ele fez")
# ---------------------------------------------------------------------------

