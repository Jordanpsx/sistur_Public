# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para RoleService e has_permission.

Cobertura:
- criar: happy path + AuditLog gerado
- criar: falha por nome duplicado
- desativar: happy path + AuditLog gerado
- desativar: proteção de super_admin
- atribuir_ao_funcionario: happy path + AuditLog com permission_change
- remover_do_funcionario: happy path + AuditLog com permission_change
- has_permission: super_admin bypassa todas as verificações
- has_permission: role regular — verdadeiro/falso por permissão
- has_permission: funcionário sem role retorna False
- adicionar_permissao / remover_permissao: idempotência
"""

import pytest

from app.core.models import AuditAction, AuditLog
from app.core.permissions import has_permission
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import ReservaSource
from app.models.role import Role, RolePermission
from app.services.role_service import RoleService


# ---------------------------------------------------------------------------
# Fixtures auxiliares
# ---------------------------------------------------------------------------

@pytest.fixture
def funcionario(app):
    """Cria um Funcionario simples sem role para uso nos testes."""
    with app.app_context():
        f = Funcionario(nome="Teste Silva", cpf="52998224725", ativo=True)
        _db.session.add(f)
        _db.session.commit()
        return f.id  # retorna ID para evitar objetos detached


# ---------------------------------------------------------------------------
# criar
# ---------------------------------------------------------------------------

def test_criar_role_retorna_instancia(app, db):
    """Criar um role deve retornar uma instância persistida com id."""
    with app.app_context():
        role = RoleService.criar(nome="gerente_rh", descricao="Gerente de RH")
        assert role.id is not None
        assert role.nome == "gerente_rh"
        assert role.ativo is True
        assert role.is_super_admin is False


def test_criar_role_gera_audit_log(app, db):
    """Criar um role deve gerar exatamente um AuditLog com action=create."""
    with app.app_context():
        role = RoleService.criar(nome="estoquista", descricao="Controle de estoque")
        count = (
            _db.session.query(AuditLog)
            .filter_by(action=AuditAction.create, module="roles", entity_id=role.id)
            .count()
        )
        assert count == 1


def test_criar_role_nome_duplicado_levanta_erro(app, db):
    """Criar role com nome já existente deve lançar ValueError."""
    with app.app_context():
        RoleService.criar(nome="vendedor")
        with pytest.raises(ValueError, match="já existe"):
            RoleService.criar(nome="vendedor")


def test_criar_super_admin(app, db):
    """Criar role com is_super_admin=True deve ser persistido corretamente."""
    with app.app_context():
        role = RoleService.criar(nome="super_admin", is_super_admin=True)
        assert role.is_super_admin is True


# ---------------------------------------------------------------------------
# desativar
# ---------------------------------------------------------------------------

def test_desativar_role(app, db):
    """Desativar role deve setar ativo=False e gerar AuditLog."""
    with app.app_context():
        role = RoleService.criar(nome="temporario")
        role_id = role.id

        RoleService.desativar(role_id)

        role_atualizado = _db.session.get(Role, role_id)
        assert role_atualizado.ativo is False

        count = (
            _db.session.query(AuditLog)
            .filter_by(action=AuditAction.update, module="roles", entity_id=role_id)
            .count()
        )
        assert count == 1


def test_desativar_super_admin_levanta_erro(app, db):
    """Desativar role de super_admin deve lançar ValueError de proteção."""
    with app.app_context():
        role = RoleService.criar(nome="super_admin", is_super_admin=True)
        with pytest.raises(ValueError, match="super_admin"):
            RoleService.desativar(role.id)


def test_desativar_role_inexistente_levanta_erro(app, db):
    """Desativar role inexistente deve lançar ValueError."""
    with app.app_context():
        with pytest.raises(ValueError, match="não encontrado"):
            RoleService.desativar(9999)


# ---------------------------------------------------------------------------
# adicionar_permissao / remover_permissao
# ---------------------------------------------------------------------------

def test_adicionar_permissao(app, db):
    """Adicionar permissão deve criar registro em sistur_role_permissions."""
    with app.app_context():
        role = RoleService.criar(nome="operador")
        perm = RoleService.adicionar_permissao(role.id, "dashboard", "view")
        assert perm.id is not None
        assert perm.modulo == "dashboard"
        assert perm.acao == "view"


def test_adicionar_permissao_idempotente(app, db):
    """Adicionar permissão já existente deve retornar a existente sem duplicar."""
    with app.app_context():
        role = RoleService.criar(nome="operador2")
        perm1 = RoleService.adicionar_permissao(role.id, "dashboard", "view")
        perm2 = RoleService.adicionar_permissao(role.id, "dashboard", "view")
        assert perm1.id == perm2.id
        count = (
            _db.session.query(RolePermission)
            .filter_by(role_id=role.id, modulo="dashboard", acao="view")
            .count()
        )
        assert count == 1


def test_adicionar_permissao_escopada_por_source(app, db):
    """Permissão escopada deve coexistir com permissão global da mesma ação."""
    with app.app_context():
        role = RoleService.criar(nome="operador_venue")
        source = ReservaSource(name="Cachoeira", display_name="Cachoeira", is_active=True)
        _db.session.add(source)
        _db.session.commit()

        global_perm = RoleService.adicionar_permissao(role.id, "reservas", "view")
        scoped_perm = RoleService.adicionar_permissao(
            role.id,
            "reservas",
            "view",
            source_id=source.id,
        )

        assert global_perm.id != scoped_perm.id
        assert global_perm.source_id is None
        assert scoped_perm.source_id == source.id


def test_remover_permissao(app, db):
    """Remover permissão deve excluir o registro e gerar AuditLog."""
    with app.app_context():
        role = RoleService.criar(nome="gestor")
        RoleService.adicionar_permissao(role.id, "funcionarios", "view")
        RoleService.remover_permissao(role.id, "funcionarios", "view")

        count = (
            _db.session.query(RolePermission)
            .filter_by(role_id=role.id, modulo="funcionarios", acao="view")
            .count()
        )
        assert count == 0


def test_remover_permissao_escopada_preserva_global(app, db):
    """Remover permissão de uma venue não deve remover a permissão global."""
    with app.app_context():
        role = RoleService.criar(nome="operador_venue_remove")
        source = ReservaSource(name="Cachoeira", display_name="Cachoeira", is_active=True)
        _db.session.add(source)
        _db.session.commit()

        RoleService.adicionar_permissao(role.id, "reservas", "view")
        RoleService.adicionar_permissao(role.id, "reservas", "view", source_id=source.id)
        RoleService.remover_permissao(role.id, "reservas", "view", source_id=source.id)

        rows = (
            _db.session.query(RolePermission)
            .filter_by(role_id=role.id, modulo="reservas", acao="view")
            .all()
        )

        assert len(rows) == 1
        assert rows[0].source_id is None


def test_remover_permissao_inexistente_nao_levanta_erro(app, db):
    """Remover permissão que não existe deve encerrar silenciosamente."""
    with app.app_context():
        role = RoleService.criar(nome="visitante")
        RoleService.remover_permissao(role.id, "dashboard", "view")  # não existe — ok


# ---------------------------------------------------------------------------
# atribuir_ao_funcionario
# ---------------------------------------------------------------------------

def test_atribuir_role_ao_funcionario(app, db, funcionario):
    """Atribuir role deve atualizar role_id do funcionário."""
    with app.app_context():
        role = RoleService.criar(nome="funcionario_basico")
        f = RoleService.atribuir_ao_funcionario(funcionario, role.id)
        assert f.role_id == role.id


def test_atribuir_role_gera_audit_permission_change(app, db, funcionario):
    """Atribuir role deve gerar exatamente um AuditLog com action=permission_change."""
    with app.app_context():
        role = RoleService.criar(nome="func_permissao")
        f = RoleService.atribuir_ao_funcionario(funcionario, role.id)

        count = (
            _db.session.query(AuditLog)
            .filter_by(
                action=AuditAction.permission_change,
                module="roles",
                entity_id=f.id,
            )
            .count()
        )
        assert count == 1


def test_atribuir_role_inativo_levanta_erro(app, db, funcionario):
    """Atribuir role inativo deve lançar ValueError."""
    with app.app_context():
        role = RoleService.criar(nome="descontinuado")
        role.ativo = False
        _db.session.commit()

        with pytest.raises(ValueError, match="inativo"):
            RoleService.atribuir_ao_funcionario(funcionario, role.id)


# ---------------------------------------------------------------------------
# remover_do_funcionario
# ---------------------------------------------------------------------------

def test_remover_role_do_funcionario(app, db, funcionario):
    """Remover role deve setar role_id=None e gerar AuditLog."""
    with app.app_context():
        role = RoleService.criar(nome="func_remover")
        RoleService.atribuir_ao_funcionario(funcionario, role.id)

        f = RoleService.remover_do_funcionario(funcionario)
        assert f.role_id is None

        count = (
            _db.session.query(AuditLog)
            .filter_by(
                action=AuditAction.permission_change,
                module="roles",
                entity_id=f.id,
            )
            .count()
        )
        # 1 log de atribuição + 1 log de remoção
        assert count == 2


# ---------------------------------------------------------------------------
# has_permission
# ---------------------------------------------------------------------------

def test_has_permission_super_admin_bypassa_tudo(app, db, funcionario):
    """Super admin deve ter acesso a qualquer módulo.acao sem permissão explícita."""
    with app.app_context():
        role = RoleService.criar(nome="sa_test", is_super_admin=True)
        RoleService.atribuir_ao_funcionario(funcionario, role.id)

        assert has_permission(funcionario, "dashboard", "view") is True
        assert has_permission(funcionario, "funcionarios", "delete") is True
        assert has_permission(funcionario, "modulo_inexistente", "acao_inexistente") is True


def test_has_permission_role_regular_verdadeiro(app, db, funcionario):
    """Funcionário com permissão explícita deve receber True."""
    with app.app_context():
        role = RoleService.criar(nome="role_basico")
        RoleService.adicionar_permissao(role.id, "dashboard", "view")
        RoleService.atribuir_ao_funcionario(funcionario, role.id)

        assert has_permission(funcionario, "dashboard", "view") is True


def test_has_permission_role_regular_falso(app, db, funcionario):
    """Funcionário sem permissão específica deve receber False."""
    with app.app_context():
        role = RoleService.criar(nome="role_restrito")
        RoleService.adicionar_permissao(role.id, "dashboard", "view")
        RoleService.atribuir_ao_funcionario(funcionario, role.id)

        assert has_permission(funcionario, "funcionarios", "create") is False


def test_has_permission_reservas_view_aceita_escopo_de_source(app, db, funcionario):
    """reservas.view escopado libera entrada no módulo; a policy filtra a venue."""
    with app.app_context():
        role = RoleService.criar(nome="reservas_view_source")
        source = ReservaSource(name="Cachoeira", display_name="Cachoeira", is_active=True)
        _db.session.add(source)
        _db.session.commit()

        RoleService.adicionar_permissao(
            role.id,
            "reservas",
            "view",
            source_id=source.id,
        )
        RoleService.atribuir_ao_funcionario(funcionario, role.id)

        assert has_permission(funcionario, "reservas", "view") is True
        assert has_permission(funcionario, "reservas", "edit") is False


def test_has_permission_sem_role_retorna_false(app, db, funcionario):
    """
    Funcionário sem role atribuído deve retornar False (quando já existe super_admin).

    Nota: Se nenhum super_admin existir, o bootstrap mode ativa e retorna True.
    Este teste primeiro cria um super_admin para desativar o bootstrap.
    """
    with app.app_context():
        # Criar um super_admin para desativar bootstrap mode
        role = RoleService.criar(nome="sa_para_teste", is_super_admin=True)

        # Agora funcionário sem role deve retornar False
        assert has_permission(funcionario, "dashboard", "view") is False


def test_has_permission_role_inativo_retorna_false(app, db, funcionario):
    """Funcionário com role inativo deve receber False mesmo tendo a permissão."""
    with app.app_context():
        role = RoleService.criar(nome="role_vai_inativar")
        RoleService.adicionar_permissao(role.id, "dashboard", "view")

        # Atribuir o role ao funcionário
        f = _db.session.get(Funcionario, funcionario)
        f.role_id = role.id
        _db.session.commit()

        # Inativar o role diretamente (sem chamar desativar pois é super_admin=False)
        role.ativo = False
        _db.session.commit()

        assert has_permission(funcionario, "dashboard", "view") is False


# ---------------------------------------------------------------------------
# Bootstrap mode (primeiro super_admin do sistema)
# ---------------------------------------------------------------------------

def test_bootstrap_mode_sem_role_primeiro_usuario(app):
    """
    Bootstrap mode: sem nenhum super_admin no banco, funcionário sem role
    tem acesso total a qualquer módulo/ação.

    Caso de uso: permitir que o primeiro usuário do sistema se autopromocionalize
    temporariamente para configurar roles e permissões.
    """
    with app.app_context():
        # Criar um funcionário SEM role
        f = Funcionario(nome="Primeiro Usuário", cpf="11144477735", ativo=True)
        _db.session.add(f)
        _db.session.commit()
        fid = f.id

        # Nenhum super_admin existe → bootstrap mode ativado
        assert has_permission(fid, "dashboard", "view") is True
        assert has_permission(fid, "funcionarios", "create") is True
        assert has_permission(fid, "qualquer_modulo", "qualquer_acao") is True


def test_bootstrap_mode_desativa_apos_primeiro_super_admin(app, db):
    """
    Após criar o primeiro super_admin, o bootstrap mode desativa.
    Funcionários sem role voltam a ter acesso negado.
    """
    with app.app_context():
        # Criar funcionário 1 (sem role) — será super_admin implícito
        f1 = Funcionario(nome="Usuário 1", cpf="11144477735", ativo=True)
        _db.session.add(f1)
        _db.session.commit()
        f1_id = f1.id

        # Verificar que tem acesso (bootstrap mode)
        assert has_permission(f1_id, "dashboard", "view") is True

        # Agora criar um super_admin explícito
        role_sa = RoleService.criar(nome="super_admin_real", is_super_admin=True)
        RoleService.atribuir_ao_funcionario(f1_id, role_sa.id)

        # Criar funcionário 2 (sem role) após super_admin existir
        f2 = Funcionario(nome="Usuário 2", cpf="64110585900", ativo=True)
        _db.session.add(f2)
        _db.session.commit()
        f2_id = f2.id

        # Funcionário 2 SEM role não tem acesso (bootstrap desativado)
        assert has_permission(f2_id, "dashboard", "view") is False
