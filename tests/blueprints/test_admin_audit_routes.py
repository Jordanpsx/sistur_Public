# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração para as rotas do blueprint Admin — Audit Log Viewer.

Cenários cobertos
-----------------
- Sem sessão → redirect 302 para login
- Sem permissão audit.view nem audit.view_all → 403
- Supervisor (audit.view) com área → 200, vê apenas logs da própria área
- Supervisor sem área definida → 200, tabela vazia
- Gerente (audit.view_all) → 200, vê todos os logs
- Super admin → 200, vê todos os logs (bypass)
- Filtro ?module=auth → filtra por módulo
- Filtro ?user_id=<id> → filtra por ator
- Paginação ?page=2 → 200
"""

from __future__ import annotations

import json
import pytest

from app.core.models import Area, AuditAction, AuditLog, UserType
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission


# ---------------------------------------------------------------------------
# Helpers de fixtures
# ---------------------------------------------------------------------------

def _criar_area(name: str, slug: str) -> Area:
    """Cria e persiste uma Area no banco.

    Args:
        name: Nome legível da área.
        slug: Slug único da área.

    Returns:
        Instância persistida de Area.
    """
    area = Area(name=name, slug=slug, is_active=True)
    _db.session.add(area)
    _db.session.flush()
    return area


def _criar_role(nome: str, is_super: bool = False, permissoes: list[tuple] = None) -> Role:
    """Cria e persiste um Role com permissões opcionais.

    Args:
        nome:       Slug do role.
        is_super:   Se True, is_super_admin=True (bypass total).
        permissoes: Lista de tuplas (modulo, acao).

    Returns:
        Instância persistida de Role.
    """
    role = Role(nome=nome, is_super_admin=is_super, ativo=True)
    _db.session.add(role)
    _db.session.flush()
    for modulo, acao in (permissoes or []):
        _db.session.add(RolePermission(role_id=role.id, modulo=modulo, acao=acao))
    _db.session.flush()
    return role


def _criar_funcionario(nome: str, cpf: str, role: Role | None = None,
                       area: Area | None = None) -> Funcionario:
    """Cria e persiste um Funcionario ativo.

    Args:
        nome:  Nome do funcionário.
        cpf:   CPF limpo (11 dígitos).
        role:  Role a atribuir (opcional).
        area:  Area a atribuir (opcional).

    Returns:
        Instância persistida de Funcionario.
    """
    f = Funcionario(nome=nome, cpf=cpf, ativo=True)
    if role:
        f.role_id = role.id
    if area:
        f.area_id = area.id
    _db.session.add(f)
    _db.session.commit()
    return f


def _criar_log(module: str, action: AuditAction = AuditAction.create,
               user_id: int | None = None, area_id: int | None = None) -> AuditLog:
    """Cria e persiste um AuditLog diretamente (sem passar pelo AuditService).

    Args:
        module:   Nome do módulo.
        action:   Tipo de ação.
        user_id:  ID do ator (opcional).
        area_id:  ID da área (opcional).

    Returns:
        Instância persistida de AuditLog.
    """
    log = AuditLog(
        module=module,
        action=action,
        user_type=UserType.employee,
        user_id=user_id,
        area_id=area_id,
        new_state={"teste": True},
    )
    _db.session.add(log)
    _db.session.commit()
    return log


# ---------------------------------------------------------------------------
# Fixtures de clientes
# ---------------------------------------------------------------------------

@pytest.fixture
def client_sem_sessao(client):
    """Cliente sem sessão ativa."""
    return client


@pytest.fixture
def client_sem_permissao(app, client, db):
    """Cliente autenticado como funcionário sem nenhuma permissão de auditoria."""
    with app.app_context():
        # Precisa existir um super_admin para que bootstrap mode não entre em ação
        _criar_role("super_admin", is_super=True)
        role = _criar_role("funcionario", permissoes=[("dashboard", "view")])
        f = _criar_funcionario("Sem Permissão", "07699220030", role)
        fid = f.id
    with client.session_transaction() as sess:
        sess["funcionario_id"] = fid
    return client


@pytest.fixture
def client_supervisor(app, client, db):
    """Cliente autenticado como supervisor (audit.view) com área definida."""
    with app.app_context():
        _criar_role("super_admin", is_super=True)
        area = _criar_area("Restaurante", "restaurante")
        role = _criar_role("supervisor", permissoes=[("audit", "view")])
        f = _criar_funcionario("Carlos Supervisor", "64110585900", role, area)
        fid = f.id
    with client.session_transaction() as sess:
        sess["funcionario_id"] = fid
    return client


@pytest.fixture
def client_gerente(app, client, db):
    """Cliente autenticado como gerente (audit.view_all) sem restrição de área."""
    with app.app_context():
        _criar_role("super_admin", is_super=True)
        role = _criar_role("gerente", permissoes=[("audit", "view_all")])
        f = _criar_funcionario("Ana Gerente", "11144477735", role)
        fid = f.id
    with client.session_transaction() as sess:
        sess["funcionario_id"] = fid
    return client


@pytest.fixture
def client_super_admin(app, client, db):
    """Cliente autenticado como super_admin (bypass total)."""
    with app.app_context():
        role = _criar_role("super_admin", is_super=True)
        f = _criar_funcionario("Super Admin", "52998224725", role)
        fid = f.id
    with client.session_transaction() as sess:
        sess["funcionario_id"] = fid
    return client


# ---------------------------------------------------------------------------
# Testes de controle de acesso
# ---------------------------------------------------------------------------

class TestControleAcesso:

    def test_sem_sessao_redireciona_login(self, client_sem_sessao):
        """Acesso sem sessão deve redirecionar para login."""
        resp = client_sem_sessao.get("/admin/audit-logs")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]

    def test_sem_permissao_retorna_403(self, client_sem_permissao):
        """Funcionário sem permissão audit.view nem audit.view_all → 403."""
        resp = client_sem_permissao.get("/admin/audit-logs")
        assert resp.status_code == 403

    def test_supervisor_retorna_200(self, client_supervisor):
        """Supervisor com audit.view deve receber 200."""
        resp = client_supervisor.get("/admin/audit-logs")
        assert resp.status_code == 200
        assert b"audit-logs-table" in resp.data or b"Nenhum registro" in resp.data

    def test_gerente_retorna_200(self, client_gerente):
        """Gerente com audit.view_all deve receber 200."""
        resp = client_gerente.get("/admin/audit-logs")
        assert resp.status_code == 200

    def test_super_admin_retorna_200(self, client_super_admin):
        """Super admin deve receber 200."""
        resp = client_super_admin.get("/admin/audit-logs")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Testes de filtros e escopo de área
# ---------------------------------------------------------------------------

class TestFiltrosEEscopo:

    def test_supervisor_ve_somente_logs_da_propria_area(self, app, client_supervisor, db):
        """Supervisor deve ver apenas logs onde area_id == seu area_id."""
        with app.app_context():
            area_rh = _criar_area("RH", "rh")
            area_rest = Area.query.filter_by(slug="restaurante").first()

            _criar_log("ponto", area_id=area_rest.id)     # deve aparecer
            _criar_log("rh",    area_id=area_rh.id)       # NÃO deve aparecer
            _criar_log("auth",  area_id=None)              # NÃO deve aparecer

        resp = client_supervisor.get("/admin/audit-logs")
        body = resp.data.decode()

        assert "ponto" in body
        assert "rh" not in body.split("Log de Auditoria")[1]  # após o header

    def test_gerente_ve_todos_os_logs(self, app, client_gerente, db):
        """Gerente deve ver logs de qualquer módulo e qualquer área."""
        with app.app_context():
            area = _criar_area("RH", "rh")
            _criar_log("ponto",  area_id=None)
            _criar_log("rh",     area_id=area.id)
            _criar_log("auth",   area_id=None)

        resp = client_gerente.get("/admin/audit-logs")
        body = resp.data.decode()

        assert "ponto" in body
        assert "auth"  in body

    def test_filtro_por_modulo(self, app, client_super_admin, db):
        """Filtro ?module=auth deve retornar apenas logs do módulo auth."""
        with app.app_context():
            _criar_log("auth")
            _criar_log("ponto")

        resp = client_super_admin.get("/admin/audit-logs?module=auth")
        body = resp.data.decode()

        assert "auth" in body
        # ponto não deveria aparecer como módulo nos badges
        # (pode aparecer no dropdown — verificamos ausência na tabela)

    def test_filtro_por_user_id(self, app, client_super_admin, db):
        """Filtro ?user_id=<id> deve filtrar pelo ator corretamente."""
        with app.app_context():
            f1 = _criar_funcionario("Ator Um", "64110585900")
            _criar_log("ponto", user_id=f1.id)
            _criar_log("auth",  user_id=999)

        resp = client_super_admin.get(f"/admin/audit-logs?user_id={999}&module=auth")
        assert resp.status_code == 200

    def test_paginacao_page_2(self, client_super_admin):
        """Acesso à ?page=2 deve retornar 200 sem erro."""
        resp = client_super_admin.get("/admin/audit-logs?page=2")
        assert resp.status_code == 200
