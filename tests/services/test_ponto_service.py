# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários e de integração para PontoService.

Separados em duas classes:
    TestApplyTolerance       — função pura (sem DB)
    TestCalculateDailyBalance — função pura (sem DB)
    TestRegistrarBatida      — mutação com DB (usa fixture app/db do conftest)
    TestProcessarDia         — mutação com DB
    TestEditarBatidaAdmin    — mutação com DB (anteriormente stub)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.models import AuditAction, AuditLog
from app.models.funcionario import Funcionario
from app.models.ponto import ProcessingStatus, TimeDay, TimeEntry
from app.services.ponto_service import PontoService, _apply_tolerance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _punch(hour: int, minute: int = 0) -> dict:
    """Constrói um dict de batida para o dia fixo 2026-01-15."""
    return {"time": datetime(2026, 1, 15, hour, minute, tzinfo=timezone.utc)}


def _criar_funcionario(db) -> Funcionario:
    """Cria e persiste um funcionário de teste."""
    f = Funcionario(
        nome="Colaborador Teste",
        cpf="52998224725",
        minutos_esperados_dia=480,
        minutos_almoco=60,
        ativo=True,
    )
    db.session.add(f)
    db.session.commit()
    return f


# ---------------------------------------------------------------------------
# _apply_tolerance (puro — sem DB)
# ---------------------------------------------------------------------------

class TestApplyTolerance:
    def test_hora_extra_inalterada(self):
        assert _apply_tolerance(30, 10) == 30

    def test_zero_inalterado(self):
        assert _apply_tolerance(0, 10) == 0

    def test_atraso_pequeno_perdoado(self):
        assert _apply_tolerance(-8, 10) == 0

    def test_limite_da_tolerancia_perdoado(self):
        assert _apply_tolerance(-10, 10) == 0

    def test_atraso_grande_apenas_excesso_penalizado(self):
        assert _apply_tolerance(-25, 10) == -15

    def test_um_acima_da_tolerancia(self):
        assert _apply_tolerance(-11, 10) == -1


# ---------------------------------------------------------------------------
# calculate_daily_balance (puro — sem DB)
# ---------------------------------------------------------------------------

class TestCalculateDailyBalance:

    def test_saldo_par_normal_zero(self):
        """4 batidas CLT (8-12, 13-17): pares somam 8h=480 min, expected=480 → saldo 0.

        O almoço NÃO é subtraído separadamente — o intervalo 12h-13h nunca
        é somado, pois fica entre os pares. Sem desconto duplo.
        """
        punches = [_punch(8, 0), _punch(12, 0), _punch(13, 0), _punch(17, 0)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=480)
        # Par(0,1)=240 min + Par(2,3)=240 min = 480 min trabalhados
        assert result["minutos_trabalhados"] == 480
        assert result["saldo_calculado_minutos"] == 0
        assert result["needs_review"] is False

    def test_saldo_impar_congela(self):
        """Rule #5: 3 batidas → needs_review=True, saldo congelado."""
        punches = [_punch(8), _punch(12), _punch(13)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=480)
        assert result["needs_review"] is True
        assert result["saldo_calculado_minutos"] == 0
        assert result["minutos_trabalhados"] == 0

    def test_saldo_um_punch_congela(self):
        """1 batida também é ímpar → needs_review=True."""
        result = PontoService.calculate_daily_balance([_punch(8)], expected_minutes=480)
        assert result["needs_review"] is True

    def test_tolerancia_pequeno_atraso(self):
        """8 min de atraso com 2 batidas → dentro da tolerância → saldo 0."""
        punches = [_punch(8, 8), _punch(17, 0)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=540)
        assert result["saldo_calculado_minutos"] == 0
        assert result["needs_review"] is False

    def test_tolerancia_grande_atraso(self):
        """25 min de atraso → apenas -15 penalizado."""
        punches = [_punch(8, 25), _punch(17, 0)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=540)
        assert result["saldo_calculado_minutos"] == -15

    def test_hora_extra_nao_afetada(self):
        """Horas extras não sofrem tolerância."""
        punches = [_punch(8, 0), _punch(18, 0)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=540)
        assert result["saldo_calculado_minutos"] == 60

    def test_zero_punches_returns_review(self):
        """0 é par — sem batidas o saldo fica negativo."""
        result = PontoService.calculate_daily_balance([], expected_minutes=480)
        assert result["needs_review"] is False
        assert result["minutos_trabalhados"] == 0
        assert result["saldo_calculado_minutos"] == _apply_tolerance(-480, 10)

    def test_caso_real_quatro_batidas_11h25_a_14h02(self):
        """Reproduz o bug reportado: 11:25, 11:34, 12:56, 14:02 → 75 min (não 15).

        Bug anterior: o código subtraía 60 min fixos após somar os pares,
        resultando em 75 - 60 = 15 min. O correto é 75 min, pois o almoço
        (11:34 → 12:56 = 82 min) já fica excluído pela estrutura dos pares.
        """
        punches = [
            {"time": datetime(2026, 2, 25, 11, 25, tzinfo=timezone.utc)},
            {"time": datetime(2026, 2, 25, 11, 34, tzinfo=timezone.utc)},
            {"time": datetime(2026, 2, 25, 12, 56, tzinfo=timezone.utc)},
            {"time": datetime(2026, 2, 25, 14,  2, tzinfo=timezone.utc)},
        ]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=480)
        # Par(0,1): 11:25→11:34 =  9 min
        # Par(2,3): 12:56→14:02 = 66 min  → total = 75 min
        assert result["minutos_trabalhados"] == 75
        assert result["needs_review"] is False

    def test_quatro_batidas_nao_subtrai_almoco_adicional(self):
        """Com 4 batidas, minutos_trabalhados é apenas a soma dos pares (almoço já excluído)."""
        punches = [_punch(8), _punch(12), _punch(13), _punch(17)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=480)
        # Par(0,1): 08:00→12:00 = 240 min
        # Par(2,3): 13:00→17:00 = 240 min  → total = 480 min (sem subtração extra)
        assert result["minutos_trabalhados"] == 480

    def test_duas_batidas_trabalho_continuo(self):
        """Com 2 batidas (sem almoço registrado), soma o intervalo inteiro."""
        punches = [_punch(8), _punch(17)]
        result = PontoService.calculate_daily_balance(punches, expected_minutes=480)
        assert result["minutos_trabalhados"] == 540


# ---------------------------------------------------------------------------
# registrar_batida (com DB)
# ---------------------------------------------------------------------------

class TestRegistrarBatida:

    def test_registra_batida_e_cria_time_entry(self, app, db):
        """Happy path: registrar_batida cria uma TimeEntry no banco."""
        with app.app_context():
            f = _criar_funcionario(db)
            entry = PontoService.registrar_batida(
                funcionario_id=f.id,
                punch_time=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
                source="employee",
                ator_id=f.id,
            )
            assert entry.id is not None
            assert entry.funcionario_id == f.id
            assert entry.processing_status == ProcessingStatus.PROCESSADO

    def test_registrar_batida_gera_audit(self, app, db):
        """Cada batida deve gerar exatamente um AuditLog com action=create."""
        with app.app_context():
            f = _criar_funcionario(db)
            entry = PontoService.registrar_batida(
                funcionario_id=f.id,
                punch_time=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
                ator_id=f.id,
            )
            count = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.create, entity_id=entry.id, module="ponto")
                .count()
            )
            assert count == 1

    def test_registrar_batida_cria_time_day(self, app, db):
        """Após registrar batida, um TimeDay deve ser criado ou atualizado."""
        with app.app_context():
            f = _criar_funcionario(db)
            punch_time = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
            PontoService.registrar_batida(
                funcionario_id=f.id,
                punch_time=punch_time,
                ator_id=f.id,
            )
            day = (
                db.session.query(TimeDay)
                .filter_by(funcionario_id=f.id, shift_date=punch_time.date())
                .first()
            )
            assert day is not None
            assert day.needs_review is True  # 1 batida é ímpar

    def test_duplicata_levanta_value_error(self, app, db):
        """Batida duplicada em menos de 5 segundos deve levantar ValueError."""
        with app.app_context():
            f = _criar_funcionario(db)
            t = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
            PontoService.registrar_batida(funcionario_id=f.id, punch_time=t, ator_id=f.id)
            t2 = t + timedelta(seconds=2)
            with pytest.raises(ValueError, match="duplicada"):
                PontoService.registrar_batida(funcionario_id=f.id, punch_time=t2, ator_id=f.id)

    def test_funcionario_inexistente_levanta_value_error(self, app, db):
        """Funcionário não encontrado deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                PontoService.registrar_batida(funcionario_id=9999, ator_id=1)


# ---------------------------------------------------------------------------
# reprocess_interval (com DB)
# ---------------------------------------------------------------------------

class TestReprocessarPonto:

    def test_reprocess_success(self, app, db):
        """Reprocessar atualiza os saldos mantendo os snapshots antigos."""
        with app.app_context():
            f = _criar_funcionario(db)
            data_str = datetime(2026, 1, 15, tzinfo=timezone.utc)
            
            # 2 batidas
            PontoService.registrar_batida(f.id, data_str.replace(hour=8), ator_id=f.id)
            PontoService.registrar_batida(f.id, data_str.replace(hour=17), ator_id=f.id)
            
            # Adulterando o snapshot pra estragar o saldo simulando bug velho
            day = db.session.query(TimeDay).filter_by(funcionario_id=f.id).first()
            day.saldo_calculado_minutos = 15 # Valor arbitrário e errado
            db.session.commit()
            
            res = PontoService.reprocess_interval(
                funcionario_id=f.id,
                start_date=data_str.date(),
                end_date=data_str.date(),
                ator_id=f.id,
                override_snapshots=False
            )
            
            # Verificando se arrumou para +60 (ja que a jornada snapshot tava certa)
            db.session.refresh(day)
            assert res["dias_processados"] == 1
            assert day.saldo_calculado_minutos == 60
            
    def test_reprocess_com_override(self, app, db):
        """Reprocessar ignorando snapshot antigo, adotando jornada JSON atual."""
        with app.app_context():
            f = _criar_funcionario(db)
            data_str = datetime(2026, 1, 15, tzinfo=timezone.utc)
            
            # 2 batidas (9 hrs trabalhadas - 8h as 17h = 540 min)
            PontoService.registrar_batida(f.id, data_str.replace(hour=8), ator_id=f.id)
            PontoService.registrar_batida(f.id, data_str.replace(hour=17), ator_id=f.id)
            
            # Mudando a carga horária atual do funcionário na Quinta-feira do evento para 6h (360m)
            f.minutos_esperados_dia = 480
            f.jornada_semanal = {
                "quinta": {"ativo": True, "minutos": 360, "almoco": 60}
            }
            db.session.commit()
            
            # Override!
            PontoService.reprocess_interval(
                funcionario_id=f.id,
                start_date=data_str.date(),
                end_date=data_str.date(),
                ator_id=f.id,
                override_snapshots=True,
                tolerance=15
            )
            
            # Antes o saldo era de +60 (480 min esperado). Agora a pessoa deve 360 (jornada semanal de quinta lida corretamente) e trabalhou 540.
            # Saldo esperado: 540 - 360 = +180 min
            day = db.session.query(TimeDay).filter_by(funcionario_id=f.id).first()
            
            assert day.expected_minutes_snapshot == 360
            assert day.tolerance_snapshot == 15
            assert day.saldo_calculado_minutos == 180

    def test_reprocess_gera_log_massivo(self, app, db):
        """A operação de reprocessamento deve gerar 1 único AuditLog com os deltas."""
        with app.app_context():
            f = _criar_funcionario(db)
            start = datetime(2026, 1, 10, tzinfo=timezone.utc).date()
            end = datetime(2026, 1, 15, tzinfo=timezone.utc).date()
            
            # Adiciona batidas de ponto no dia 12 para que haja de fato reprocessamento e log:
            dt12 = datetime(2026, 1, 12, tzinfo=timezone.utc)
            PontoService.registrar_batida(f.id, dt12.replace(hour=8), ator_id=f.id)
            PontoService.registrar_batida(f.id, dt12.replace(hour=17), ator_id=f.id)
            
            
            PontoService.reprocess_interval(f.id, start, end, ator_id=f.id)
            db.session.commit()
            
            logs = db.session.query(AuditLog).filter_by(action=AuditAction.update, module="ponto", entity_id=f.id).all()
            # Procurar o log massivo pela key 'action' que injetamos manualmente no payload
            mass_logs = [l for l in logs if l.previous_state and l.previous_state.get("action") == "mass_reprocess_before"]
            assert len(mass_logs) == 1

# ---------------------------------------------------------------------------
# processar_dia (com DB)
# ---------------------------------------------------------------------------

class TestProcessarDia:

    def test_par_de_batidas_gera_saldo_correto(self, app, db):
        """2 batidas (08:00–17:00) → 540 min, esperado 480 → saldo +60."""
        with app.app_context():
            f = _criar_funcionario(db)
            shift_date = datetime(2026, 1, 15, tzinfo=timezone.utc).date()
            for h in [8, 17]:
                PontoService.registrar_batida(
                    funcionario_id=f.id,
                    punch_time=datetime(2026, 1, 15, h, 0, tzinfo=timezone.utc),
                    ator_id=f.id,
                )
            day = (
                db.session.query(TimeDay)
                .filter_by(funcionario_id=f.id, shift_date=shift_date)
                .first()
            )
            assert day is not None
            assert day.needs_review is False
            assert day.saldo_calculado_minutos == 60   # +1h extra

    def test_batida_impar_seta_needs_review(self, app, db):
        """1 batida (ímpar) → needs_review=True."""
        with app.app_context():
            f = _criar_funcionario(db)
            PontoService.registrar_batida(
                funcionario_id=f.id,
                punch_time=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
                ator_id=f.id,
            )
            day = db.session.query(TimeDay).filter_by(funcionario_id=f.id).first()
            assert day.needs_review is True


# ---------------------------------------------------------------------------
# editar_batida_admin (com DB)
# ---------------------------------------------------------------------------

class TestEditarBatidaAdmin:

    def test_motivo_vazio_levanta_value_error(self):
        """motivo vazio deve levantar ValueError antes de acessar o banco."""
        with pytest.raises(ValueError, match="motivo"):
            PontoService.editar_batida_admin(
                time_entry_id=1,
                novo_horario=datetime(2026, 1, 15, 9, 0),
                motivo="",
                ator_id=1,
            )

    def test_editar_batida_gera_audit(self, app, db):
        """Editar uma batida deve gerar exatamente um AuditLog com action=update."""
        with app.app_context():
            f = _criar_funcionario(db)
            entry = PontoService.registrar_batida(
                funcionario_id=f.id,
                punch_time=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
                ator_id=f.id,
            )
            novo_horario = datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc)
            PontoService.editar_batida_admin(
                time_entry_id=entry.id,
                novo_horario=novo_horario,
                motivo="Correção de horário de entrada",
                ator_id=f.id,
            )
            count = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=entry.id, module="ponto")
                .count()
            )
            assert count == 1

    def test_editar_batida_atualiza_punch_time(self, app, db):
        """O punch_time da batida deve ser atualizado para o novo horário."""
        with app.app_context():
            f = _criar_funcionario(db)
            entry = PontoService.registrar_batida(
                funcionario_id=f.id,
                punch_time=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
                ator_id=f.id,
            )
            novo_horario = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
            PontoService.editar_batida_admin(
                time_entry_id=entry.id,
                novo_horario=novo_horario,
                motivo="Teste de edição",
                ator_id=f.id,
            )
            db.session.expire(entry)
            updated = db.session.get(TimeEntry, entry.id)
            # SQLite retorna datetimes sem tzinfo; comparamos apenas o valor ingênuo
            assert updated.punch_time.replace(tzinfo=None) == novo_horario.replace(tzinfo=None)
            assert updated.admin_change_reason == "Teste de edição"

    def test_batida_inexistente_levanta_value_error(self, app, db):
        """Batida não encontrada deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                PontoService.editar_batida_admin(
                    time_entry_id=9999,
                    novo_horario=datetime(2026, 1, 15, 9, 0),
                    motivo="Motivo qualquer",
                    ator_id=1,
                )

# ---------------------------------------------------------------------------
# recalcular_banco_global e registrar_abatimento_horas (com DB)
# ---------------------------------------------------------------------------

class TestRecalcularBancoGlobal:
    
    def test_recalcular_banco_soma_correta(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            data_str = datetime(2026, 1, 15, tzinfo=timezone.utc)
            
            # 2 batidas no dia 15 (+60min saldo)
            PontoService.registrar_batida(f.id, data_str.replace(hour=8), ator_id=f.id)
            PontoService.registrar_batida(f.id, data_str.replace(hour=17), ator_id=f.id)
            
            # 2 batidas no dia 16 (+60min saldo)
            data_16 = data_str + timedelta(days=1)
            PontoService.registrar_batida(f.id, data_16.replace(hour=8), ator_id=f.id)
            PontoService.registrar_batida(f.id, data_16.replace(hour=17), ator_id=f.id)
            
            # Adiciona abatimento de 30min. Pagamento, não folga: folga abate a
            # jornada inteira do dia e neutraliza o dia, o que mudaria a soma.
            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=30,
                data_registro=data_str.date(),
                observacao="Desconto teste",
                ator_id=f.id,
                pagamento_valor="10.00",
            )
            
            # Total esperado: 60 + 60 - 30 = 90
            saldo = PontoService.recalcular_banco_global(f.id, ator_id=f.id)
            
            assert saldo == 90
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 90


class TestRegistrarAbatimentoHoras:

    def test_registrar_abatimento_reduz_cache(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 100
            db.session.commit()
            
            deduction = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=40,
                data_registro=datetime(2026, 1, 15).date(),
                observacao="Pagamento em dinheiro",
                ator_id=f.id,
                pagamento_valor=150.00
            )
            
            assert deduction.id is not None
            assert deduction.deduction_type.value == "pagamento"
            assert deduction.minutos_abatidos == 40
            
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 60

    def test_minutos_negativos_levanta_value_error(self, app, db):
        """Em pagamento os minutos são informados — e precisam ser positivos.

        (Em folga eles são ignorados: abate-se a jornada prevista do dia.)
        """
        with app.app_context():
            f = _criar_funcionario(db)
            with pytest.raises(ValueError, match="maiores que zero"):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="pagamento",
                    pagamento_valor="10.00",
                    minutos=-10,
                    data_registro=datetime.now().date(),
                    observacao="Invalido",
                    ator_id=f.id
                )

    def test_tipo_invalido_levanta_value_error(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            with pytest.raises(ValueError):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="tipo_inexistente",
                    minutos=30,
                    data_registro=datetime.now().date(),
                    observacao="Teste",
                    ator_id=f.id,
                )

    def test_pagamento_grava_valor_observacao_e_autor(self, app, db):
        """Pagamento em dinheiro registra valor, texto ao colaborador e quem lançou."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()

            ded = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=1200,
                data_registro=datetime(2026, 9, 30).date(),
                observacao="Pagamento das horas extras de setembro/2026",
                ator_id=f.id,
                pagamento_valor="500,00".replace(",", "."),
            )

            assert ded.deduction_type.value == "pagamento"
            assert float(ded.pagamento_valor) == 500.00
            assert ded.observacao == "Pagamento das horas extras de setembro/2026"
            assert ded.criado_por_id == f.id
            assert ded.eh_estorno is False

            db.session.refresh(f)
            assert f.banco_horas_acumulado == 0

    def test_pagamento_sem_valor_recusa(self, app, db):
        """Abater minutos sem dizer quanto foi pago torna o lançamento inconferível."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 600
            db.session.commit()
            with pytest.raises(ValueError, match="valor pago"):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="pagamento",
                    minutos=60,
                    data_registro=datetime(2026, 9, 30).date(),
                    observacao="Pagamento de setembro",
                    ator_id=f.id,
                )

    def test_pagamento_sem_observacao_recusa(self, app, db):
        """A observação é a única explicação que o colaborador recebe pela queda no saldo."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 600
            db.session.commit()
            with pytest.raises(ValueError, match="observação é obrigatória"):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="pagamento",
                    minutos=60,
                    data_registro=datetime(2026, 9, 30).date(),
                    observacao="   ",
                    ator_id=f.id,
                    pagamento_valor="100.00",
                )

    def test_saldo_insuficiente_recusa(self, app, db):
        """Não se paga 20h a quem tem 2h — sendo dinheiro, o erro sai antes de alguém ver."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 120
            db.session.commit()
            with pytest.raises(ValueError, match="Saldo insuficiente"):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="pagamento",
                    minutos=1200,
                    data_registro=datetime(2026, 9, 30).date(),
                    observacao="Pagamento indevido",
                    ator_id=f.id,
                    pagamento_valor="500.00",
                )
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 120

    def test_abater_saldo_exato_e_permitido(self, app, db):
        """O limite é o saldo, inclusive — abater tudo zera e não levanta erro."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 480
            db.session.commit()
            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=480,
                data_registro=datetime(2026, 9, 30).date(),
                observacao="Pagamento integral",
                ator_id=f.id,
                pagamento_valor="200.00",
            )
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 0

    def test_tipos_antigos_da_ui_dao_erro_legivel(self, app, db):
        """Os tipos que o formulário antigo enviava devem falhar com mensagem útil.

        Regressão do defeito que manteve a função inoperante: o menu oferecia
        'ferias', 'atestado' e afins, e o erro que chegava ao operador era a
        repr crua do Python.
        """
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 600
            db.session.commit()
            for tipo in ("ferias", "atestado", "licenca", "falta_justificada"):
                with pytest.raises(ValueError, match="Tipos aceitos"):
                    PontoService.registrar_abatimento_horas(
                        funcionario_id=f.id,
                        deduction_type=tipo,
                        minutos=60,
                        data_registro=datetime(2026, 9, 30).date(),
                        observacao="x",
                        ator_id=f.id,
                    )

    def test_auditoria_registra_valor_observacao_e_autor(self, app, db):
        """A trilha de um evento financeiro precisa carregar valor e justificativa."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 900
            db.session.commit()

            ded = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=900,
                data_registro=datetime(2026, 9, 30).date(),
                observacao="Pagamento set/2026",
                ator_id=f.id,
                pagamento_valor="375.50",
            )

            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.create, entity_id=ded.id, module="ponto_abatimento")
                .one()
            )
            assert log.new_state["pagamento_valor"] == "375.50"
            assert log.new_state["observacao"] == "Pagamento set/2026"
            assert log.new_state["criado_por_id"] == f.id
            assert log.new_state["data_registro"] == "2026-09-30"
            assert log.new_state["saldo_anterior"] == 900
            assert log.new_state["saldo_novo"] == 0

    def test_audit_log_criado(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 120
            db.session.commit()
            deduction = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=60,
                data_registro=datetime(2026, 3, 10).date(),
                observacao="Pagamento parcial",
                ator_id=f.id,
                pagamento_valor="25.00",
            )
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.create, entity_id=deduction.id, module="ponto_abatimento")
                .one()
            )
            assert log.new_state["minutos"] == 60
            assert log.new_state["saldo_anterior"] == 120
            assert log.new_state["saldo_novo"] == 60


class TestFolgaCompensatoria:
    """Folga compensatória troca um dia de jornada por saldo — e só uma vez.

    Regressão do caso relatado em staging: a folga de 16/09 foi lançada, mas o
    dia sem batida continuou contando como falta (−5h50). O saldo pagava a
    mesma folga duas vezes: −350 pelo dia e −350 pelo lançamento.

    Cenário: jornada padrão de 480 min (fallback do cadastro). Dois dias de
    crédito de +300 cada (13h trabalhadas) e um terceiro dia sem batida, que
    sozinho vale −470 (−480 + 10 de tolerância).
    """

    _CREDITO_1 = datetime(2026, 1, 12, tzinfo=timezone.utc)
    _CREDITO_2 = datetime(2026, 1, 13, tzinfo=timezone.utc)
    _DIA_FOLGA = datetime(2026, 1, 14).date()

    def _cenario(self, db):
        f = _criar_funcionario(db)  # minutos_esperados_dia=480
        for d in (self._CREDITO_1, self._CREDITO_2):
            PontoService.registrar_batida(f.id, d.replace(hour=9), ator_id=f.id)
            PontoService.registrar_batida(f.id, d.replace(hour=22), ator_id=f.id)
        PontoService.processar_dia(f.id, self._DIA_FOLGA)
        db.session.refresh(f)
        return f

    @staticmethod
    def _dia(db, f, data):
        from app.models.ponto import TimeDay
        return db.session.query(TimeDay).filter_by(funcionario_id=f.id, shift_date=data).one()

    def test_cenario_de_partida(self, app, db):
        """Confere a premissa: dia sem batida debita, saldo já carrega o débito."""
        with app.app_context():
            f = self._cenario(db)
            assert self._dia(db, f, self._DIA_FOLGA).saldo_final_minutos == -470
            assert f.banco_horas_acumulado == 300 + 300 - 470

    def test_folga_neutraliza_o_dia_e_cobra_uma_vez(self, app, db):
        with app.app_context():
            f = self._cenario(db)

            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="folga",
                minutos=0,
                data_registro=self._DIA_FOLGA,
                observacao="Folga compensatória",
                ator_id=f.id,
            )

            dia = self._dia(db, f, self._DIA_FOLGA)
            assert dia.expected_minutes_snapshot == 0
            assert dia.saldo_final_minutos == 0

            db.session.refresh(f)
            # 600 de crédito − 480 da jornada trocada; o −470 do dia sumiu
            assert f.banco_horas_acumulado == 120
            # E a verdade recalculada do zero concorda com o cache
            assert PontoService.recalcular_banco_global(f.id, ator_id=f.id) == 120

    def test_abate_jornada_prevista_e_ignora_minutos_digitados(self, app, db):
        """Digitar 470 (o desvio com tolerância) não pode virar o valor da folga."""
        with app.app_context():
            f = self._cenario(db)
            ded = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="folga",
                minutos=470,
                data_registro=self._DIA_FOLGA,
                observacao="",
                ator_id=f.id,
            )
            assert ded.minutos_abatidos == 480

    def test_trava_de_saldo_considera_o_dia_neutralizado(self, app, db):
        """Saldo cru 130 < 480, mas o −470 do dia volta: 600 disponíveis → aceita.

        Comparar com o saldo cru recusaria exatamente a folga que o
        colaborador tem crédito para tirar.
        """
        with app.app_context():
            f = self._cenario(db)
            assert f.banco_horas_acumulado == 130
            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="folga",
                minutos=0,
                data_registro=self._DIA_FOLGA,
                observacao="",
                ator_id=f.id,
            )

    def test_saldo_realmente_insuficiente_recusa_sem_mexer_no_dia(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            PontoService.processar_dia(f.id, self._DIA_FOLGA)  # só o débito, sem crédito
            with pytest.raises(ValueError, match="Saldo insuficiente"):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="folga",
                    minutos=0,
                    data_registro=self._DIA_FOLGA,
                    observacao="",
                    ator_id=f.id,
                )
            assert self._dia(db, f, self._DIA_FOLGA).saldo_final_minutos == -470

    def test_dia_sem_jornada_prevista_recusa(self, app, db):
        """Folga de escala já zera o dia — não há jornada para trocar por saldo."""
        from app.models.escala import EscalaEntrada

        with app.app_context():
            f = self._cenario(db)
            db.session.add(EscalaEntrada(funcionario_id=f.id, data=self._DIA_FOLGA, tipo="FOLGA"))
            db.session.commit()
            with pytest.raises(ValueError, match="não tem jornada prevista"):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="folga",
                    minutos=0,
                    data_registro=self._DIA_FOLGA,
                    observacao="",
                    ator_id=f.id,
                )

    def test_folga_duplicada_recusa(self, app, db):
        with app.app_context():
            f = self._cenario(db)
            kwargs = dict(
                funcionario_id=f.id, deduction_type="folga", minutos=0,
                data_registro=self._DIA_FOLGA, observacao="", ator_id=f.id,
            )
            PontoService.registrar_abatimento_horas(**kwargs)
            with pytest.raises(ValueError, match="Já existe folga"):
                PontoService.registrar_abatimento_horas(**kwargs)

    def test_estorno_devolve_a_jornada_ao_dia(self, app, db):
        """Desfeita a folga, o dia volta a ser falta e o saldo volta ao ponto de partida."""
        with app.app_context():
            f = self._cenario(db)
            saldo_inicial = f.banco_horas_acumulado

            ded = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id, deduction_type="folga", minutos=0,
                data_registro=self._DIA_FOLGA, observacao="", ator_id=f.id,
            )
            PontoService.estornar_abatimento(ded.id, motivo="Lançada por engano", ator_id=f.id)

            dia = self._dia(db, f, self._DIA_FOLGA)
            assert dia.expected_minutes_snapshot == 480
            assert dia.saldo_final_minutos == -470

            db.session.refresh(f)
            assert f.banco_horas_acumulado == saldo_inicial
            assert PontoService.recalcular_banco_global(f.id, ator_id=f.id) == saldo_inicial

    def test_folga_estornada_pode_ser_lancada_de_novo(self, app, db):
        with app.app_context():
            f = self._cenario(db)
            kwargs = dict(
                funcionario_id=f.id, deduction_type="folga", minutos=0,
                data_registro=self._DIA_FOLGA, observacao="", ator_id=f.id,
            )
            ded = PontoService.registrar_abatimento_horas(**kwargs)
            PontoService.estornar_abatimento(ded.id, motivo="Data errada", ator_id=f.id)
            PontoService.registrar_abatimento_horas(**kwargs)
            assert self._dia(db, f, self._DIA_FOLGA).saldo_final_minutos == 0

    def test_folga_em_dia_futuro_sem_registro_de_ponto(self, app, db):
        """Folga agendada com antecedência: o dia nasce zerado."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 600
            db.session.commit()
            futuro = datetime(2026, 12, 21).date()

            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id, deduction_type="folga", minutos=0,
                data_registro=futuro, observacao="", ator_id=f.id,
            )
            assert self._dia(db, f, futuro).saldo_final_minutos == 0
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 120

    def test_batida_posterior_no_dia_de_folga_vira_credito(self, app, db):
        """Veio trabalhar mesmo com folga: as horas contam inteiras, sem falta."""
        with app.app_context():
            f = self._cenario(db)
            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id, deduction_type="folga", minutos=0,
                data_registro=self._DIA_FOLGA, observacao="", ator_id=f.id,
            )
            dia_utc = datetime(2026, 1, 14, tzinfo=timezone.utc)
            PontoService.registrar_batida(f.id, dia_utc.replace(hour=12), ator_id=f.id)
            PontoService.registrar_batida(f.id, dia_utc.replace(hour=14), ator_id=f.id)
            assert self._dia(db, f, self._DIA_FOLGA).saldo_final_minutos == 120


class TestEstornarAbatimento:
    """Estorno por contra-lançamento — nada é apagado, o saldo volta."""

    @staticmethod
    def _pagamento(db, f, minutos=1200, valor="500.00"):
        return PontoService.registrar_abatimento_horas(
            funcionario_id=f.id,
            deduction_type="pagamento",
            minutos=minutos,
            data_registro=datetime(2026, 9, 30).date(),
            observacao="Pagamento das horas extras de setembro/2026",
            ator_id=f.id,
            pagamento_valor=valor,
        )

    def test_estorno_devolve_saldo_e_cria_linha_inversa(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()

            original = self._pagamento(db, f)
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 0

            estorno = PontoService.estornar_abatimento(
                deduction_id=original.id,
                motivo="Lançado no colaborador errado",
                ator_id=f.id,
            )

            assert estorno.minutos_abatidos == -1200
            assert float(estorno.pagamento_valor) == -500.00
            assert estorno.estorno_de_id == original.id
            assert estorno.eh_estorno is True
            assert estorno.observacao == "Lançado no colaborador errado"

            db.session.refresh(f)
            assert f.banco_horas_acumulado == 1200

    def test_original_permanece_visivel_e_marcado(self, app, db):
        """O lançamento errado não some — o extrato mostra o par erro + correção."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()

            original = self._pagamento(db, f)
            PontoService.estornar_abatimento(
                deduction_id=original.id, motivo="Valor errado", ator_id=f.id,
            )

            linhas = PontoService.listar_abatimentos(f.id)
            assert len(linhas) == 2
            db.session.refresh(original)
            assert original.estornado is True

    def test_invariante_do_saldo_sobrevive_ao_recalculo(self, app, db):
        """`recalcular_banco_global` não precisa filtrar nada: a soma se anula sozinha.

        É a razão de o estorno ser contra-lançamento e não um status — nenhuma
        agregação precisou ganhar cláusula nova, então não há o risco de uma
        delas ser lembrada e a outra esquecida.
        """
        with app.app_context():
            f = _criar_funcionario(db)
            data = datetime(2026, 1, 15, tzinfo=timezone.utc)
            PontoService.registrar_batida(f.id, data.replace(hour=8), ator_id=f.id)
            PontoService.registrar_batida(f.id, data.replace(hour=17), ator_id=f.id)

            saldo_antes = PontoService.recalcular_banco_global(f.id, ator_id=f.id)

            original = self._pagamento(db, f, minutos=saldo_antes, valor="100.00")
            assert PontoService.recalcular_banco_global(f.id, ator_id=f.id) == 0

            PontoService.estornar_abatimento(
                deduction_id=original.id, motivo="Erro de digitação", ator_id=f.id,
            )
            assert PontoService.recalcular_banco_global(f.id, ator_id=f.id) == saldo_antes

    def test_estorno_duplo_recusado(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()
            original = self._pagamento(db, f)
            PontoService.estornar_abatimento(
                deduction_id=original.id, motivo="Primeiro estorno", ator_id=f.id,
            )
            with pytest.raises(ValueError, match="já foi estornado"):
                PontoService.estornar_abatimento(
                    deduction_id=original.id, motivo="Segundo estorno", ator_id=f.id,
                )

    def test_nao_estorna_um_estorno(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()
            original = self._pagamento(db, f)
            estorno = PontoService.estornar_abatimento(
                deduction_id=original.id, motivo="Erro", ator_id=f.id,
            )
            with pytest.raises(ValueError, match="já é um estorno"):
                PontoService.estornar_abatimento(
                    deduction_id=estorno.id, motivo="Desfazer o desfazer", ator_id=f.id,
                )

    def test_motivo_obrigatorio(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()
            original = self._pagamento(db, f)
            with pytest.raises(ValueError, match="motivo do estorno"):
                PontoService.estornar_abatimento(
                    deduction_id=original.id, motivo="  ", ator_id=f.id,
                )
            db.session.refresh(f)
            assert f.banco_horas_acumulado == 0

    def test_lancamento_inexistente(self, app, db):
        with app.app_context():
            _criar_funcionario(db)
            with pytest.raises(ValueError, match="não encontrado"):
                PontoService.estornar_abatimento(
                    deduction_id=99999, motivo="Qualquer", ator_id=1,
                )

    def test_auditoria_do_estorno(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()
            original = self._pagamento(db, f)
            estorno = PontoService.estornar_abatimento(
                deduction_id=original.id, motivo="Colaborador errado", ator_id=f.id,
            )
            log = (
                db.session.query(AuditLog)
                .filter_by(
                    action=AuditAction.create,
                    entity_id=estorno.id,
                    module="ponto_abatimento_estorno",
                )
                .one()
            )
            assert log.new_state["estorno_de_id"] == original.id
            assert log.new_state["minutos_devolvidos"] == 1200
            assert log.new_state["valor_estornado"] == "500.00"
            assert log.new_state["motivo"] == "Colaborador errado"
            assert log.new_state["saldo_anterior"] == 0
            assert log.new_state["saldo_novo"] == 1200


class TestListarAbatimentos:
    """Leitura que alimenta o extrato do colaborador e o painel do RH."""

    def test_filtra_por_periodo_e_ordena(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 3000
            db.session.commit()

            for dia in (5, 20):
                PontoService.registrar_abatimento_horas(
                    funcionario_id=f.id,
                    deduction_type="pagamento",
                    minutos=60,
                    data_registro=datetime(2026, 9, dia).date(),
                    observacao=f"Pagamento dia {dia}",
                    ator_id=f.id,
                    pagamento_valor="50.00",
                )
            PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="folga",
                minutos=60,
                data_registro=datetime(2026, 10, 3).date(),
                observacao="Folga de outubro",
                ator_id=f.id,
            )

            setembro = PontoService.listar_abatimentos(
                f.id, inicio=datetime(2026, 9, 1).date(), fim=datetime(2026, 10, 1).date(),
            )
            assert len(setembro) == 2
            # Mais recente primeiro
            assert setembro[0].data_registro.day == 20
            assert setembro[1].data_registro.day == 5

            assert len(PontoService.listar_abatimentos(f.id)) == 3

    def test_estorno_aparece_logo_abaixo_do_original(self, app, db):
        """Mesma data → ordem por id, para o par erro+correção ficar junto."""
        with app.app_context():
            f = _criar_funcionario(db)
            f.banco_horas_acumulado = 1200
            db.session.commit()
            original = PontoService.registrar_abatimento_horas(
                funcionario_id=f.id,
                deduction_type="pagamento",
                minutos=1200,
                data_registro=datetime(2026, 9, 30).date(),
                observacao="Pagamento set/2026",
                ator_id=f.id,
                pagamento_valor="500.00",
            )
            estorno = PontoService.estornar_abatimento(
                deduction_id=original.id, motivo="Erro", ator_id=f.id,
            )
            linhas = PontoService.listar_abatimentos(f.id)
            assert [l.id for l in linhas] == [original.id, estorno.id]


# ---------------------------------------------------------------------------
# deletar_batida_admin (com DB)
# ---------------------------------------------------------------------------

class TestDeletarBatidaAdmin:

    def test_deleta_batida_e_recalcula_dia(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            t1 = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 3, 10, 17, 0, tzinfo=timezone.utc)
            e1 = PontoService.registrar_batida(f.id, t1, ator_id=f.id)
            PontoService.registrar_batida(f.id, t2, ator_id=f.id)

            day_antes = (
                db.session.query(TimeDay)
                .filter_by(funcionario_id=f.id, shift_date=t1.date())
                .one()
            )
            assert day_antes.needs_review is False  # 2 batidas = par

            resultado = PontoService.deletar_batida_admin(
                time_entry_id=e1.id,
                motivo="Batida errada",
                ator_id=f.id,
            )

            assert resultado["id"] == e1.id
            # Após deleção, 1 batida restante → needs_review
            day_depois = (
                db.session.query(TimeDay)
                .filter_by(funcionario_id=f.id, shift_date=t1.date())
                .one()
            )
            assert day_depois.needs_review is True

    def test_audit_delete_criado_antes_da_exclusao(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            entry = PontoService.registrar_batida(
                f.id,
                datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc),
                ator_id=f.id,
            )
            entry_id = entry.id
            PontoService.deletar_batida_admin(
                time_entry_id=entry_id,
                motivo="Motivo válido",
                ator_id=f.id,
            )
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.delete, entity_id=entry_id, module="ponto")
                .one()
            )
            assert log.previous_state["admin_change_reason"] == "Motivo válido"
            assert db.session.get(TimeEntry, entry_id) is None

    def test_motivo_vazio_levanta_value_error(self, app, db):
        with app.app_context():
            f = _criar_funcionario(db)
            entry = PontoService.registrar_batida(
                f.id,
                datetime(2026, 3, 12, 8, 0, tzinfo=timezone.utc),
                ator_id=f.id,
            )
            with pytest.raises(ValueError):
                PontoService.deletar_batida_admin(
                    time_entry_id=entry.id,
                    motivo="",
                    ator_id=f.id,
                )

    def test_batida_inexistente_levanta_value_error(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                PontoService.deletar_batida_admin(
                    time_entry_id=99999,
                    motivo="Motivo válido",
                    ator_id=1,
                )


# ---------------------------------------------------------------------------
# TimezoneService — guard de regressão do bug 2.3b
# ---------------------------------------------------------------------------

class TestTimezoneGuard:
    """
    Garante que parse_datetime_local() interpreta corretamente o horário no
    fuso do sistema (America/Sao_Paulo, UTC-3) e o converte para UTC.

    Bug 2.3b: antes do fix, o código carimbava tzinfo=utc diretamente sobre
    o valor local, resultando em deslocamento de 3 horas na persistência.
    """

    def test_horario_local_convertido_para_utc(self, app, db):
        from app.services.timezone_service import TimezoneService
        from zoneinfo import ZoneInfo

        with app.app_context():
            # Sao Paulo é UTC-3 (horário padrão)
            resultado = TimezoneService.parse_datetime_local("2026-03-10T14:00")
            # 14:00 BRT → 17:00 UTC
            assert resultado.tzinfo is not None
            assert resultado.hour == 17
            assert resultado.minute == 0

    def test_ida_e_volta_preserva_horario(self, app, db):
        """datetime_local_input(parse_datetime_local(valor)) == valor original."""
        from app.services.timezone_service import TimezoneService

        with app.app_context():
            original = "2026-03-10T09:30"
            utc = TimezoneService.parse_datetime_local(original)
            exibido = TimezoneService.datetime_local_input(utc)
            assert exibido == original

    def test_valor_vazio_levanta_value_error(self, app, db):
        from app.services.timezone_service import TimezoneService

        with app.app_context():
            with pytest.raises(ValueError, match="obrigatório"):
                TimezoneService.parse_datetime_local("")

    def test_formato_invalido_levanta_value_error(self, app, db):
        from app.services.timezone_service import TimezoneService

        with app.app_context():
            with pytest.raises(ValueError, match="inválido"):
                TimezoneService.parse_datetime_local("not-a-date")


# ---------------------------------------------------------------------------
# calculate_daily_balance com fator_multiplicador (D18 — feriado)
# ---------------------------------------------------------------------------

class TestCalculateDailyBalanceFatorFeriado:
    """Testes puros (sem DB) para o parâmetro fator_multiplicador (D18)."""

    def test_fator_padrao_comportamento_inalterado(self):
        """fator_multiplicador=1.0 (padrão) não altera o cálculo existente."""
        punches = [_punch(8), _punch(17)]  # 540 min trabalhados
        result = PontoService.calculate_daily_balance(
            punches, expected_minutes=480, fator_multiplicador=1.0
        )
        assert result["minutos_trabalhados"] == 540
        assert result["saldo_calculado_minutos"] == 60  # 540 - 480

    def test_feriado_dobra_saldo(self):
        """fator=2.0 + expected=0 (feriado): saldo = trabalhados × 2."""
        punches = [_punch(8), _punch(17)]  # 540 min reais
        result = PontoService.calculate_daily_balance(
            punches, expected_minutes=0, fator_multiplicador=2.0
        )
        # minutos_trabalhados = real (sem fator)
        assert result["minutos_trabalhados"] == 540
        # saldo = 540 × 2 − 0 = 1080
        assert result["saldo_calculado_minutos"] == 1080
        assert result["needs_review"] is False

    def test_feriado_sem_batidas_saldo_zero(self):
        """Feriado sem batidas: expected=0 e trabalhado=0 → saldo=0."""
        result = PontoService.calculate_daily_balance(
            [], expected_minutes=0, fator_multiplicador=2.0
        )
        assert result["minutos_trabalhados"] == 0
        assert result["saldo_calculado_minutos"] == 0

    def test_feriado_impar_batidas_needs_review(self):
        """Regra #5 tem precedência sobre fator_multiplicador: ímpar → needs_review=True."""
        result = PontoService.calculate_daily_balance(
            [_punch(8)], expected_minutes=0, fator_multiplicador=2.0
        )
        assert result["needs_review"] is True


# ---------------------------------------------------------------------------
# _get_expected_minutes — D7 (escala override) e D18 (feriado → 0)
# ---------------------------------------------------------------------------

_DATA_QUINTA = __import__("datetime").date(2026, 1, 15)  # quinta-feira (weekday=3)


class TestGetExpectedMinutesD7:
    """Testa a função _get_expected_minutes com escala e feriado (D7 + D18)."""

    def test_sem_escala_sem_jornada_retorna_minutos_padrao(self, app, db):
        """Sem escala e sem jornada_semanal → retorna minutos_esperados_dia."""
        from unittest.mock import patch
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)  # minutos_esperados_dia=480
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 480

    def test_jornada_semanal_ativa_usa_minutos_do_dia(self, app, db):
        """jornada_semanal com quinta ativa → retorna minutos configurados."""
        from unittest.mock import patch
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": True, "minutos": 360}}
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 360

    def test_jornada_semanal_dia_inativo_retorna_zero(self, app, db):
        """jornada_semanal com o dia "ativo": false → 0, não o padrão global.

        Regressão: o dia inativo caía no fallback `minutos_esperados_dia`
        (480), então quem não trabalha no dia e ainda assim batia ponto
        ficava com débito no banco de horas em vez de hora extra.
        """
        from unittest.mock import patch
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)  # minutos_esperados_dia=480
            f.jornada_semanal = {"quinta": {"ativo": False, "minutos": 0}}
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 0

    def test_jornada_semanal_sem_o_dia_cai_no_padrao(self, app, db):
        """Dia ausente do dicionário → nada declarado, mantém o padrão global.

        Distinto de "ativo": false: aqui a jornada simplesmente não diz nada
        sobre a quinta-feira, então o fallback do cadastro continua valendo.
        """
        from unittest.mock import patch
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"segunda": {"ativo": True, "minutos": 360}}
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 480

    def test_batida_em_dia_inativo_vira_hora_extra(self, app, db):
        """Fim a fim: 2h num dia não trabalhado → +2h de crédito, não débito.

        Caso real (folha de 12/09/2026): 12:00–14:00 num sábado inativo
        aparecia como −5h50, porque o esperado vinha 480 em vez de 0.
        """
        from unittest.mock import patch
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": False, "minutos": 0}}
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                esperado = _get_expected_minutes(f, _DATA_QUINTA)

            calc = PontoService.calculate_daily_balance(
                [_punch(12), _punch(14)],
                expected_minutes=esperado,
                tolerance=10,
            )
            assert calc["minutos_trabalhados"] == 120
            assert calc["saldo_calculado_minutos"] == 120

    def test_folga_compensatoria_retorna_zero(self, app, db):
        """Folga compensatória vigente zera a expectativa; estornada, volta."""
        from unittest.mock import patch
        from app.models.ponto import DeductionType, TimeBankDeduction
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            original = TimeBankDeduction(
                funcionario_id=f.id, deduction_type=DeductionType.folga,
                minutos_abatidos=480, data_registro=_DATA_QUINTA,
            )
            db.session.add(original)
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                assert _get_expected_minutes(f, _DATA_QUINTA) == 0

                db.session.add(TimeBankDeduction(
                    funcionario_id=f.id, deduction_type=DeductionType.folga,
                    minutos_abatidos=-480, data_registro=_DATA_QUINTA,
                    estorno_de_id=original.id,
                ))
                db.session.commit()
                assert _get_expected_minutes(f, _DATA_QUINTA) == 480

    def test_pagamento_nao_zera_o_dia(self, app, db):
        """Só folga troca o dia por saldo; pagamento em dinheiro não mexe na jornada."""
        from unittest.mock import patch
        from app.models.ponto import DeductionType, TimeBankDeduction
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            db.session.add(TimeBankDeduction(
                funcionario_id=f.id, deduction_type=DeductionType.pagamento,
                minutos_abatidos=480, data_registro=_DATA_QUINTA,
            ))
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                assert _get_expected_minutes(f, _DATA_QUINTA) == 480

    def test_escala_folga_retorna_zero(self, app, db):
        """EscalaEntrada FOLGA para a data → expected=0, independente da jornada."""
        from unittest.mock import patch
        from app.models.escala import EscalaEntrada
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": True, "minutos": 480}}
            db.session.flush()
            entrada = EscalaEntrada(
                funcionario_id=f.id,
                data=_DATA_QUINTA,
                tipo="FOLGA",
            )
            db.session.add(entrada)
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 0

    def test_escala_falta_justificada_retorna_zero(self, app, db):
        """EscalaEntrada FALTA_JUSTIFICADA → expected=0, mesmo efeito de FOLGA."""
        from unittest.mock import patch
        from app.models.escala import EscalaEntrada
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": True, "minutos": 480}}
            db.session.flush()
            entrada = EscalaEntrada(
                funcionario_id=f.id,
                data=_DATA_QUINTA,
                tipo="FALTA_JUSTIFICADA",
                justificativa="Atestado médico anexado: https://exemplo.com/atestado.pdf",
            )
            db.session.add(entrada)
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 0

    def test_escala_normal_usa_jornada(self, app, db):
        """EscalaEntrada NORMAL → expected = minutos da jornada (NORMAL confirma trabalho)."""
        from unittest.mock import patch
        from app.models.escala import EscalaEntrada
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": True, "minutos": 360}}
            db.session.flush()
            entrada = EscalaEntrada(
                funcionario_id=f.id,
                data=_DATA_QUINTA,
                tipo="NORMAL",
            )
            db.session.add(entrada)
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 360

    def test_feriado_retorna_zero_mesmo_com_escala_normal(self, app, db):
        """Feriado tem prioridade máxima: expected=0 mesmo com escala NORMAL."""
        from unittest.mock import patch
        from app.models.escala import EscalaEntrada
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": True, "minutos": 480}}
            db.session.flush()
            entrada = EscalaEntrada(
                funcionario_id=f.id,
                data=_DATA_QUINTA,
                tipo="NORMAL",
            )
            db.session.add(entrada)
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=True):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 0

    def test_feriado_retorna_zero_mesmo_com_jornada(self, app, db):
        """Feriado → 0 independente de jornada_semanal configurada."""
        from unittest.mock import patch
        from app.services.ponto_service import _get_expected_minutes

        with app.app_context():
            f = _criar_funcionario(db)
            f.jornada_semanal = {"quinta": {"ativo": True, "minutos": 480}}
            db.session.commit()
            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=True):
                resultado = _get_expected_minutes(f, _DATA_QUINTA)
            assert resultado == 0


# ---------------------------------------------------------------------------
# processar_dia com D7 (escala override) e D18 (feriado)
# ---------------------------------------------------------------------------

class TestProcessarDiaD7:
    """Integração completa: processar_dia considera escala e feriado."""

    def test_folga_na_escala_snapshot_zero(self, app, db):
        """FOLGA na escala → expected_minutes_snapshot=0 → sem débito de ausência."""
        from unittest.mock import patch
        from datetime import date
        from app.models.escala import EscalaEntrada

        with app.app_context():
            f = _criar_funcionario(db)
            data = date(2026, 1, 15)
            entrada = EscalaEntrada(funcionario_id=f.id, data=data, tipo="FOLGA")
            db.session.add(entrada)
            db.session.commit()

            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                time_day = PontoService.processar_dia(f.id, data)

            assert time_day.expected_minutes_snapshot == 0
            # Sem batidas e expected=0 → saldo=0, sem débito
            assert time_day.saldo_final_minutos == 0
            assert time_day.needs_review is False

    def test_feriado_com_batidas_saldo_dobrado(self, app, db):
        """Feriado com batidas → minutos_trabalhados real + saldo = trabalhados × 2."""
        from unittest.mock import patch
        from datetime import date

        with app.app_context():
            f = _criar_funcionario(db)
            data = date(2026, 1, 15)

            t_base = datetime(2026, 1, 15, tzinfo=timezone.utc)
            # Par de batidas: 8h–17h = 540 min
            for hora in (8, 17):
                db.session.add(TimeEntry(
                    funcionario_id=f.id,
                    shift_date=data,
                    punch_time=t_base.replace(hour=hora),
                    source="employee",
                ))
            db.session.commit()

            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=True):
                time_day = PontoService.processar_dia(f.id, data)

            assert time_day.expected_minutes_snapshot == 0
            assert time_day.minutos_trabalhados == 540          # real, sem fator
            assert time_day.saldo_calculado_minutos == 1080     # 540 × 2

    def test_dia_normal_sem_mudancas(self, app, db):
        """Dia comum sem escala e sem feriado: comportamento original preservado."""
        from unittest.mock import patch
        from datetime import date

        with app.app_context():
            f = _criar_funcionario(db)  # minutos_esperados_dia=480
            data = date(2026, 1, 15)

            t_base = datetime(2026, 1, 15, tzinfo=timezone.utc)
            for hora in (8, 17):
                db.session.add(TimeEntry(
                    funcionario_id=f.id,
                    shift_date=data,
                    punch_time=t_base.replace(hour=hora),
                    source="employee",
                ))
            db.session.commit()

            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                time_day = PontoService.processar_dia(f.id, data)

            assert time_day.expected_minutes_snapshot == 480
            assert time_day.minutos_trabalhados == 540
            assert time_day.saldo_calculado_minutos == 60   # 540 - 480

    def test_escala_atualiza_snapshot_em_time_day_existente(self, app, db):
        """Quando TimeDay já existe, processar_dia atualiza o expected_minutes_snapshot (D7)."""
        from unittest.mock import patch
        from datetime import date
        from app.models.escala import EscalaEntrada

        with app.app_context():
            f = _criar_funcionario(db)
            data = date(2026, 1, 15)

            # TimeDay criado antes da escala existir (snapshot com jornada padrão)
            old_day = TimeDay(
                funcionario_id=f.id,
                shift_date=data,
                expected_minutes_snapshot=480,
                tolerance_snapshot=10,
                saldo_final_minutos=0,
                minutos_trabalhados=0,
                saldo_calculado_minutos=0,
            )
            db.session.add(old_day)
            db.session.commit()

            # Operador marca FOLGA após o TimeDay ser criado
            entrada = EscalaEntrada(funcionario_id=f.id, data=data, tipo="FOLGA")
            db.session.add(entrada)
            db.session.commit()

            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                PontoService.processar_dia(f.id, data)

            db.session.refresh(old_day)
            # Snapshot atualizado para 0 (FOLGA)
            assert old_day.expected_minutes_snapshot == 0

    def test_auto_debit_aplicado_limpa_quando_esperado_zera(self, app, db):
        """Débito provisório (auto_debit_aplicado) some quando o esperado do
        dia zera — não só quando chega batida real. Sem esta regra, uma
        falta justificada lançada depois que o débito já foi aplicado
        deixaria o TimeDay com auto_debit_aplicado=True para sempre, mesmo
        com saldo correto (0)."""
        from unittest.mock import patch
        from datetime import date
        from app.models.escala import EscalaEntrada

        with app.app_context():
            f = _criar_funcionario(db)
            data = date(2026, 1, 15)
            f.banco_horas_acumulado = -480
            db.session.flush()

            # Estado deixado por AvisoService.verificar_atrasos(): débito
            # provisório aplicado, sem nenhuma batida.
            time_day = TimeDay(
                funcionario_id=f.id,
                shift_date=data,
                expected_minutes_snapshot=480,
                tolerance_snapshot=10,
                minutos_trabalhados=0,
                saldo_calculado_minutos=-480,
                saldo_final_minutos=-480,
                auto_debit_aplicado=True,
                needs_review=False,
            )
            db.session.add(time_day)
            db.session.commit()

            # Falta justificada lançada depois — zera o esperado do dia.
            db.session.add(EscalaEntrada(
                funcionario_id=f.id, data=data, tipo="FALTA_JUSTIFICADA",
                justificativa="Atestado médico",
            ))
            db.session.commit()

            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                PontoService.processar_dia(f.id, data)

            db.session.refresh(time_day)
            db.session.refresh(f)
            assert time_day.auto_debit_aplicado is False
            assert time_day.saldo_final_minutos == 0
            assert f.banco_horas_acumulado == 0

    def test_auto_debit_aplicado_continua_sem_batida_e_esperado_positivo(self, app, db):
        """Sem escala/feriado que zere o esperado, o débito provisório
        continua em vigor — comportamento original preservado."""
        from unittest.mock import patch
        from datetime import date

        with app.app_context():
            f = _criar_funcionario(db)
            data = date(2026, 1, 15)

            time_day = TimeDay(
                funcionario_id=f.id,
                shift_date=data,
                expected_minutes_snapshot=480,
                tolerance_snapshot=10,
                minutos_trabalhados=0,
                saldo_calculado_minutos=-480,
                saldo_final_minutos=-480,
                auto_debit_aplicado=True,
                needs_review=False,
            )
            db.session.add(time_day)
            db.session.commit()

            with patch("app.services.calendar_service.CalendarService.eh_feriado", return_value=False):
                PontoService.processar_dia(f.id, data)

            db.session.refresh(time_day)
            assert time_day.auto_debit_aplicado is True
