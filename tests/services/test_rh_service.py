# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de cobertura para RHService.

Cobre as agregações de leitura (sem mutações) que alimentam o dashboard de RH,
a folha de ponto individual e a planilha mensal de batidas.

Fixtures: `app` + `db` via conftest.py (SQLite in-memory, esquema recriado por teste).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.ponto import (
    DeductionType,
    PunchType,
    TimeBankDeduction,
    TimeDay,
    TimeEntry,
)
from app.services.rh_service import RHService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CPF_BASE = "52998224725"
_CPF_2    = "11144477735"
_CPF_3    = "07699220030"


def _func(nome: str, cpf: str, ativo: bool = True) -> Funcionario:
    f = Funcionario(nome=nome, cpf=cpf, ativo=ativo)
    _db.session.add(f)
    _db.session.flush()
    return f


def _time_day(
    funcionario_id: int,
    shift: date,
    trabalhados: int = 480,
    saldo: int = 0,
    expected: int = 480,
    needs_review: bool = False,
) -> TimeDay:
    td = TimeDay(
        funcionario_id=funcionario_id,
        shift_date=shift,
        minutos_trabalhados=trabalhados,
        saldo_calculado_minutos=saldo,
        expected_minutes_snapshot=expected,
        needs_review=needs_review,
    )
    _db.session.add(td)
    _db.session.flush()
    return td


def _entry(
    funcionario_id: int,
    shift: date,
    punch_type: PunchType,
    hora: int = 8,
) -> TimeEntry:
    punch_time = datetime(shift.year, shift.month, shift.day, hora, 0, tzinfo=timezone.utc)
    e = TimeEntry(
        funcionario_id=funcionario_id,
        punch_time=punch_time,
        shift_date=shift,
        punch_type=punch_type,
    )
    _db.session.add(e)
    _db.session.flush()
    return e


# ---------------------------------------------------------------------------
# resumo_mensal()
# ---------------------------------------------------------------------------

class TestResumoMensal:

    def test_retorna_zeros_em_mes_vazio(self, app, db):
        with app.app_context():
            _func("Ativo", _CPF_BASE)
            res = RHService.resumo_mensal(2026, 1)

        assert res["total_minutos_trabalhados"] == 0
        assert res["total_saldo_minutos"] == 0
        assert res["dias_com_revisao"] == 0
        assert res["total_funcionarios"] == 1

    def test_soma_minutos_corretos(self, app, db):
        with app.app_context():
            f = _func("Joao", _CPF_BASE)
            _time_day(f.id, date(2026, 3, 10), trabalhados=480, saldo=60)
            _time_day(f.id, date(2026, 3, 11), trabalhados=420, saldo=-60)
            res = RHService.resumo_mensal(2026, 3)

        assert res["total_minutos_trabalhados"] == 900
        assert res["total_saldo_minutos"] == 0

    def test_contagem_dias_revisao(self, app, db):
        with app.app_context():
            f = _func("Revisao", _CPF_BASE)
            _time_day(f.id, date(2026, 3, 10), needs_review=True)
            _time_day(f.id, date(2026, 3, 11), needs_review=False)
            _time_day(f.id, date(2026, 3, 12), needs_review=True)
            res = RHService.resumo_mensal(2026, 3)

        assert res["dias_com_revisao"] == 2

    def test_contagem_funcionarios_ativos(self, app, db):
        with app.app_context():
            _func("Ativo1", _CPF_BASE)
            _func("Ativo2", _CPF_2)
            _func("Inativo", _CPF_3, ativo=False)
            res = RHService.resumo_mensal(2026, 3)

        assert res["total_funcionarios"] == 2

    def test_nao_mistura_meses(self, app, db):
        with app.app_context():
            f = _func("Mes", _CPF_BASE)
            _time_day(f.id, date(2026, 2, 28), trabalhados=480)
            _time_day(f.id, date(2026, 3, 1), trabalhados=300)
            res = RHService.resumo_mensal(2026, 2)

        assert res["total_minutos_trabalhados"] == 480

    def test_virada_ano_dezembro(self, app, db):
        with app.app_context():
            f = _func("Dezembro", _CPF_BASE)
            _time_day(f.id, date(2025, 12, 31), trabalhados=480)
            _time_day(f.id, date(2026, 1, 1), trabalhados=300)
            res = RHService.resumo_mensal(2025, 12)

        assert res["total_minutos_trabalhados"] == 480


# ---------------------------------------------------------------------------
# alertas_revisao()
# ---------------------------------------------------------------------------

class TestAlertasRevisao:

    def test_retorna_apenas_needs_review(self, app, db):
        with app.app_context():
            f = _func("Alerta", _CPF_BASE)
            _time_day(f.id, date.today(), needs_review=True)
            _time_day(f.id, date(2026, 1, 5), needs_review=False)
            alertas = RHService.alertas_revisao(janela_dias=30)

        assert len(alertas) == 1
        assert alertas[0]["funcionario_id"] == f.id

    def test_ignora_registros_fora_da_janela(self, app, db):
        from datetime import timedelta
        with app.app_context():
            f = _func("Janela", _CPF_BASE)
            data_velha = date.today() - timedelta(days=31)
            _time_day(f.id, data_velha, needs_review=True)
            alertas = RHService.alertas_revisao(janela_dias=30)

        assert alertas == []

    def test_ignora_funcionarios_inativos(self, app, db):
        with app.app_context():
            f = _func("Inativo", _CPF_BASE, ativo=False)
            _time_day(f.id, date.today(), needs_review=True)
            alertas = RHService.alertas_revisao(janela_dias=30)

        assert alertas == []

    def test_ordenado_por_data_desc(self, app, db):
        with app.app_context():
            f = _func("Ordem", _CPF_BASE)
            _time_day(f.id, date(2026, 3, 10), needs_review=True)
            _time_day(f.id, date(2026, 3, 12), needs_review=True)
            alertas = RHService.alertas_revisao(janela_dias=365)

        datas = [a["shift_date"] for a in alertas]
        assert datas == sorted(datas, reverse=True)


# ---------------------------------------------------------------------------
# listar_com_filtros()
# ---------------------------------------------------------------------------

class TestListarComFiltros:

    def test_status_ativo(self, app, db):
        with app.app_context():
            _func("Ativo", _CPF_BASE)
            _func("Inativo", _CPF_2, ativo=False)
            res = RHService.listar_com_filtros(status="ativo")
            assert res.total == 1
            assert res.items[0].nome == "Ativo"

    def test_status_inativo(self, app, db):
        with app.app_context():
            _func("Ativo", _CPF_BASE)
            _func("Inativo", _CPF_2, ativo=False)
            res = RHService.listar_com_filtros(status="inativo")
            assert res.total == 1
            assert res.items[0].nome == "Inativo"

    def test_status_todos(self, app, db):
        with app.app_context():
            _func("Ativo", _CPF_BASE)
            _func("Inativo", _CPF_2, ativo=False)
            res = RHService.listar_com_filtros(status="todos")
            assert res.total == 2

    def test_busca_por_nome(self, app, db):
        with app.app_context():
            _func("Ana Lima", _CPF_BASE)
            _func("Carlos Brito", _CPF_2)
            res = RHService.listar_com_filtros(q="ana")
            assert res.total == 1
            assert res.items[0].nome == "Ana Lima"

    def test_busca_por_cpf(self, app, db):
        with app.app_context():
            _func("Busca CPF", _CPF_BASE)
            _func("Outro", _CPF_2)
            res = RHService.listar_com_filtros(q=_CPF_BASE[:6])
            assert any(f.cpf == _CPF_BASE for f in res.items)

    def test_ordenado_por_nome(self, app, db):
        with app.app_context():
            _func("Zara", _CPF_BASE)
            _func("Ana", _CPF_2)
            _func("Maria", _CPF_3)
            res = RHService.listar_com_filtros(status="todos")
            nomes = [f.nome for f in res.items]
        assert nomes == sorted(nomes)

    def test_filtro_area_id(self, app, db):
        from app.core.models import Area
        with app.app_context():
            area = Area(name="TI", slug="ti", is_active=True)
            _db.session.add(area)
            _db.session.flush()
            f_com_area = _func("Com Area", _CPF_BASE)
            f_com_area.area_id = area.id
            _db.session.flush()
            _func("Sem Area", _CPF_2)
            res = RHService.listar_com_filtros(area_id=area.id)
            assert res.total == 1
            assert res.items[0].nome == "Com Area"


# ---------------------------------------------------------------------------
# folha_ponto_mes()
# ---------------------------------------------------------------------------

class TestFolhaPontoMes:

    def test_nao_encontrado_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                RHService.folha_ponto_mes(99999, 2026, 3)

    def test_mes_sem_registros_retorna_linhas_vazias(self, app, db):
        with app.app_context():
            f = _func("Sem Ponto", _CPF_BASE)
            res = RHService.folha_ponto_mes(f.id, 2026, 3)

        assert res["linhas"] == []
        assert res["total_minutos_trabalhados"] == 0
        assert res["dias_com_revisao"] == 0

    def test_dia_com_batidas_completas(self, app, db):
        with app.app_context():
            f = _func("Completo", _CPF_BASE)
            d = date(2026, 3, 10)
            _time_day(f.id, d, trabalhados=480, saldo=0, expected=480)
            _entry(f.id, d, PunchType.clock_in, hora=8)
            _entry(f.id, d, PunchType.lunch_start, hora=12)
            _entry(f.id, d, PunchType.lunch_end, hora=13)
            _entry(f.id, d, PunchType.clock_out, hora=17)
            res = RHService.folha_ponto_mes(f.id, 2026, 3)

        assert len(res["linhas"]) == 1
        linha = res["linhas"][0]
        assert linha["clock_in"] is not None
        assert linha["clock_out"] is not None
        assert linha["needs_review"] is False
        assert linha["status"] == "OK"

    def test_dia_com_needs_review(self, app, db):
        with app.app_context():
            f = _func("Impar", _CPF_BASE)
            d = date(2026, 3, 11)
            _time_day(f.id, d, needs_review=True)
            res = RHService.folha_ponto_mes(f.id, 2026, 3)

        assert res["linhas"][0]["needs_review"] is True
        assert res["linhas"][0]["status"] == "Revisar"
        assert res["dias_com_revisao"] == 1

    def test_totais_corretos(self, app, db):
        with app.app_context():
            f = _func("Totais", _CPF_BASE)
            _time_day(f.id, date(2026, 3, 10), trabalhados=480, saldo=60, expected=480)
            _time_day(f.id, date(2026, 3, 11), trabalhados=420, saldo=-60, expected=480)
            res = RHService.folha_ponto_mes(f.id, 2026, 3)

        assert res["total_minutos_trabalhados"] == 900
        assert res["total_saldo_minutos"] == 0
        assert res["total_expected_minutos"] == 960

    def test_folga_compensatoria_aparece_e_e_cobrada_na_folha(self, app, db):
        """Caso relatado (PDF de set/2026): a folga precisa aparecer e custar na folha.

        No banco o dia fica zerado e a jornada sai por um lançamento à parte.
        Sem esta montagem, o PDF mostrava 16/09 como 0h00/+0h00 e o saldo do
        mês +6h59 — como se a folga não tivesse sido descontada.
        """
        from app.models.ponto import DeductionType, TimeBankDeduction

        with app.app_context():
            f = _func("Folga", _CPF_BASE)
            _time_day(f.id, date(2026, 9, 15), trabalhados=779, saldo=419, expected=360)
            _time_day(f.id, date(2026, 9, 16), trabalhados=0, saldo=0, expected=0)
            _db.session.add(TimeBankDeduction(
                funcionario_id=f.id, deduction_type=DeductionType.folga,
                minutos_abatidos=360, data_registro=date(2026, 9, 16),
            ))
            _db.session.commit()
            res = RHService.folha_ponto_mes(f.id, 2026, 9)

        dia_normal, dia_folga = res["linhas"]
        assert dia_normal["folga_compensatoria"] is False
        assert dia_normal["desvio_minutos"] == 419

        assert dia_folga["folga_compensatoria"] is True
        assert dia_folga["folga_minutos"] == 360
        assert dia_folga["esperado_exibido_minutos"] == 360
        assert dia_folga["desvio_minutos"] == -360

        # A conta fecha na própria folha: 12h59 − 12h00 = +0h59
        assert res["total_minutos_trabalhados"] == 779
        assert res["total_expected_minutos"] == 720
        assert res["total_saldo_minutos"] == 59

    def test_folga_estornada_nao_aparece(self, app, db):
        from app.models.ponto import DeductionType, TimeBankDeduction

        with app.app_context():
            f = _func("Estornada", _CPF_BASE)
            _time_day(f.id, date(2026, 9, 16), trabalhados=0, saldo=-350, expected=360)
            original = TimeBankDeduction(
                funcionario_id=f.id, deduction_type=DeductionType.folga,
                minutos_abatidos=360, data_registro=date(2026, 9, 16),
            )
            _db.session.add(original)
            _db.session.flush()
            _db.session.add(TimeBankDeduction(
                funcionario_id=f.id, deduction_type=DeductionType.folga,
                minutos_abatidos=-360, data_registro=date(2026, 9, 16),
                estorno_de_id=original.id,
            ))
            _db.session.commit()
            res = RHService.folha_ponto_mes(f.id, 2026, 9)

        linha = res["linhas"][0]
        assert linha["folga_compensatoria"] is False
        assert linha["desvio_minutos"] == -350
        assert res["total_saldo_minutos"] == -350

    def test_pagamento_nao_entra_na_linha_do_dia(self, app, db):
        """Pagamento em dinheiro é evento do banco, não do dia trabalhado."""
        from app.models.ponto import DeductionType, TimeBankDeduction

        with app.app_context():
            f = _func("Pagamento", _CPF_BASE)
            _time_day(f.id, date(2026, 9, 15), trabalhados=480, saldo=120, expected=360)
            _db.session.add(TimeBankDeduction(
                funcionario_id=f.id, deduction_type=DeductionType.pagamento,
                minutos_abatidos=120, data_registro=date(2026, 9, 15),
            ))
            _db.session.commit()
            res = RHService.folha_ponto_mes(f.id, 2026, 9)

        assert res["linhas"][0]["folga_compensatoria"] is False
        assert res["total_saldo_minutos"] == 120

    def test_virada_de_mes_nao_mistura(self, app, db):
        with app.app_context():
            f = _func("Virada", _CPF_BASE)
            _time_day(f.id, date(2025, 12, 31), trabalhados=480)
            _time_day(f.id, date(2026, 1, 1), trabalhados=300)
            res = RHService.folha_ponto_mes(f.id, 2025, 12)

        assert len(res["linhas"]) == 1
        assert res["linhas"][0]["shift_date"] == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# matriz_batidas_mes()
# ---------------------------------------------------------------------------

class TestMatrizBatidasMes:

    def test_mes_sem_batidas(self, app, db):
        with app.app_context():
            _func("Sem Batida", _CPF_BASE)
            res = RHService.matriz_batidas_mes(2026, 3)

        assert len(res["linhas"]) == 1
        assert res["linhas"][0]["batidas"] == {}

    def test_contagem_por_funcionario_e_dia(self, app, db):
        with app.app_context():
            f = _func("Batidas", _CPF_BASE)
            d = date(2026, 3, 10)
            _entry(f.id, d, PunchType.clock_in, hora=8)
            _entry(f.id, d, PunchType.lunch_start, hora=12)
            res = RHService.matriz_batidas_mes(2026, 3)

        linha = res["linhas"][0]
        assert linha["batidas"].get(10) == 2

    def test_fins_semana_identificados(self, app, db):
        with app.app_context():
            res = RHService.matriz_batidas_mes(2026, 3)

        # março 2026: 7=sáb, 8=dom, 14=sáb, 15=dom, 21=sáb, 22=dom, 28=sáb, 29=dom
        assert 7 in res["fins_semana"]
        assert 8 in res["fins_semana"]
        assert 10 not in res["fins_semana"]

    def test_ultimo_dia_do_mes(self, app, db):
        with app.app_context():
            f = _func("UltimoDia", _CPF_BASE)
            d = date(2026, 3, 31)
            _entry(f.id, d, PunchType.clock_in, hora=8)
            res = RHService.matriz_batidas_mes(2026, 3)

        linha = res["linhas"][0]
        assert linha["batidas"].get(31) == 1
        assert res["dias_no_mes"] == 31


# ---------------------------------------------------------------------------
# funcionarios_de_folga (2.4)
# ---------------------------------------------------------------------------

from app.models.avisos import AusenciaJustificada, TipoAusencia  # noqa: E402
from app.services.escala_service import EscalaService  # noqa: E402

_SEG = date(2026, 6, 1)   # segunda-feira
_SAB = date(2026, 6, 6)   # sábado


def _func_clt(nome, cpf, ativos=("segunda", "terca", "quarta", "quinta", "sexta")):
    todos = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
    jornada = {d: {"ativo": d in ativos, "minutos": 480 if d in ativos else 0, "almoco": 60} for d in todos}
    f = Funcionario(nome=nome, cpf=cpf, ativo=True, tipo_contrato="CLT", jornada_semanal=jornada)
    _db.session.add(f)
    _db.session.flush()
    return f


def _ausencia(fid, data, tipo, aprovado=True):
    a = AusenciaJustificada(funcionario_id=fid, data=data, tipo=tipo, aprovado=aprovado)
    _db.session.add(a)
    _db.session.flush()
    return a


def _por_nome(res, nome):
    return next((r for r in res if r["funcionario"].nome == nome), None)


class TestFuncionariosDeFolga:

    def test_escala_folga_aparece(self, app, db):
        with app.app_context():
            f = _func_clt("Ana", _CPF_BASE)
            EscalaService.registrar(f.id, _SEG, "FOLGA", ator_id=None)
            item = _por_nome(RHService.funcionarios_de_folga(_SEG), "Ana")
        assert item is not None
        assert item["fonte"] == "escala"
        assert item["motivo"] == "Folga (escala)"

    def test_escala_falta_justificada_aparece(self, app, db):
        """Sem esse ramo, a ausência cairia no fallback de jornada e um dia
        útil CLT reapareceria como "deveria estar trabalhando"."""
        with app.app_context():
            f = _func_clt("Diana", _CPF_BASE)
            EscalaService.registrar(
                f.id, _SEG, "FALTA_JUSTIFICADA",
                justificativa="Atestado médico", ator_id=None,
            )
            item = _por_nome(RHService.funcionarios_de_folga(_SEG), "Diana")
        assert item is not None
        assert item["fonte"] == "escala"
        assert item["motivo"] == "Falta justificada (escala)"

    def test_escala_normal_nao_aparece(self, app, db):
        with app.app_context():
            f = _func_clt("Bruno", _CPF_BASE)
            EscalaService.registrar(f.id, _SEG, "NORMAL", ator_id=None)
            res = RHService.funcionarios_de_folga(_SEG)
        assert _por_nome(res, "Bruno") is None

    def test_ausencia_aprovada_aparece(self, app, db):
        with app.app_context():
            f = _func_clt("Carla", _CPF_BASE)
            _ausencia(f.id, _SEG, TipoAusencia.ATESTADO, aprovado=True)
            item = _por_nome(RHService.funcionarios_de_folga(_SEG), "Carla")
        assert item is not None
        assert item["fonte"] == "ausencia"
        assert item["motivo"] == "Atestado"

    def test_ausencia_nao_aprovada_nao_aparece(self, app, db):
        with app.app_context():
            # jornada ativa na segunda + ausência não aprovada → não está de folga
            f = _func_clt("Dora", _CPF_BASE)
            _ausencia(f.id, _SEG, TipoAusencia.ATESTADO, aprovado=False)
            res = RHService.funcionarios_de_folga(_SEG)
        assert _por_nome(res, "Dora") is None

    def test_jornada_inativa_aparece(self, app, db):
        with app.app_context():
            f = _func_clt("Edu", _CPF_BASE)  # seg-sex → sábado inativo
            item = _por_nome(RHService.funcionarios_de_folga(_SAB), "Edu")
        assert item is not None
        assert item["fonte"] == "jornada"
        assert item["motivo"] == "Folga (jornada)"

    def test_jornada_ativa_nao_aparece(self, app, db):
        with app.app_context():
            f = _func_clt("Fabi", _CPF_BASE)  # segunda ativa
            res = RHService.funcionarios_de_folga(_SEG)
        assert _por_nome(res, "Fabi") is None

    def test_escala_normal_sobrepoe_jornada(self, app, db):
        with app.app_context():
            # jornada com segunda INATIVA, mas escalado NORMAL na segunda → trabalhando
            f = _func_clt("Gal", _CPF_BASE, ativos=("terca", "quarta", "quinta", "sexta"))
            EscalaService.registrar(f.id, _SEG, "NORMAL", ator_id=None)
            res = RHService.funcionarios_de_folga(_SEG)
        assert _por_nome(res, "Gal") is None

    def test_ausencia_tem_prioridade_sobre_escala_folga(self, app, db):
        with app.app_context():
            f = _func_clt("Hugo", _CPF_BASE)
            EscalaService.registrar(f.id, _SEG, "FOLGA", ator_id=None)
            _ausencia(f.id, _SEG, TipoAusencia.FERIAS, aprovado=True)
            item = _por_nome(RHService.funcionarios_de_folga(_SEG), "Hugo")
        assert item["fonte"] == "ausencia"
        assert item["motivo"] == "Férias"

    def test_diarista_sem_escala_nao_aparece(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Ivo", cpf=_CPF_BASE, ativo=True, tipo_contrato="DIARISTA")
            _db.session.add(f)
            _db.session.flush()
            res = RHService.funcionarios_de_folga(_SAB)
        assert _por_nome(res, "Ivo") is None

    def test_inativo_nao_aparece(self, app, db):
        with app.app_context():
            f = _func_clt("Joana", _CPF_BASE)
            f.ativo = False
            _db.session.flush()
            EscalaService.registrar(f.id, _SEG, "FOLGA", ator_id=None)
            res = RHService.funcionarios_de_folga(_SEG)
        assert _por_nome(res, "Joana") is None


class TestFolgasNoPeriodo:
    """Versão em lote — deve devolver exatamente o mesmo que o dia-a-dia."""

    def test_equivale_ao_dia_a_dia(self, app, db):
        # Cobre as quatro fontes (ausência, escala folga, escala falta
        # justificada, jornada) no mesmo período.
        with app.app_context():
            ana = _func_clt("Ana", "52998224725")
            bruno = _func_clt("Bruno", "15350946056")
            diana = _func_clt("Diana", "16899854372")
            EscalaService.registrar(ana.id, _SEG, "FOLGA", ator_id=None)
            EscalaService.registrar(bruno.id, _SEG, "NORMAL", ator_id=None)
            EscalaService.registrar(
                diana.id, _SEG, "FALTA_JUSTIFICADA",
                justificativa="Atestado médico", ator_id=None,
            )
            _ausencia(bruno.id, date(2026, 6, 2), TipoAusencia.FERIAS, aprovado=True)

            inicio, fim = _SEG, _SAB  # 1 a 6 de junho (inclui sábado → jornada)
            lote = RHService.folgas_no_periodo(inicio, fim)

            for i in range((fim - inicio).days + 1):
                dia = inicio + timedelta(days=i)
                esperado = [
                    (x["funcionario"].id, x["motivo"], x["fonte"])
                    for x in RHService.funcionarios_de_folga(dia)
                ]
                obtido = [
                    (x["funcionario"].id, x["motivo"], x["fonte"]) for x in lote[dia]
                ]
                assert obtido == esperado, f"divergência em {dia}"

    def test_cobre_todos_os_dias_do_periodo(self, app, db):
        with app.app_context():
            _func_clt("Ana", "52998224725")
            lote = RHService.folgas_no_periodo(_SEG, _SAB)
        assert len(lote) == 6
        assert set(lote.keys()) == {_SEG + timedelta(days=i) for i in range(6)}

    def test_dia_sem_folga_vem_como_lista_vazia(self, app, db):
        with app.app_context():
            _func_clt("Ana", "52998224725")  # seg-sex ativos
            lote = RHService.folgas_no_periodo(_SEG, _SEG)
        assert lote[_SEG] == []

    def test_periodo_de_um_dia_unico(self, app, db):
        with app.app_context():
            f = _func_clt("Ana", "52998224725")
            EscalaService.registrar(f.id, _SEG, "FOLGA", ator_id=None)
            lote = RHService.folgas_no_periodo(_SEG, _SEG)
        assert len(lote) == 1
        assert lote[_SEG][0]["motivo"] == "Folga (escala)"

    def test_nao_vaza_dias_fora_do_periodo(self, app, db):
        # Folga registrada FORA do intervalo não pode contaminar o resultado.
        with app.app_context():
            f = _func_clt("Ana", "52998224725")
            EscalaService.registrar(f.id, _SAB, "FOLGA", ator_id=None)
            lote = RHService.folgas_no_periodo(_SEG, _SEG)
        assert lote == {_SEG: []}


class TestDivergenciasBancoHoras:
    """2.9c — detector de drift do somatório materializado `banco_horas_acumulado`."""

    def _func_com_banco(self, nome, cpf, acumulado):
        f = Funcionario(nome=nome, cpf=cpf, ativo=True, banco_horas_acumulado=acumulado)
        _db.session.add(f)
        _db.session.flush()
        return f

    def test_sem_divergencia_retorna_vazio(self, app, db):
        with app.app_context():
            f = self._func_com_banco("Ana", "52998224725", 120)
            td = _time_day(f.id, _SEG, saldo=120)
            td.saldo_final_minutos = 120
            _db.session.flush()
            assert RHService.divergencias_banco_horas() == []

    def test_detecta_cache_maior_que_o_real(self, app, db):
        with app.app_context():
            # cache diz 500, mas os dias somam só 120 → drift de +380
            f = self._func_com_banco("Bruno", "52998224725", 500)
            td = _time_day(f.id, _SEG, saldo=120)
            td.saldo_final_minutos = 120
            _db.session.flush()
            div = RHService.divergencias_banco_horas()

        assert len(div) == 1
        assert div[0]["funcionario"].id == f.id
        assert div[0]["acumulado"] == 500
        assert div[0]["esperado"] == 120
        assert div[0]["diferenca"] == 380

    def test_desconta_abatimentos_do_esperado(self, app, db):
        with app.app_context():
            # 480 de saldo − 60 abatidos = 420 esperado; cache correto → sem divergência
            f = self._func_com_banco("Carla", "52998224725", 420)
            td = _time_day(f.id, _SEG, saldo=480)
            td.saldo_final_minutos = 480
            _db.session.add(TimeBankDeduction(
                funcionario_id=f.id,
                deduction_type=DeductionType.folga,
                minutos_abatidos=60,
                data_registro=_SEG,
            ))
            _db.session.flush()
            assert RHService.divergencias_banco_horas() == []

    def test_funcionario_sem_timeday_com_cache_nao_zero_diverge(self, app, db):
        with app.app_context():
            # nenhum TimeDay → esperado 0; cache 90 é drift puro
            f = self._func_com_banco("Dora", "52998224725", 90)
            div = RHService.divergencias_banco_horas()
        assert len(div) == 1
        assert div[0]["esperado"] == 0
        assert div[0]["diferenca"] == 90

    def test_ordena_pela_maior_diferenca_absoluta(self, app, db):
        with app.app_context():
            self._func_com_banco("Ana", "52998224725", 10)     # +10
            self._func_com_banco("Bruno", "15350946056", -300)  # -300 (maior em módulo)
            self._func_com_banco("Carla", "64792940557", 50)    # +50
            div = RHService.divergencias_banco_horas()

        assert [d["diferenca"] for d in div] == [-300, 50, 10]

    def test_inativo_ignorado_por_padrao_mas_incluido_sob_demanda(self, app, db):
        with app.app_context():
            f = self._func_com_banco("Joana", "52998224725", 75)
            f.ativo = False
            _db.session.flush()
            assert RHService.divergencias_banco_horas() == []
            todos = RHService.divergencias_banco_horas(apenas_ativos=False)
        assert len(todos) == 1
        assert todos[0]["funcionario"].id == f.id

    def test_recalcular_banco_global_zera_a_divergencia(self, app, db):
        # Integração com o corretor já existente (PontoService).
        from app.services.ponto_service import PontoService

        with app.app_context():
            f = self._func_com_banco("Bruno", "52998224725", 500)
            td = _time_day(f.id, _SEG, saldo=120)
            td.saldo_final_minutos = 120
            _db.session.flush()
            assert len(RHService.divergencias_banco_horas()) == 1

            PontoService.recalcular_banco_global(f.id, ator_id=None)

            assert RHService.divergencias_banco_horas() == []
            assert db.session.get(Funcionario, f.id).banco_horas_acumulado == 120
