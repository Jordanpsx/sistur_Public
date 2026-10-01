import pytest
from datetime import datetime, date, time, timezone
from app.models.ajuste_ponto import AjustePontoRequest, StatusAjuste
from app.models.ponto import TimeEntry
from app.models.avisos import Aviso, TipoAviso
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission
from app.services.ajuste_ponto_service import AjustePontoService
from app.extensions import db

@pytest.fixture
def supervisor_id(app):
    """Cria um supervisor com permissão de ajuste e retorna o ID."""
    with app.app_context():
        role = Role(nome="Supervisor", is_super_admin=False)
        db.session.add(role)
        db.session.flush()
        
        perm = RolePermission(role_id=role.id, modulo="ajuste_ponto", acao="aprovar")
        db.session.add(perm)
        
        sup = Funcionario(
            nome="Super Visor",
            cpf="111.111.111-11",
            email="supervisor@teste.com",
            role_id=role.id,
            ativo=True
        )
        db.session.add(sup)
        db.session.commit()
        return sup.id

@pytest.fixture
def funcionario_comum_id(app):
    """Cria um funcionário comum e retorna o ID."""
    with app.app_context():
        role = Role(nome="Comum", is_super_admin=False)
        db.session.add(role)
        db.session.flush()
        
        func = Funcionario(
            nome="Jose Comum",
            cpf="222.222.222-22",
            email="jose@teste.com",
            role_id=role.id,
            ativo=True
        )
        db.session.add(func)
        db.session.commit()
        return func.id

def test_criar_solicitacao(app, db, funcionario_comum_id, supervisor_id):
    """Testa se a criação da solicitação de ajuste funciona e cria um aviso associado."""
    with app.app_context():
        # Cria a solicitação
        solicitacao = AjustePontoService.criar_solicitacao(
            funcionario_id=funcionario_comum_id,
            data_solicitada=date(2023, 10, 25),
            horario_solicitado=datetime(2023, 10, 25, 8, 0, tzinfo=timezone.utc),
            motivo="Esqueci de bater o ponto na chegada",
            tipo_ponto='clock_in',
        )

        assert solicitacao.id is not None
        assert solicitacao.funcionario_id == funcionario_comum_id
        assert solicitacao.status == StatusAjuste.PENDENTE
        assert solicitacao.motivo == "Esqueci de bater o ponto na chegada"

        # Verifica se o aviso foi criado para os supervisores (ou no caso, quem tem permissão)
        avisos = Aviso.query.filter_by(tipo=TipoAviso.AJUSTE_PONTO).all()
        assert len(avisos) > 0
        assert avisos[0].ajuste_ponto_id == solicitacao.id


def test_aprovar_ajuste(app, db, funcionario_comum_id, supervisor_id):
    """Testa aprovar uma solicitação, que deve criar/atualizar um TimeEntry."""
    with app.app_context():
        # Arranjo
        solicitacao = AjustePontoService.criar_solicitacao(
            funcionario_id=funcionario_comum_id,
            data_solicitada=date(2023, 10, 25),
            horario_solicitado=datetime(2023, 10, 25, 8, 15, tzinfo=timezone.utc),
            motivo="Esqueci de bater o ponto",
            tipo_ponto='clock_in',
        )
        
        # Ação
        result = AjustePontoService.aprovar(
            ajuste_id=solicitacao.id,
            supervisor_id=supervisor_id,
        )
        
        # Assert
        assert result.status == StatusAjuste.APROVADO
        assert result.supervisor_id == supervisor_id
        assert result.resolvido_em is not None
        
        # Verifica no banco a batida
        entry = TimeEntry.query.filter_by(funcionario_id=funcionario_comum_id).order_by(TimeEntry.id.desc()).first()
        assert entry is not None
        assert entry.funcionario_id == funcionario_comum_id
        assert entry.source.name == 'admin'


def test_rejeitar_ajuste(app, db, funcionario_comum_id, supervisor_id):
    """Testa rejeitar um ajuste."""
    with app.app_context():
        # Arranjo
        solicitacao = AjustePontoService.criar_solicitacao(
            funcionario_id=funcionario_comum_id,
            data_solicitada=date(2023, 10, 27),
            horario_solicitado=datetime(2023, 10, 27, 18, 0, tzinfo=timezone.utc),
            motivo="Saída justificativa sem prova",
            tipo_ponto='clock_out',
        )
        
        # Ação
        result = AjustePontoService.rejeitar(
            ajuste_id=solicitacao.id,
            supervisor_id=supervisor_id,
            motivo_rejeicao="Não comprovado",
        )
        
        # Assert
        assert result.status == StatusAjuste.REJEITADO
        assert result.motivo_rejeicao == "Não comprovado"
        assert result.supervisor_id == supervisor_id



# ---------------------------------------------------------------------------
# 2.11 — Aprovações compartilhadas: encerrar avisos-irmãos ao resolver
# ---------------------------------------------------------------------------

@pytest.fixture
def segundo_supervisor_id(app):
    """Cria um segundo supervisor com permissão de aprovar e retorna o ID."""
    with app.app_context():
        role = Role(nome="Supervisor B", is_super_admin=False)
        db.session.add(role)
        db.session.flush()
        db.session.add(RolePermission(role_id=role.id, modulo="ajuste_ponto", acao="aprovar"))
        sup = Funcionario(
            nome="Segundo Supervisor",
            cpf="333.333.333-33",
            email="sup2@teste.com",
            role_id=role.id,
            ativo=True,
        )
        db.session.add(sup)
        db.session.commit()
        return sup.id


def _solicitar(funcionario_id):
    return AjustePontoService.criar_solicitacao(
        funcionario_id=funcionario_id,
        data_solicitada=date(2026, 7, 1),
        horario_solicitado=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        motivo="Esqueci de bater",
        tipo_ponto="clock_in",
    )


def test_avisos_de_aprovadores_carregam_referencia(app, db, funcionario_comum_id,
                                                   supervisor_id, segundo_supervisor_id):
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        avisos = Aviso.query.filter_by(referencia_tipo="ajuste_ponto", referencia_id=req.id).all()
        destinatarios = {a.destinatario_id for a in avisos}
        assert supervisor_id in destinatarios
        assert segundo_supervisor_id in destinatarios


def test_aprovar_encerra_aviso_dos_demais_aprovadores(app, db, funcionario_comum_id,
                                                      supervisor_id, segundo_supervisor_id):
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        AjustePontoService.aprovar(req.id, supervisor_id)

        # O aviso original do segundo supervisor foi marcado como lido
        pendentes_b = Aviso.query.filter_by(
            destinatario_id=segundo_supervisor_id,
            referencia_tipo="ajuste_ponto",
            referencia_id=req.id,
            is_lido=False,
        ).count()
        assert pendentes_b == 0


def test_aprovar_gera_desfecho_para_outros_aprovadores(app, db, funcionario_comum_id,
                                                       supervisor_id, segundo_supervisor_id):
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        AjustePontoService.aprovar(req.id, supervisor_id)

        desfechos_b = Aviso.query.filter_by(destinatario_id=segundo_supervisor_id).filter(
            Aviso.titulo == "Pedido de ajuste resolvido"
        ).all()
        assert len(desfechos_b) == 1
        assert "aprovada" in desfechos_b[0].mensagem

        # Quem resolveu não recebe desfecho de si mesmo
        desfechos_a = Aviso.query.filter_by(destinatario_id=supervisor_id).filter(
            Aviso.titulo == "Pedido de ajuste resolvido"
        ).count()
        assert desfechos_a == 0


def test_rejeitar_tambem_encerra_e_gera_desfecho(app, db, funcionario_comum_id,
                                                 supervisor_id, segundo_supervisor_id):
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        AjustePontoService.rejeitar(req.id, supervisor_id, "Horário incompatível")

        pendentes_b = Aviso.query.filter_by(
            destinatario_id=segundo_supervisor_id,
            referencia_tipo="ajuste_ponto",
            referencia_id=req.id,
            is_lido=False,
        ).count()
        assert pendentes_b == 0

        desfechos_b = Aviso.query.filter_by(destinatario_id=segundo_supervisor_id).filter(
            Aviso.titulo == "Pedido de ajuste resolvido"
        ).all()
        assert len(desfechos_b) == 1
        assert "rejeitada" in desfechos_b[0].mensagem


def test_segundo_aprovador_recebe_feedback_de_ja_resolvido(app, db, funcionario_comum_id,
                                                           supervisor_id, segundo_supervisor_id):
    """Concorrência: o primeiro a resolver vence; o segundo sabe quem resolveu."""
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        AjustePontoService.aprovar(req.id, supervisor_id)
        with pytest.raises(ValueError, match="já foi aprovada por Super Visor"):
            AjustePontoService.aprovar(req.id, segundo_supervisor_id)
        with pytest.raises(ValueError, match="já foi aprovada por Super Visor"):
            AjustePontoService.rejeitar(req.id, segundo_supervisor_id, "tarde demais")


def test_solicitante_nao_recebe_desfecho_generico(app, db, funcionario_comum_id,
                                                  supervisor_id, segundo_supervisor_id):
    """O colaborador recebe o aviso pessoal de resultado, não o desfecho genérico."""
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        AjustePontoService.aprovar(req.id, supervisor_id)
        desfechos = Aviso.query.filter_by(destinatario_id=funcionario_comum_id).filter(
            Aviso.titulo == "Pedido de ajuste resolvido"
        ).count()
        assert desfechos == 0
        pessoais = Aviso.query.filter_by(destinatario_id=funcionario_comum_id).filter(
            Aviso.titulo.contains("aprovado")
        ).count()
        assert pessoais == 1


def test_encerrar_por_referencia_audita(app, db, funcionario_comum_id,
                                        supervisor_id, segundo_supervisor_id):
    from app.core.models import AuditLog, AuditAction
    with app.app_context():
        req = _solicitar(funcionario_comum_id)
        AjustePontoService.aprovar(req.id, supervisor_id)
        log = db.session.query(AuditLog).filter_by(
            action=AuditAction.update, module="avisos", entity_id=req.id
        ).first()
        assert log is not None
        assert log.previous_state["action"] == "encerrar_por_referencia"
