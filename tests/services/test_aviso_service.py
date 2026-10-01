# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para AvisoService e AusenciaService.

Cenários cobertos:
    1. criar() gera exatamente 1 AuditLog.
    2. contar_nao_lidos() retorna 0 após marcar_todos_lidos().
    3. verificar_atrasos() cria Aviso(ATRASO) para funcionário sem batida.
    4. verificar_atrasos() ignora funcionário com AusenciaJustificada aprovada.
    5. verificar_atrasos() ignora funcionário que já registrou ponto.
    6. processar_dia() reverte auto_debit_aplicado quando batida real chega.
    7. AusenciaService.aprovar() reverte débito provisório no banco de horas.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.models import AuditAction, AuditLog
from app.models.avisos import Aviso, AusenciaJustificada, TipoAviso, TipoAusencia
from app.models.funcionario import Funcionario
from app.models.ponto import TimeDay, TimeEntry
from app.services.aviso_service import AvisoService
from app.services.ausencia_service import AusenciaService


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_CPF = "52998224725"
_DATA_TESTE = date(2026, 3, 6)   # quinta-feira


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _criar_funcionario(db, *, cpf: str = _CPF, ativo: bool = True) -> Funcionario:
    """Cria e persiste um Funcionario mínimo de teste."""
    f = Funcionario(
        nome="Teste Colaborador",
        cpf=cpf,
        ativo=ativo,
        minutos_esperados_dia=480,
        minutos_almoco=60,
    )
    db.session.add(f)
    db.session.commit()
    return f


def _criar_funcionario_com_horario(db, hora_entrada: time = time(8, 0)) -> Funcionario:
    """Cria Funcionario com horario_entrada_padrao configurado."""
    f = Funcionario(
        nome="Colaborador Pontual",
        cpf="11144477735",
        ativo=True,
        minutos_esperados_dia=480,
        minutos_almoco=60,
        horario_entrada_padrao=hora_entrada,
    )
    db.session.add(f)
    db.session.commit()
    return f


def _count_audit(db, action: AuditAction, module: str | None = None) -> int:
    """Conta linhas de AuditLog filtrando por ação e módulo opcional."""
    q = db.session.query(AuditLog).filter_by(action=action)
    if module:
        q = q.filter_by(module=module)
    return q.count()


# ---------------------------------------------------------------------------
# Teste 1 — criar() dispara exatamente 1 AuditLog
# ---------------------------------------------------------------------------


class TestCriarAviso:

    def test_criar_aviso_dispara_audit(self, app, db):
        """criar() deve persistir o aviso e registrar 1 AuditLog(create, 'avisos')."""
        with app.app_context():
            func = _criar_funcionario(db)
            aviso = AvisoService.criar(
                destinatario_id=func.id,
                titulo="Aviso de teste",
                mensagem="Mensagem de teste",
                tipo="SISTEMA",
                ator_id=None,
            )

            assert aviso.id is not None
            assert aviso.titulo == "Aviso de teste"
            assert aviso.tipo == TipoAviso.SISTEMA
            assert aviso.is_lido is False

            count = _count_audit(db, AuditAction.create, module="avisos")
            assert count == 1

    def test_criar_aviso_destinatario_inativo_levanta_erro(self, app, db):
        """criar() deve lançar ValueError se o destinatário estiver inativo."""
        with app.app_context():
            func = _criar_funcionario(db, ativo=False)
            with pytest.raises(ValueError, match="inativo"):
                AvisoService.criar(
                    destinatario_id=func.id,
                    titulo="Ignorado",
                    mensagem="",
                    tipo="SISTEMA",
                )


# ---------------------------------------------------------------------------
# Teste 2 — contar_nao_lidos() retorna 0 após marcar_todos_lidos()
# ---------------------------------------------------------------------------


class TestMarcarTodosLidos:

    def test_marcar_todos_lidos_zera_contador(self, app, db):
        """Após marcar_todos_lidos(), contar_nao_lidos() deve retornar 0."""
        with app.app_context():
            func = _criar_funcionario(db)

            # Criar 3 avisos não lidos diretamente no banco (sem passar por criar())
            for i in range(3):
                a = Aviso(
                    destinatario_id=func.id,
                    titulo=f"Aviso {i}",
                    tipo=TipoAviso.SISTEMA,
                    is_lido=False,
                )
                db.session.add(a)
            db.session.commit()

            assert AvisoService.contar_nao_lidos(func.id) == 3

            marcados = AvisoService.marcar_todos_lidos(func.id, ator_id=func.id)
            assert marcados == 3
            assert AvisoService.contar_nao_lidos(func.id) == 0


# ---------------------------------------------------------------------------
# Teste 3 — verificar_atrasos() cria Aviso(ATRASO) para funcionário sem batida
# ---------------------------------------------------------------------------


class TestVerificarAtrasos:

    def test_cria_aviso_atraso_sem_batida(self, app, db):
        """verificar_atrasos() deve criar Aviso(ATRASO) e TimeDay com auto_debit_aplicado=True
        para funcionário com horario_entrada_padrao e sem batida no dia."""
        with app.app_context():
            func = _criar_funcionario_com_horario(db, hora_entrada=time(8, 0))

            # Simular horário local às 08:30 (20 min de tolerância já passou)
            hora_simulada = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                     8, 30, tzinfo=timezone.utc)

            with patch("app.services.aviso_service.datetime") as mock_dt:
                mock_dt.now.return_value = hora_simulada
                mock_dt.combine = datetime.combine
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                # ConfiguracaoService é importado localmente — patchar no módulo de origem
                with patch(
                    "app.services.configuracao_service.ConfiguracaoService.get",
                    return_value="UTC",
                ):
                    resultado = AvisoService.verificar_atrasos()

            assert resultado["processados"] == 1
            assert resultado["erros"] == []

            avisos = db.session.query(Aviso).filter_by(
                destinatario_id=func.id, tipo=TipoAviso.ATRASO
            ).all()
            assert len(avisos) >= 1

            time_day = db.session.query(TimeDay).filter_by(
                funcionario_id=func.id, shift_date=_DATA_TESTE
            ).first()
            assert time_day is not None
            assert time_day.auto_debit_aplicado is True
            assert time_day.saldo_final_minutos < 0

    def test_ignora_diarista(self, app, db):
        """Diarista nunca gera Aviso/débito de atraso automático, mesmo com
        horário de entrada configurado — não tem jornada fixa nem banco de horas (2.2)."""
        with app.app_context():
            f = Funcionario(
                nome="Diarista Pontual",
                cpf="11144477735",
                ativo=True,
                minutos_esperados_dia=480,
                minutos_almoco=60,
                horario_entrada_padrao=time(8, 0),
                tipo_contrato="DIARISTA",
            )
            db.session.add(f)
            db.session.commit()

            hora_simulada = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                     8, 30, tzinfo=timezone.utc)
            with patch("app.services.aviso_service.datetime") as mock_dt:
                mock_dt.now.return_value = hora_simulada
                mock_dt.combine = datetime.combine
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                with patch(
                    "app.services.configuracao_service.ConfiguracaoService.get",
                    return_value="UTC",
                ):
                    resultado = AvisoService.verificar_atrasos()

            assert resultado["processados"] == 0
            avisos = db.session.query(Aviso).filter_by(
                destinatario_id=f.id, tipo=TipoAviso.ATRASO
            ).all()
            assert avisos == []
            time_day = db.session.query(TimeDay).filter_by(
                funcionario_id=f.id, shift_date=_DATA_TESTE
            ).first()
            assert time_day is None

    # -----------------------------------------------------------------------
    # Teste 4 — ignora AusenciaJustificada aprovada
    # -----------------------------------------------------------------------

    def test_ignora_ausencia_justificada_aprovada(self, app, db):
        """verificar_atrasos() não deve criar Aviso se o funcionário tem ausência aprovada."""
        with app.app_context():
            func = _criar_funcionario_com_horario(db, hora_entrada=time(8, 0))

            # Criar ausência aprovada para hoje
            ausencia = AusenciaJustificada(
                funcionario_id=func.id,
                data=_DATA_TESTE,
                tipo=TipoAusencia.FOLGA,
                aprovado=True,
            )
            db.session.add(ausencia)
            db.session.commit()

            hora_simulada = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                     8, 30, tzinfo=timezone.utc)

            with patch("app.services.aviso_service.datetime") as mock_dt:
                mock_dt.now.return_value = hora_simulada
                mock_dt.combine = datetime.combine
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                with patch(
                    "app.services.configuracao_service.ConfiguracaoService.get",
                    return_value="UTC",
                ):
                    resultado = AvisoService.verificar_atrasos()

            assert resultado["processados"] == 0

            avisos = db.session.query(Aviso).filter_by(
                destinatario_id=func.id, tipo=TipoAviso.ATRASO
            ).count()
            assert avisos == 0

    def test_ignora_dia_com_folga_compensatoria(self, app, db):
        """Dia trocado por saldo não gera 'Atraso detectado' nem débito provisório."""
        from app.models.ponto import DeductionType, TimeBankDeduction

        with app.app_context():
            func = _criar_funcionario_com_horario(db, hora_entrada=time(8, 0))
            db.session.add(TimeBankDeduction(
                funcionario_id=func.id, deduction_type=DeductionType.folga,
                minutos_abatidos=480, data_registro=_DATA_TESTE,
            ))
            db.session.commit()

            hora_simulada = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                     8, 30, tzinfo=timezone.utc)
            with patch("app.services.aviso_service.datetime") as mock_dt:
                mock_dt.now.return_value = hora_simulada
                mock_dt.combine = datetime.combine
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                with patch(
                    "app.services.configuracao_service.ConfiguracaoService.get",
                    return_value="UTC",
                ):
                    resultado = AvisoService.verificar_atrasos()

            assert resultado["processados"] == 0
            assert db.session.query(Aviso).filter_by(
                destinatario_id=func.id, tipo=TipoAviso.ATRASO
            ).count() == 0
            assert db.session.query(TimeDay).filter_by(
                funcionario_id=func.id, shift_date=_DATA_TESTE
            ).first() is None

    def test_ignora_dia_com_folga_na_escala(self, app, db):
        """Mesma garantia para a FOLGA da escala, que também zera a expectativa."""
        from app.models.escala import EscalaEntrada

        with app.app_context():
            func = _criar_funcionario_com_horario(db, hora_entrada=time(8, 0))
            db.session.add(EscalaEntrada(funcionario_id=func.id, data=_DATA_TESTE, tipo="FOLGA"))
            db.session.commit()

            hora_simulada = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                     8, 30, tzinfo=timezone.utc)
            with patch("app.services.aviso_service.datetime") as mock_dt:
                mock_dt.now.return_value = hora_simulada
                mock_dt.combine = datetime.combine
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                with patch(
                    "app.services.configuracao_service.ConfiguracaoService.get",
                    return_value="UTC",
                ):
                    resultado = AvisoService.verificar_atrasos()

            assert resultado["processados"] == 0
            assert db.session.query(Aviso).filter_by(
                destinatario_id=func.id, tipo=TipoAviso.ATRASO
            ).count() == 0

    # -----------------------------------------------------------------------
    # Teste 5 — ignora funcionário que já bateu ponto
    # -----------------------------------------------------------------------

    def test_ignora_quem_ja_bateu_ponto(self, app, db):
        """verificar_atrasos() não deve criar Aviso se o funcionário já registrou batida."""
        with app.app_context():
            func = _criar_funcionario_com_horario(db, hora_entrada=time(8, 0))

            # Inserir batida real para hoje
            entry = TimeEntry(
                funcionario_id=func.id,
                shift_date=_DATA_TESTE,
                punch_time=datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                    8, 5, tzinfo=timezone.utc),
                source="employee",
            )
            db.session.add(entry)
            db.session.commit()

            hora_simulada = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                                     8, 30, tzinfo=timezone.utc)

            with patch("app.services.aviso_service.datetime") as mock_dt:
                mock_dt.now.return_value = hora_simulada
                mock_dt.combine = datetime.combine
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                with patch(
                    "app.services.configuracao_service.ConfiguracaoService.get",
                    return_value="UTC",
                ):
                    resultado = AvisoService.verificar_atrasos()

            assert resultado["processados"] == 0

            avisos = db.session.query(Aviso).filter_by(
                destinatario_id=func.id, tipo=TipoAviso.ATRASO
            ).count()
            assert avisos == 0


# ---------------------------------------------------------------------------
# Teste 6 — processar_dia() reverte auto_debit_aplicado com batida real
# ---------------------------------------------------------------------------


class TestProcessarDiaRevertAutoDebit:

    def test_processar_dia_reverte_auto_debit(self, app, db):
        """processar_dia() deve zerar auto_debit_aplicado e corrigir banco_horas_acumulado
        quando batidas reais são processadas para um dia com débito provisório ativo."""
        from app.services.ponto_service import PontoService

        with app.app_context():
            func = _criar_funcionario(db)

            # Simular estado após verificar_atrasos(): TimeDay com débito provisório
            time_day = TimeDay(
                funcionario_id=func.id,
                shift_date=_DATA_TESTE,
                saldo_final_minutos=-480,
                saldo_calculado_minutos=-480,
                minutos_trabalhados=0,
                expected_minutes_snapshot=480,
                tolerance_snapshot=10,
                auto_debit_aplicado=True,
                needs_review=False,
            )
            db.session.add(time_day)
            func.banco_horas_acumulado = -480
            db.session.commit()

            # Inserir 2 batidas reais (par válido: entrada às 8h, saída às 9h)
            t_base = datetime(_DATA_TESTE.year, _DATA_TESTE.month, _DATA_TESTE.day,
                              tzinfo=timezone.utc)
            for hora in (8, 9):
                entry = TimeEntry(
                    funcionario_id=func.id,
                    shift_date=_DATA_TESTE,
                    punch_time=t_base.replace(hour=hora),
                    source="employee",
                )
                db.session.add(entry)
            db.session.commit()

            # Processar o dia — deve reverter auto_debit_aplicado via delta
            PontoService.processar_dia(func.id, _DATA_TESTE)

            db.session.refresh(time_day)
            db.session.refresh(func)

            assert time_day.auto_debit_aplicado is False
            # Saldo não é mais o débito provisório de -480
            assert time_day.saldo_final_minutos != -480
            # banco_horas_acumulado foi corrigido (não deve mais ser -480)
            assert func.banco_horas_acumulado != -480


# ---------------------------------------------------------------------------
# Teste 7 — AusenciaService.aprovar() reverte débito provisório
# ---------------------------------------------------------------------------


class TestAprovarAusenciaRevertaDebito:

    def test_aprovar_ausencia_reverte_debito_provisorio(self, app, db):
        """AusenciaService.aprovar() deve zerar saldo_final e corrigir banco_horas_acumulado
        quando existe TimeDay com auto_debit_aplicado=True na data da ausência."""
        with app.app_context():
            func = _criar_funcionario(db)

            # Estado após verificar_atrasos()
            time_day = TimeDay(
                funcionario_id=func.id,
                shift_date=_DATA_TESTE,
                saldo_final_minutos=-480,
                saldo_calculado_minutos=-480,
                minutos_trabalhados=0,
                expected_minutes_snapshot=480,
                tolerance_snapshot=10,
                auto_debit_aplicado=True,
            )
            db.session.add(time_day)
            func.banco_horas_acumulado = -480
            db.session.commit()

            # Criar ausência pendente
            ausencia = AusenciaJustificada(
                funcionario_id=func.id,
                data=_DATA_TESTE,
                tipo=TipoAusencia.ATESTADO,
                aprovado=False,
                criado_por_id=func.id,
            )
            db.session.add(ausencia)
            db.session.commit()

            # Aprovar — deve reverter o débito
            AusenciaService.aprovar(ausencia.id, ator_id=func.id)

            db.session.refresh(time_day)
            db.session.refresh(func)

            assert time_day.auto_debit_aplicado is False
            assert time_day.saldo_final_minutos == 0
            assert func.banco_horas_acumulado == 0


class TestVerificarIntegridadeBancoHoras:
    """2.9c — job diário que detecta drift do banco de horas e avisa quem corrige."""

    def _super_admin(self, db, cpf="15350946056"):
        from app.models.role import Role

        role = Role(nome="admin_teste", is_super_admin=True, ativo=True)
        db.session.add(role)
        db.session.flush()
        f = Funcionario(nome="Super Admin", cpf=cpf, ativo=True, role_id=role.id)
        db.session.add(f)
        db.session.commit()
        return f

    def _com_drift(self, db, cpf=_CPF, acumulado=500):
        f = Funcionario(nome="Colaborador Drift", cpf=cpf, ativo=True,
                        banco_horas_acumulado=acumulado)
        db.session.add(f)
        db.session.commit()
        return f

    def test_sem_divergencia_nao_cria_aviso(self, app, db):
        with app.app_context():
            self._super_admin(db)
            _criar_funcionario(db)  # banco zerado, sem TimeDay → esperado 0
            res = AvisoService.verificar_integridade_banco_horas()

            assert res == {"divergencias": 0, "avisos_criados": 0, "erros": []}
            assert db.session.query(Aviso).count() == 0

    def test_cria_aviso_para_super_admin(self, app, db):
        with app.app_context():
            admin = self._super_admin(db)
            self._com_drift(db)
            res = AvisoService.verificar_integridade_banco_horas()

            assert res["divergencias"] == 1
            assert res["avisos_criados"] == 1
            aviso = db.session.query(Aviso).one()
            assert aviso.destinatario_id == admin.id
            assert aviso.tipo == TipoAviso.SISTEMA
            assert "Colaborador Drift" in aviso.titulo
            assert "+500" in aviso.mensagem

    def test_nao_duplica_se_ja_existe_aviso_nao_lido(self, app, db):
        with app.app_context():
            self._super_admin(db)
            self._com_drift(db)
            AvisoService.verificar_integridade_banco_horas()
            # segunda rodada do job, mesma divergência ainda pendente
            res = AvisoService.verificar_integridade_banco_horas()

            assert res["divergencias"] == 1
            assert res["avisos_criados"] == 0
            assert db.session.query(Aviso).count() == 1

    def test_avisa_de_novo_apos_o_anterior_ser_lido(self, app, db):
        with app.app_context():
            self._super_admin(db)
            self._com_drift(db)
            AvisoService.verificar_integridade_banco_horas()
            db.session.query(Aviso).update({"is_lido": True})
            db.session.commit()

            res = AvisoService.verificar_integridade_banco_horas()
            assert res["avisos_criados"] == 1
            assert db.session.query(Aviso).count() == 2

    def test_o_proprio_afetado_nao_recebe_o_aviso(self, app, db):
        with app.app_context():
            # o super admin é quem tem o drift — não deve avisar a si mesmo
            admin = self._super_admin(db)
            admin.banco_horas_acumulado = 300
            db.session.commit()

            res = AvisoService.verificar_integridade_banco_horas()
            assert res["divergencias"] == 1
            assert res["avisos_criados"] == 0

    def test_apos_recalcular_o_job_para_de_avisar(self, app, db):
        from app.services.ponto_service import PontoService

        with app.app_context():
            self._super_admin(db)
            f = self._com_drift(db)
            assert AvisoService.verificar_integridade_banco_horas()["divergencias"] == 1

            PontoService.recalcular_banco_global(f.id, ator_id=None)

            res = AvisoService.verificar_integridade_banco_horas()
            assert res["divergencias"] == 0
            assert res["avisos_criados"] == 0
