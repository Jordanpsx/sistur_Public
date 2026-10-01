# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes do PontoImportService — importação em lote do relógio de ponto externo.

Cobre as quatro regras de negócio do serviço: deduplicação, descarte de dias com
mais de 4 batidas, rejeição de funcionário inválido e auditoria coletiva.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.models import AuditAction, AuditLog
from app.extensions import db
from app.models.avisos import Aviso
from app.models.ponto import ProcessingStatus, PunchSource, PunchType, TimeDay, TimeEntry
from app.services.ponto_import_service import PontoImportService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _funcionario(nome: str = "Carlos Exemplo", cpf: str = "52998224725", ativo: bool = True):
    """Cria um funcionário direto no banco, sem passar pelo service."""
    from app.models.funcionario import Funcionario

    f = Funcionario(nome=nome, cpf=cpf, ativo=ativo, minutos_esperados_dia=480)
    db.session.add(f)
    db.session.flush()
    return f


def _batidas(funcionario_id: int, dia: str, *horas: str) -> list[dict]:
    """Monta o payload no formato que o programa externo envia."""
    return [
        {"funcionario_id": funcionario_id, "punch_time": f"{dia}T{h}:00"}
        for h in horas
    ]


class TestImportarLote:
    def test_happy_path_grava_as_quatro_batidas(self, app, db):
        with app.app_context():
            f = _funcionario()
            resultado = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
            )

            assert resultado["importadas"] == 4
            assert resultado["duplicadas"] == 0
            assert resultado["dias_ignorados"] == 0

            gravadas = db.session.query(TimeEntry).filter_by(funcionario_id=f.id).all()
            assert len(gravadas) == 4
            assert {e.source for e in gravadas} == {PunchSource.IMPORT}

    def test_tipos_saem_na_sequencia_clt(self, app, db):
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
            )

            gravadas = (
                db.session.query(TimeEntry)
                .filter_by(funcionario_id=f.id)
                .order_by(TimeEntry.punch_time)
                .all()
            )
            assert [e.punch_type for e in gravadas] == [
                PunchType.clock_in,
                PunchType.lunch_start,
                PunchType.lunch_end,
                PunchType.clock_out,
            ]

    def test_converte_hora_local_do_relogio_para_utc(self, app, db):
        with app.app_context():
            f = _funcionario()
            PostoImport = PontoImportService
            PostoImport.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00"),
                timezone_relogio="America/Sao_Paulo",
            )

            entrada = db.session.query(TimeEntry).filter_by(funcionario_id=f.id).one()
            gravado = entrada.punch_time
            if gravado.tzinfo is None:
                gravado = gravado.replace(tzinfo=timezone.utc)
            # America/Sao_Paulo em agosto = UTC-3 → 08:00 local vira 11:00 UTC.
            assert gravado.astimezone(timezone.utc).hour == 11

    def test_fuso_desconhecido_e_recusado(self, app, db):
        with app.app_context():
            f = _funcionario()
            with pytest.raises(ValueError, match="Fuso horário desconhecido"):
                PontoImportService.importar_lote(
                    _batidas(f.id, "2026-08-03", "08:00"),
                    timezone_relogio="Marte/Olympus",
                )

    def test_lote_vazio_e_recusado(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="vazia"):
                PontoImportService.importar_lote([])

    def test_horario_invalido_e_recusado(self, app, db):
        with app.app_context():
            f = _funcionario()
            with pytest.raises(ValueError, match="Horário inválido"):
                PontoImportService.importar_lote(
                    [{"funcionario_id": f.id, "punch_time": "ontem de manhã"}]
                )


class TestRegra1Deduplicacao:
    def test_reimportar_o_mesmo_lote_nao_duplica(self, app, db):
        with app.app_context():
            f = _funcionario()
            lote = _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")

            PontoImportService.importar_lote(lote)
            segundo = PontoImportService.importar_lote(lote)

            assert segundo["importadas"] == 0
            assert segundo["duplicadas"] == 4
            assert db.session.query(TimeEntry).filter_by(funcionario_id=f.id).count() == 4

    def test_dedup_ignora_o_punch_type(self, app, db):
        """A UniqueConstraint inclui punch_type e por isso não pega reimportação.

        Guarda de regressão: se a deduplicação passar a depender do tipo, as
        batidas repetidas entram como 'extra' e a jornada dobra em silêncio.
        """
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(_batidas(f.id, "2026-08-03", "08:00", "12:00"))

            entrada = (
                db.session.query(TimeEntry)
                .filter_by(funcionario_id=f.id)
                .order_by(TimeEntry.punch_time)
                .first()
            )
            entrada.punch_type = PunchType.extra
            db.session.commit()

            segundo = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00")
            )
            assert segundo["importadas"] == 0
            assert segundo["duplicadas"] == 2

    def test_importa_apenas_as_batidas_novas(self, app, db):
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(_batidas(f.id, "2026-08-03", "08:00", "12:00"))
            segundo = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
            )

            assert segundo["importadas"] == 2
            assert segundo["duplicadas"] == 2
            assert db.session.query(TimeEntry).filter_by(funcionario_id=f.id).count() == 4


class TestRegra2DiaInconsistente:
    def test_dia_com_cinco_batidas_e_pulado_inteiro(self, app, db):
        with app.app_context():
            f = _funcionario()
            resultado = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "07:37", "13:49", "18:03", "18:06", "19:29")
            )

            assert resultado["importadas"] == 0
            assert resultado["dias_ignorados"] == 1
            assert resultado["batidas_ignoradas"] == 5
            # Nenhuma batida daquele dia entrou — nem as quatro primeiras.
            assert db.session.query(TimeEntry).filter_by(funcionario_id=f.id).count() == 0

    def test_dia_inconsistente_nao_contamina_os_outros_dias(self, app, db):
        with app.app_context():
            f = _funcionario()
            lote = (
                _batidas(f.id, "2026-08-03", "07:37", "13:49", "18:03", "18:06", "19:29")
                + _batidas(f.id, "2026-08-04", "08:00", "12:00", "14:00", "17:00")
            )
            resultado = PontoImportService.importar_lote(lote)

            assert resultado["importadas"] == 4
            assert resultado["dias_ignorados"] == 1

            gravadas = db.session.query(TimeEntry).filter_by(funcionario_id=f.id).all()
            assert {e.shift_date for e in gravadas} == {date(2026, 8, 4)}

    def test_batidas_ja_gravadas_contam_para_o_limite(self, app, db):
        """3 batidas no banco + 2 novas = 5 no dia → o dia é pulado."""
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00")
            )
            segundo = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "17:00", "19:00")
            )

            assert segundo["importadas"] == 0
            assert segundo["dias_ignorados"] == 1
            assert db.session.query(TimeEntry).filter_by(funcionario_id=f.id).count() == 3

    def test_exatamente_quatro_batidas_passa(self, app, db):
        with app.app_context():
            f = _funcionario()
            resultado = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
            )
            assert resultado["dias_ignorados"] == 0
            assert resultado["importadas"] == 4

    def test_dia_ja_inconsistente_sem_batida_nova_nao_gera_aviso(self, app, db):
        """A regra só vale para o que a importação geraria, não para o passado."""
        with app.app_context():
            f = _funcionario()
            for hora in ("08:00", "12:00", "14:00", "17:00", "19:00"):
                db.session.add(
                    TimeEntry(
                        funcionario_id=f.id,
                        punch_time=datetime(2026, 8, 3, int(hora[:2]) + 3, 0, tzinfo=timezone.utc),
                        shift_date=date(2026, 8, 3),
                        punch_type=PunchType.extra,
                        source=PunchSource.KIOSK,
                        processing_status=ProcessingStatus.PENDENTE,
                    )
                )
            db.session.commit()

            resultado = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00")
            )

            assert resultado["duplicadas"] == 2
            assert resultado["dias_ignorados"] == 0
            assert db.session.query(Aviso).count() == 0

    def test_detalhe_da_inconsistencia_identifica_pessoa_e_dia(self, app, db):
        with app.app_context():
            f = _funcionario(nome="Otavio de Exemplo")
            resultado = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "07:00", "12:00", "14:00", "17:00", "19:00")
            )

            detalhe = resultado["inconsistencias"][0]
            assert detalhe["funcionario_nome"] == "Otavio de Exemplo"
            assert detalhe["shift_date"] == "2026-08-03"
            assert detalhe["batidas_no_dia"] == 5


class TestRegra3FuncionarioInvalido:
    def test_funcionario_inexistente_rejeita_o_lote_inteiro(self, app, db):
        with app.app_context():
            f = _funcionario()
            lote = (
                _batidas(f.id, "2026-08-03", "08:00", "12:00")
                + _batidas(99999, "2026-08-03", "08:00", "12:00")
            )
            with pytest.raises(ValueError, match="não existem"):
                PontoImportService.importar_lote(lote)

            # Nada foi gravado, nem as batidas do funcionário válido.
            assert db.session.query(TimeEntry).count() == 0

    def test_funcionario_inativo_rejeita_o_lote(self, app, db):
        with app.app_context():
            f = _funcionario(nome="Desligado", cpf="11144477735", ativo=False)
            with pytest.raises(ValueError, match="inativos"):
                PontoImportService.importar_lote(
                    _batidas(f.id, "2026-08-03", "08:00", "12:00")
                )
            assert db.session.query(TimeEntry).count() == 0

    def test_mensagem_lista_todos_os_ids_de_uma_vez(self, app, db):
        with app.app_context():
            lote = _batidas(88888, "2026-08-03", "08:00") + _batidas(99999, "2026-08-03", "08:00")
            with pytest.raises(ValueError) as exc:
                PontoImportService.importar_lote(lote)
            assert "88888" in str(exc.value)
            assert "99999" in str(exc.value)


class TestRegra4Auditoria:
    def test_um_unico_audit_log_por_importacao(self, app, db):
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
                + _batidas(f.id, "2026-08-04", "08:00", "12:00", "14:00", "17:00")
            )

            logs = (
                db.session.query(AuditLog)
                .filter_by(module="ponto", action=AuditAction.create)
                .all()
            )
            assert len(logs) == 1
            assert logs[0].new_state["operacao"] == "importacao_lote"
            assert logs[0].new_state["importadas"] == 8

    def test_audit_registra_as_inconsistencias(self, app, db):
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "07:00", "12:00", "14:00", "17:00", "19:00")
            )

            log = db.session.query(AuditLog).filter_by(module="ponto").one()
            assert log.new_state["dias_ignorados"] == 1
            assert log.new_state["batidas_ignoradas"] == 5

    def test_audit_registra_dispositivo_e_periodo(self, app, db):
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00"),
                dispositivo="REP-02",
                periodo_inicio=date(2026, 8, 1),
                periodo_fim=date(2026, 8, 16),
            )

            log = db.session.query(AuditLog).filter_by(module="ponto").one()
            assert log.new_state["dispositivo"] == "REP-02"
            assert log.new_state["periodo_inicio"] == "2026-08-01"


class TestAvisoDeInconsistencia:
    def _com_super_admin(self):
        from app.models.funcionario import Funcionario
        from app.models.role import Role

        role = Role(nome="super_admin", is_super_admin=True)
        db.session.add(role)
        db.session.flush()

        admin = Funcionario(nome="RH Chefe", cpf="11144477735", ativo=True, role_id=role.id)
        db.session.add(admin)
        db.session.flush()
        return admin

    def test_dia_pulado_gera_aviso(self, app, db):
        with app.app_context():
            admin = self._com_super_admin()
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "07:00", "12:00", "14:00", "17:00", "19:00")
            )

            avisos = db.session.query(Aviso).filter_by(destinatario_id=admin.id).all()
            assert len(avisos) == 1
            assert "NÃO" in avisos[0].mensagem
            assert "03/08/2026" in avisos[0].mensagem

    def test_um_aviso_por_destinatario_e_nao_por_dia(self, app, db):
        """Vinte dias inconsistentes não podem virar vinte notificações."""
        with app.app_context():
            admin = self._com_super_admin()
            f = _funcionario()
            lote = []
            for dia in range(3, 9):
                lote += _batidas(
                    f.id, f"2026-08-0{dia}", "07:00", "12:00", "14:00", "17:00", "19:00"
                )
            resultado = PontoImportService.importar_lote(lote)

            assert resultado["dias_ignorados"] == 6
            assert db.session.query(Aviso).filter_by(destinatario_id=admin.id).count() == 1

    def test_importacao_limpa_nao_gera_aviso(self, app, db):
        with app.app_context():
            self._com_super_admin()
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
            )
            assert db.session.query(Aviso).count() == 0


class TestRecalculoDiario:
    def test_processa_o_dia_e_calcula_o_saldo(self, app, db):
        with app.app_context():
            f = _funcionario()
            PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00", "17:00")
            )

            dia = (
                db.session.query(TimeDay)
                .filter_by(funcionario_id=f.id, shift_date=date(2026, 8, 3))
                .one()
            )
            # 08:00→12:00 (240) + 14:00→17:00 (180) = 420 min
            assert dia.minutos_trabalhados == 420
            assert not dia.needs_review

    def test_dia_impar_entra_em_revisao(self, app, db):
        with app.app_context():
            f = _funcionario()
            resultado = PontoImportService.importar_lote(
                _batidas(f.id, "2026-08-03", "08:00", "12:00", "14:00")
            )

            assert resultado["dias_em_revisao"] == 1
            dia = (
                db.session.query(TimeDay)
                .filter_by(funcionario_id=f.id, shift_date=date(2026, 8, 3))
                .one()
            )
            assert dia.needs_review
