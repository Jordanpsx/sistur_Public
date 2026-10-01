# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes para EscalaService.

Cobre: registrar (upsert, tipo + labels), remover, funcionarios_de_folga,
calendario_mes, resumo_mes, registrar_lote, gerar_a_partir_do_padrao.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.core.models import AuditLog, AuditAction
from app.extensions import db as _db
from app.models.escala import EscalaEntrada
from app.models.funcionario import Funcionario
from app.models.ponto import TimeDay, TimeEntry
from app.services.escala_service import EscalaService

_DATA = date(2026, 6, 15)


def _func(nome: str = "Colaborador", cpf: str = "52998224725") -> Funcionario:
    f = Funcionario(nome=nome, cpf=cpf, ativo=True)
    _db.session.add(f)
    _db.session.flush()
    return f


def _func_com_jornada(
    nome: str = "CLT",
    cpf: str = "52998224725",
    horario=time(8, 0),
    dias_ativos=("segunda", "terca", "quarta", "quinta", "sexta"),
) -> Funcionario:
    """Cria um CLT com jornada semanal nos dias indicados (resto folga)."""
    todos = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
    jornada = {
        dia: {"ativo": dia in dias_ativos, "minutos": 480 if dia in dias_ativos else 0, "almoco": 60}
        for dia in todos
    }
    f = Funcionario(nome=nome, cpf=cpf, ativo=True, tipo_contrato="CLT",
                    jornada_semanal=jornada, horario_entrada_padrao=horario)
    _db.session.add(f)
    _db.session.flush()
    return f


# ---------------------------------------------------------------------------
# registrar — upsert
# ---------------------------------------------------------------------------

class TestRegistrar:

    def test_cria_nova_entrada(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            assert e.id is not None
            assert e.tipo == "NORMAL"
            assert e.labels == ["MANHA"]
            assert e.data == _DATA

    def test_audit_create(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "FOLGA", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=e.id, module="escala"
            ).one()
            assert log.new_state["tipo"] == "FOLGA"

    def test_atualiza_existente(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            e2 = EscalaService.registrar(f.id, _DATA, "NORMAL", ["TARDE"], ator_id=None)
            assert e2.labels == ["TARDE"]
            assert _db.session.query(EscalaEntrada).filter_by(
                funcionario_id=f.id, data=_DATA
            ).count() == 1

    def test_audit_update_na_atualizacao(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            EscalaService.registrar(f.id, _DATA, "FOLGA", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=e.id, module="escala"
            ).one()
            assert log.previous_state["tipo"] == "NORMAL"
            assert log.new_state["tipo"] == "FOLGA"

    def test_tipo_invalido_levanta(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="inválido"):
                EscalaService.registrar(f.id, _DATA, "INVALIDO", ator_id=None)

    def test_label_invalido_levanta(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="inválido"):
                EscalaService.registrar(f.id, _DATA, "NORMAL", ["XPTO"], ator_id=None)

    def test_folga_ignora_labels(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "FOLGA", ["MANHA", "TARDE"], ator_id=None)
            assert e.labels == []

    def test_falta_justificada_exige_justificativa(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="justificativa"):
                EscalaService.registrar(f.id, _DATA, "FALTA_JUSTIFICADA", ator_id=None)

    def test_falta_justificada_com_texto_vazio_levanta(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="justificativa"):
                EscalaService.registrar(
                    f.id, _DATA, "FALTA_JUSTIFICADA", justificativa="   ", ator_id=None,
                )

    def test_falta_justificada_happy_path(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(
                f.id, _DATA, "FALTA_JUSTIFICADA",
                justificativa="Atestado médico anexado: https://exemplo.com/atestado.pdf",
                ator_id=None,
            )
            assert e.tipo == "FALTA_JUSTIFICADA"
            assert e.justificativa == "Atestado médico anexado: https://exemplo.com/atestado.pdf"
            assert e.labels == []

    def test_falta_justificada_ignora_labels(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(
                f.id, _DATA, "FALTA_JUSTIFICADA", ["MANHA"],
                justificativa="Consulta médica", ator_id=None,
            )
            assert e.labels == []

    def test_audit_registra_justificativa(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(
                f.id, _DATA, "FALTA_JUSTIFICADA",
                justificativa="Consulta médica agendada", ator_id=None,
            )
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=e.id, module="escala"
            ).one()
            assert log.new_state["justificativa"] == "Consulta médica agendada"

    def test_multilabel_dois_turnos(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA", "TARDE"], ator_id=None)
            assert e.labels == ["MANHA", "TARDE"]

    def test_funcionario_nao_encontrado_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                EscalaService.registrar(99999, _DATA, "NORMAL", ator_id=None)

    def test_normaliza_para_maiusculo(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "normal", ["manha"], ator_id=None)
            assert e.tipo == "NORMAL"
            assert e.labels == ["MANHA"]


# ---------------------------------------------------------------------------
# remover
# ---------------------------------------------------------------------------

class TestRemover:

    def test_remove_entrada(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ator_id=None)
            EscalaService.remover(f.id, _DATA, ator_id=None)
            assert _db.session.query(EscalaEntrada).filter_by(
                funcionario_id=f.id, data=_DATA
            ).first() is None

    def test_audit_delete(self, app, db):
        with app.app_context():
            f = _func()
            e = EscalaService.registrar(f.id, _DATA, "NORMAL", ["TARDE"], ator_id=None)
            eid = e.id
            EscalaService.remover(f.id, _DATA, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.delete, entity_id=eid, module="escala"
            ).one()
            assert log.previous_state["tipo"] == "NORMAL"

    def test_nao_encontrada_levanta(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="não encontrada"):
                EscalaService.remover(f.id, _DATA, ator_id=None)


# ---------------------------------------------------------------------------
# funcionarios_de_folga (2.4)
# ---------------------------------------------------------------------------

class TestFuncionariosDeFolga:

    def test_retorna_ids_com_folga(self, app, db):
        with app.app_context():
            f1 = _func("F1", "52998224725")
            f2 = _func("F2", "61098798085")
            f1_id, f2_id = f1.id, f2.id
            EscalaService.registrar(f1_id, _DATA, "FOLGA", ator_id=None)
            EscalaService.registrar(f2_id, _DATA, "NORMAL", ator_id=None)
            ids = EscalaService.funcionarios_de_folga(_DATA)
        assert f1_id in ids
        assert f2_id not in ids

    def test_data_sem_folgas_retorna_vazio(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ["TARDE"], ator_id=None)
            ids = EscalaService.funcionarios_de_folga(_DATA)
        assert ids == []

    def test_data_sem_nenhum_registro(self, app, db):
        with app.app_context():
            ids = EscalaService.funcionarios_de_folga(_DATA)
        assert ids == []

    def test_varios_em_folga_mesma_data(self, app, db):
        with app.app_context():
            f1 = _func("F1", "52998224725")
            f2 = _func("F2", "61098798085")
            f3 = _func("F3", "34578814722")
            EscalaService.registrar(f1.id, _DATA, "FOLGA", ator_id=None)
            EscalaService.registrar(f2.id, _DATA, "FOLGA", ator_id=None)
            EscalaService.registrar(f3.id, _DATA, "NORMAL", ["NOITE"], ator_id=None)
            ids = EscalaService.funcionarios_de_folga(_DATA)
            f1_id, f2_id, f3_id = f1.id, f2.id, f3.id
        assert f1_id in ids
        assert f2_id in ids
        assert f3_id not in ids


# ---------------------------------------------------------------------------
# calendario_mes
# ---------------------------------------------------------------------------

class TestCalendarioMes:

    def test_retorna_estrutura_basica(self, app, db):
        with app.app_context():
            _func()
            cal = EscalaService.calendario_mes(2026, 6)
            assert cal["dias_no_mes"] == 30
            assert len(cal["fins_semana"]) > 0
            assert isinstance(cal["linhas"], list)
            assert isinstance(cal["totais"], dict)
            assert isinstance(cal["totais_labels"], dict)

    def test_linha_tem_todos_os_dias(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            cal = EscalaService.calendario_mes(2026, 6)
            linha = next(l for l in cal["linhas"] if l["funcionario"].id == f.id)
        assert len(linha["dias"]) == 30
        assert linha["dias"][1] is not None
        assert linha["dias"][2] is None

    def test_totais_contados_corretamente(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ["MANHA"], ator_id=None)
            EscalaService.registrar(f.id, date(2026, 6, 2), "FOLGA", ator_id=None)
            EscalaService.registrar(
                f.id, date(2026, 6, 3), "FALTA_JUSTIFICADA",
                justificativa="Consulta médica", ator_id=None,
            )
            cal = EscalaService.calendario_mes(2026, 6)
        assert cal["totais"]["NORMAL"] == 1
        assert cal["totais"]["FOLGA"] == 1
        assert cal["totais"]["FALTA_JUSTIFICADA"] == 1
        assert cal["totais_labels"]["MANHA"] == 1


# ---------------------------------------------------------------------------
# registrar_lote
# ---------------------------------------------------------------------------

class TestRegistrarLote:

    def test_cria_multiplas_entradas(self, app, db):
        with app.app_context():
            f1 = _func("F1", "52998224725")
            f2 = _func("F2", "61098798085")
            datas = [date(2026, 6, 1), date(2026, 6, 2)]
            count = EscalaService.registrar_lote([f1.id, f2.id], datas, "NORMAL", ["TARDE"], ator_id=None)
        assert count == 4  # 2 funcs × 2 datas

    def test_upsert_no_lote(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ["MANHA"], ator_id=None)
            EscalaService.registrar_lote([f.id], [date(2026, 6, 1)], "FOLGA", ator_id=None)
            e = _db.session.query(EscalaEntrada).filter_by(
                funcionario_id=f.id, data=date(2026, 6, 1)
            ).one()
        assert e.tipo == "FOLGA"

    def test_lote_falta_justificada_aplica_mesmo_texto_a_todas_as_datas(self, app, db):
        with app.app_context():
            f = _func()
            datas = [date(2026, 6, 1), date(2026, 6, 2)]
            EscalaService.registrar_lote(
                [f.id], datas, "FALTA_JUSTIFICADA",
                justificativa="Atestado de 2 dias", ator_id=None,
            )
            entradas = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f.id).all()
        assert len(entradas) == 2
        assert all(e.tipo == "FALTA_JUSTIFICADA" for e in entradas)
        assert all(e.justificativa == "Atestado de 2 dias" for e in entradas)

    def test_lote_falta_justificada_sem_texto_levanta(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="justificativa"):
                EscalaService.registrar_lote(
                    [f.id], [date(2026, 6, 1)], "FALTA_JUSTIFICADA", ator_id=None,
                )


# ---------------------------------------------------------------------------
# Reprocessamento do Ponto ao lançar FALTA_JUSTIFICADA
# ---------------------------------------------------------------------------

def _time_day_com_debito_provisorio(funcionario_id: int, data: date,
                                     expected: int = 480, saldo: int = -480) -> TimeDay:
    """Simula o estado deixado por AvisoService.verificar_atrasos(): débito
    provisório aplicado, sem batidas reais."""
    td = TimeDay(
        funcionario_id=funcionario_id,
        shift_date=data,
        expected_minutes_snapshot=expected,
        tolerance_snapshot=10,
        minutos_trabalhados=0,
        saldo_calculado_minutos=saldo,
        saldo_final_minutos=saldo,
        auto_debit_aplicado=True,
        needs_review=False,
    )
    _db.session.add(td)
    _db.session.flush()
    return td


class TestReprocessamentoAoJustificar:

    def test_falta_justificada_anula_debito_provisorio(self, app, db):
        """Débito provisório já aplicado (verificar_atrasos) some na hora do
        lançamento da justificativa — não precisa esperar batida real."""
        with app.app_context():
            f = _func()
            f.banco_horas_acumulado = -480
            _db.session.flush()
            _time_day_com_debito_provisorio(f.id, _DATA)

            EscalaService.registrar(
                f.id, _DATA, "FALTA_JUSTIFICADA",
                justificativa="Atestado médico", ator_id=None,
            )

            td = _db.session.query(TimeDay).filter_by(
                funcionario_id=f.id, shift_date=_DATA
            ).one()
            f_fresh = _db.session.get(Funcionario, f.id)
        assert td.auto_debit_aplicado is False
        assert td.saldo_final_minutos == 0
        assert td.expected_minutes_snapshot == 0
        assert f_fresh.banco_horas_acumulado == 0

    def test_falta_justificada_sem_time_day_existente_cria_zerado(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(
                f.id, _DATA, "FALTA_JUSTIFICADA",
                justificativa="Consulta médica", ator_id=None,
            )
            td = _db.session.query(TimeDay).filter_by(
                funcionario_id=f.id, shift_date=_DATA
            ).one()
        assert td.expected_minutes_snapshot == 0
        assert td.saldo_final_minutos == 0
        assert td.auto_debit_aplicado is False

    def test_atualizar_para_falta_justificada_tambem_reprocessa(self, app, db):
        """Editar um dia já existente (NORMAL → FALTA_JUSTIFICADA) reprocessa
        do mesmo jeito que criar direto como FALTA_JUSTIFICADA."""
        with app.app_context():
            f = _func()
            f.banco_horas_acumulado = -480
            _db.session.flush()
            _time_day_com_debito_provisorio(f.id, _DATA)
            EscalaService.registrar(f.id, _DATA, "NORMAL", ator_id=None)

            EscalaService.registrar(
                f.id, _DATA, "FALTA_JUSTIFICADA",
                justificativa="Atestado entregue depois", ator_id=None,
            )

            td = _db.session.query(TimeDay).filter_by(
                funcionario_id=f.id, shift_date=_DATA
            ).one()
        assert td.auto_debit_aplicado is False
        assert td.saldo_final_minutos == 0

    def test_normal_nao_reprocessa_ponto(self, app, db):
        with app.app_context(), patch(
            "app.services.ponto_service.PontoService.processar_dia"
        ) as mock_proc:
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ator_id=None)
        mock_proc.assert_not_called()

    def test_folga_nao_reprocessa_ponto(self, app, db):
        with app.app_context(), patch(
            "app.services.ponto_service.PontoService.processar_dia"
        ) as mock_proc:
            f = _func()
            EscalaService.registrar(f.id, _DATA, "FOLGA", ator_id=None)
        mock_proc.assert_not_called()

    def test_lote_reprocessa_todas_as_datas(self, app, db):
        with app.app_context():
            f = _func()
            f.banco_horas_acumulado = -960
            _db.session.flush()
            d1, d2 = date(2026, 6, 15), date(2026, 6, 16)
            _time_day_com_debito_provisorio(f.id, d1)
            _time_day_com_debito_provisorio(f.id, d2)

            EscalaService.registrar_lote(
                [f.id], [d1, d2], "FALTA_JUSTIFICADA",
                justificativa="Atestado de 2 dias", ator_id=None,
            )

            tds = _db.session.query(TimeDay).filter_by(funcionario_id=f.id).all()
            f_fresh = _db.session.get(Funcionario, f.id)
        assert len(tds) == 2
        assert all(td.auto_debit_aplicado is False for td in tds)
        assert all(td.saldo_final_minutos == 0 for td in tds)
        assert f_fresh.banco_horas_acumulado == 0


# ---------------------------------------------------------------------------
# resumo_mes
# ---------------------------------------------------------------------------

class TestResumoMes:

    def test_conta_por_tipo(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, date(2026, 6, 1), "NORMAL", ator_id=None)
            EscalaService.registrar(f.id, date(2026, 6, 2), "NORMAL", ator_id=None)
            EscalaService.registrar(f.id, date(2026, 6, 3), "FOLGA", ator_id=None)
            res = EscalaService.resumo_mes(f.id, 2026, 6)
        assert res["NORMAL"] == 2
        assert res["FOLGA"] == 1


# ---------------------------------------------------------------------------
# gerar_a_partir_do_padrao
# ---------------------------------------------------------------------------

class TestGerarDoPadrao:

    def test_gera_normal_nos_dias_uteis_e_folga_no_fim_de_semana(self, app, db):
        with app.app_context():
            f = _func_com_jornada(horario=time(8, 0))
            res = EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            assert res["total_funcionarios"] == 1
            assert res["criados"] == 30
            entradas = {
                e.data.day: e
                for e in _db.session.query(EscalaEntrada).filter_by(funcionario_id=f.id)
            }
            for dia, e in entradas.items():
                weekday = date(2026, 6, dia).weekday()
                if weekday < 5:
                    assert e.tipo == "NORMAL"
                    assert e.labels == ["MANHA"]  # horário 8h → MANHA
                else:
                    assert e.tipo == "FOLGA"
                    assert e.labels == []

    def test_label_derivado_do_horario(self, app, db):
        with app.app_context():
            f_tarde = _func_com_jornada("Tarde", "61098798085", horario=time(13, 0))
            f_noite = _func_com_jornada("Noite", "34578814722", horario=time(19, 0))
            f_sem = _func_com_jornada("SemH", "47403284680", horario=None)
            EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            # 2026-06-01 é segunda-feira (dia útil)
            e_tarde = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f_tarde.id, data=date(2026, 6, 1)).one()
            e_noite = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f_noite.id, data=date(2026, 6, 1)).one()
            e_sem = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f_sem.id, data=date(2026, 6, 1)).one()
        assert e_tarde.labels == ["TARDE"]
        assert e_noite.labels == ["NOITE"]
        assert e_sem.labels == []  # sem horário → sem rótulo

    def test_nao_sobrescreve_por_padrao(self, app, db):
        with app.app_context():
            f = _func_com_jornada()
            # 2026-06-01 (segunda, dia útil) marcado manualmente como FOLGA
            EscalaService.registrar(f.id, date(2026, 6, 1), "FOLGA", ator_id=None)
            res = EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            e = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f.id, data=date(2026, 6, 1)).one()
        assert e.tipo == "FOLGA"        # mantido
        assert res["pulados"] >= 1

    def test_sobrescrever_substitui(self, app, db):
        with app.app_context():
            f = _func_com_jornada()
            EscalaService.registrar(f.id, date(2026, 6, 1), "FOLGA", ator_id=None)
            res = EscalaService.gerar_a_partir_do_padrao(2026, 6, sobrescrever=True, ator_id=None)
            e = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f.id, data=date(2026, 6, 1)).one()
        assert e.tipo == "NORMAL"       # substituído pelo padrão (dia útil)
        assert res["atualizados"] >= 1

    def test_sem_jornada_entra_em_sem_padrao(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Sem Jornada", cpf="52998224725", ativo=True, tipo_contrato="CLT")
            _db.session.add(f)
            _db.session.flush()
            res = EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            ids_sem = [x.id for x in res["sem_padrao"]]
            assert f.id in ids_sem
            assert res["criados"] == 0

    def test_diarista_nao_e_escalado(self, app, db):
        with app.app_context():
            f = Funcionario(nome="Diarista", cpf="52998224725", ativo=True, tipo_contrato="DIARISTA")
            _db.session.add(f)
            _db.session.flush()
            res = EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            n = _db.session.query(EscalaEntrada).filter_by(funcionario_id=f.id).count()
        assert n == 0
        assert res["total_funcionarios"] == 0


# ---------------------------------------------------------------------------
# presenca_dia
# ---------------------------------------------------------------------------

# Data no passado para evitar lógica de "hoje" nos testes sem mock de datetime
_DIA_PASSADO = date(2026, 1, 15)

_UTC_TZ = ZoneInfo("UTC")
_PATCH_TZ = patch(
    "app.services.timezone_service.ConfiguracaoService.get",
    return_value="UTC",
)


def _func_para_presenca(nome: str = "Func", cpf: str = "52998224725",
                        horario: time | None = time(8, 0)) -> Funcionario:
    f = Funcionario(
        nome=nome, cpf=cpf, ativo=True,
        minutos_esperados_dia=480,
        horario_entrada_padrao=horario,
    )
    _db.session.add(f)
    _db.session.flush()
    return f


def _escala_normal(funcionario_id: int, data: date) -> EscalaEntrada:
    e = EscalaEntrada(funcionario_id=funcionario_id, data=data, tipo="NORMAL", labels=[])
    _db.session.add(e)
    _db.session.flush()
    return e


def _time_day(funcionario_id: int, data: date, *,
              minutos_trabalhados: int = 0,
              needs_review: bool = False,
              auto_debit_aplicado: bool = False,
              saldo_final_minutos: int = 0) -> TimeDay:
    td = TimeDay(
        funcionario_id=funcionario_id,
        shift_date=data,
        expected_minutes_snapshot=480,
        tolerance_snapshot=10,
        minutos_trabalhados=minutos_trabalhados,
        saldo_calculado_minutos=saldo_final_minutos,
        saldo_final_minutos=saldo_final_minutos,
        needs_review=needs_review,
        auto_debit_aplicado=auto_debit_aplicado,
    )
    _db.session.add(td)
    _db.session.flush()
    return td


def _time_entry(funcionario_id: int, data: date, hora: int, minuto: int = 0) -> TimeEntry:
    punch = datetime(data.year, data.month, data.day, hora, minuto, tzinfo=timezone.utc)
    te = TimeEntry(
        funcionario_id=funcionario_id,
        punch_time=punch,
        shift_date=data,
        punch_type="clock_in",
        source="employee",
    )
    _db.session.add(te)
    _db.session.flush()
    return te


class TestPresencaDia:

    def test_sem_escalados_retorna_lista_vazia(self, app, db):
        with app.app_context():
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado == []

    def test_folga_nao_aparece_na_presenca(self, app, db):
        """Funcionário com FOLGA não deve entrar na lista de presença."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca()
            e = EscalaEntrada(funcionario_id=f.id, data=_DIA_PASSADO, tipo="FOLGA", labels=[])
            _db.session.add(e)
            _db.session.flush()
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado == []

    def test_falta_justificada_nao_aparece_na_presenca(self, app, db):
        """Funcionário com FALTA_JUSTIFICADA não deve entrar na lista de presença
        (só entradas NORMAL são "escaladas" para o painel)."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca()
            e = EscalaEntrada(
                funcionario_id=f.id, data=_DIA_PASSADO, tipo="FALTA_JUSTIFICADA",
                labels=[], justificativa="Atestado médico",
            )
            _db.session.add(e)
            _db.session.flush()
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado == []

    def test_presente_sem_horario_padrao(self, app, db):
        """Tem batida, sem horario_entrada_padrao → status 'presente'."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca(horario=None)
            _escala_normal(f.id, _DIA_PASSADO)
            _time_entry(f.id, _DIA_PASSADO, 8, 0)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert len(resultado) == 1
        assert resultado[0]["status"] == "presente"
        assert resultado[0]["funcionario"].id == f.id

    def test_presente_dentro_da_tolerancia(self, app, db):
        """1ª batida às 8:05 com entrada padrão 8:00 → dentro de 10 min → 'presente'."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca(horario=time(8, 0))
            _escala_normal(f.id, _DIA_PASSADO)
            _time_entry(f.id, _DIA_PASSADO, 8, 5)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado[0]["status"] == "presente"

    def test_atrasado_alem_da_tolerancia(self, app, db):
        """1ª batida às 8:25 com entrada padrão 8:00 → 25 min > tolerância → 'atrasado'."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca(horario=time(8, 0))
            _escala_normal(f.id, _DIA_PASSADO)
            _time_entry(f.id, _DIA_PASSADO, 8, 25)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado[0]["status"] == "atrasado"

    def test_ausente_por_auto_debit(self, app, db):
        """auto_debit_aplicado=True sem batidas → 'ausente'."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca()
            _escala_normal(f.id, _DIA_PASSADO)
            _time_day(f.id, _DIA_PASSADO, auto_debit_aplicado=True)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado[0]["status"] == "ausente"

    def test_revisar_quando_needs_review(self, app, db):
        """TimeDay.needs_review=True → 'revisar' (tem precedência sobre outros estados)."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca()
            _escala_normal(f.id, _DIA_PASSADO)
            _time_entry(f.id, _DIA_PASSADO, 8, 0)
            _time_day(f.id, _DIA_PASSADO, needs_review=True, minutos_trabalhados=0)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado[0]["status"] == "revisar"

    def test_ausente_data_passada_sem_batidas(self, app, db):
        """Data passada, sem batidas e sem auto_debit → 'ausente' (data < hoje)."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca()
            _escala_normal(f.id, _DIA_PASSADO)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado[0]["status"] == "ausente"

    def test_primeira_batida_exposta(self, app, db):
        """Verifica que primeira_batida é preenchida corretamente."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca(horario=None)
            _escala_normal(f.id, _DIA_PASSADO)
            _time_entry(f.id, _DIA_PASSADO, 9, 30)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        item = resultado[0]
        assert item["primeira_batida"] is not None
        assert item["primeira_batida"].hour == 9
        assert item["primeira_batida"].minute == 30

    def test_saldo_exposto_quando_time_day_existe(self, app, db):
        """saldo deve refletir saldo_final_minutos do TimeDay."""
        with app.app_context(), _PATCH_TZ:
            f = _func_para_presenca(horario=None)
            _escala_normal(f.id, _DIA_PASSADO)
            _time_entry(f.id, _DIA_PASSADO, 8, 0)
            _time_day(f.id, _DIA_PASSADO, minutos_trabalhados=540, saldo_final_minutos=60)
            resultado = EscalaService.presenca_dia(_DIA_PASSADO)
        assert resultado[0]["saldo"] == 60

    def test_filtro_area_id_exclui_outra_area(self, app, db):
        """area_id filtra: funcionários de outras áreas não aparecem."""
        from app.core.models import Area
        with app.app_context(), _PATCH_TZ:
            a1 = Area(name="Area 1", slug="area-1", is_active=True)
            a2 = Area(name="Area 2", slug="area-2", is_active=True)
            _db.session.add_all([a1, a2])
            _db.session.flush()

            f1 = Funcionario(nome="F1", cpf="52998224725", ativo=True, area_id=a1.id)
            f2 = Funcionario(nome="F2", cpf="34578814722", ativo=True, area_id=a2.id)
            _db.session.add_all([f1, f2])
            _db.session.flush()

            _escala_normal(f1.id, _DIA_PASSADO)
            _escala_normal(f2.id, _DIA_PASSADO)

            resultado = EscalaService.presenca_dia(_DIA_PASSADO, area_id=a1.id)
        assert len(resultado) == 1
        assert resultado[0]["funcionario"].id == f1.id


# ---------------------------------------------------------------------------
# Notificação via Aviso (publicar → notificar)
# ---------------------------------------------------------------------------

def _avisos_de(funcionario_id):
    from app.models.avisos import Aviso
    return _db.session.query(Aviso).filter_by(destinatario_id=funcionario_id).all()


class TestNotificacaoEscala:

    def test_registrar_novo_dia_notifica(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            avisos = _avisos_de(f.id)
            assert len(avisos) == 1
            assert "15/06/2026" in avisos[0].mensagem
            assert "Manhã" in avisos[0].mensagem

    def test_atualizar_com_mudanca_notifica(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            EscalaService.registrar(f.id, _DATA, "FOLGA", ator_id=None)
            avisos = _avisos_de(f.id)
            assert len(avisos) == 2
            assert "Folga" in avisos[-1].mensagem

    def test_atualizar_sem_mudanca_nao_notifica(self, app, db):
        """Re-salvar o mesmo estado não gera aviso duplicado."""
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            EscalaService.registrar(f.id, _DATA, "NORMAL", ["MANHA"], ator_id=None)
            assert len(_avisos_de(f.id)) == 1

    def test_registrar_notificar_false_silencioso(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", notificar=False, ator_id=None)
            assert _avisos_de(f.id) == []

    def test_remover_notifica(self, app, db):
        with app.app_context():
            f = _func()
            EscalaService.registrar(f.id, _DATA, "NORMAL", notificar=False, ator_id=None)
            EscalaService.remover(f.id, _DATA, ator_id=None)
            avisos = _avisos_de(f.id)
            assert len(avisos) == 1
            assert "removida" in avisos[0].mensagem

    def test_lote_gera_um_resumo_por_funcionario(self, app, db):
        with app.app_context():
            f = _func()
            datas = [date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)]
            EscalaService.registrar_lote([f.id], datas, "FOLGA", ator_id=None)
            avisos = _avisos_de(f.id)
            assert len(avisos) == 1
            assert "15/06" in avisos[0].mensagem
            assert "17/06" in avisos[0].mensagem

    def test_gerar_do_padrao_um_resumo_por_funcionario(self, app, db):
        with app.app_context():
            f = _func_com_jornada()
            EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            avisos = _avisos_de(f.id)
            assert len(avisos) == 1
            assert "06/2026" in avisos[0].mensagem
            assert "dia(s) de trabalho" in avisos[0].mensagem

    def test_gerar_sem_escritas_nao_notifica(self, app, db):
        """Segunda geração sem sobrescrever não escreve nada → sem novo aviso."""
        with app.app_context():
            f = _func_com_jornada()
            EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            EscalaService.gerar_a_partir_do_padrao(2026, 6, ator_id=None)
            assert len(_avisos_de(f.id)) == 1

    def test_destinatario_inativo_nao_quebra(self, app, db):
        """Falha na notificação nunca impede o registro da escala."""
        with app.app_context():
            f = _func()
            f.ativo = False
            _db.session.flush()
            # registrar exige funcionário existente, mas o aviso falharia
            # (destinatário inativo) — deve ser engolido silenciosamente.
            entrada = EscalaService.registrar(f.id, _DATA, "NORMAL", ator_id=None)
            assert entrada.id is not None
            assert _avisos_de(f.id) == []
