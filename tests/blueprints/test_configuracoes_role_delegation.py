# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes das rotas de edição delegada de permissões de roles.

O objetivo aqui é validar o contrato HTTP do editor:
- super_admin mantém edição completa;
- supervisor delegado edita somente permissões do domínio que administra;
- permissões fora do domínio do supervisor são preservadas no POST;
- roles super_admin e o próprio role do ator ficam bloqueados.
"""

from __future__ import annotations

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import ReservaSource
from app.models.role import Role, RolePermission


def _role(
    nome: str,
    *,
    is_super_admin: bool = False,
    permissoes: list[tuple[str, str]] | None = None,
) -> Role:
    role = Role(nome=nome, is_super_admin=is_super_admin, ativo=True)
    _db.session.add(role)
    _db.session.flush()
    for modulo, acao in permissoes or []:
        _db.session.add(RolePermission(role_id=role.id, modulo=modulo, acao=acao))
    _db.session.flush()
    return role


def _funcionario(nome: str, cpf: str, role: Role) -> Funcionario:
    funcionario = Funcionario(nome=nome, cpf=cpf, role_id=role.id, ativo=True)
    _db.session.add(funcionario)
    _db.session.commit()
    return funcionario


def _login(client, funcionario_id: int) -> None:
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def _permissoes(role: Role) -> set[tuple[str, str]]:
    _db.session.refresh(role)
    return {(p.modulo, p.acao) for p in role.permissoes if p.source_id is None}


def _source(nome: str) -> ReservaSource:
    source = ReservaSource(name=nome, display_name=nome, is_active=True)
    _db.session.add(source)
    _db.session.commit()
    return source


def _permissoes_escopadas(role: Role) -> set[tuple[str, str, int]]:
    _db.session.refresh(role)
    return {
        (p.modulo, p.acao, int(p.source_id))
        for p in role.permissoes
        if p.source_id is not None
    }


def test_supervisor_de_reservas_ve_apenas_permissoes_editaveis(app, client, db):
    with app.app_context():
        supervisor_role = _role(
            "supervisor_reservas",
            permissoes=[
                ("roles", "manage_reservas"),
                ("reservas", "view"),
                ("reservas", "create"),
                ("dashboard", "view"),
            ],
        )
        target_role = _role(
            "atendimento",
            permissoes=[
                ("reservas", "view"),
                ("rh", "view"),
            ],
        )
        supervisor = _funcionario("Supervisor", "52998224725", supervisor_role)
        target_id = target_role.id
        supervisor_id = supervisor.id

    _login(client, supervisor_id)
    response = client.get(f"/admin/configuracoes/roles/{target_id}/permissoes")
    body = response.data.decode()

    assert response.status_code == 200
    assert "reservas.view" in body
    assert "reservas.create" in body
    assert "rh.view" not in body
    assert "Edição delegada por domínio" in body


def test_editor_exibe_matriz_de_reservas_por_venue(app, client, db):
    with app.app_context():
        super_role = _role("super_admin", is_super_admin=True)
        target_role = _role("atendimento")
        source = _source("Cachoeira")
        super_admin = _funcionario("Super Admin", "55120799723", super_role)
        target_id = target_role.id
        source_nome = source.name
        super_admin_id = super_admin.id

    _login(client, super_admin_id)
    response = client.get(f"/admin/configuracoes/roles/{target_id}/permissoes")
    body = response.data.decode()

    assert response.status_code == 200
    assert "Reservas por venue" in body
    assert source_nome in body
    assert "scoped_reservas_view_" in body


def test_supervisor_de_reservas_lista_roles_comuns_administraveis(app, client, db):
    with app.app_context():
        supervisor_role = _role(
            "supervisor_reservas",
            permissoes=[
                ("roles", "manage_reservas"),
                ("reservas", "view"),
            ],
        )
        target_role = _role("atendimento", permissoes=[("reservas", "view")])
        rh_role = _role("rh_operador", permissoes=[("rh", "view")])
        super_role = _role("super_admin", is_super_admin=True)
        supervisor = _funcionario("Supervisor", "52998224725", supervisor_role)
        supervisor_id = supervisor.id
        supervisor_role_nome = supervisor_role.nome
        target_nome = target_role.nome
        rh_nome = rh_role.nome
        super_nome = super_role.nome

    _login(client, supervisor_id)
    response = client.get("/admin/configuracoes/roles")
    body = response.data.decode()

    assert response.status_code == 200
    assert "Roles Administráveis" in body
    assert target_nome in body
    assert rh_nome in body
    assert supervisor_role_nome not in body
    assert super_nome not in body
    assert "Criar Novo Role" not in body
    assert "Desativar" not in body


def test_funcionario_sem_delegacao_nao_acessa_listagem_roles(app, client, db):
    with app.app_context():
        role = _role("funcionario", permissoes=[("dashboard", "view")])
        funcionario = _funcionario("Funcionario", "11144477735", role)
        funcionario_id = funcionario.id

    _login(client, funcionario_id)
    response = client.get("/admin/configuracoes/roles")

    assert response.status_code == 403


def test_supervisor_de_reservas_preserva_permissoes_fora_do_dominio(app, client, db):
    with app.app_context():
        supervisor_role = _role(
            "supervisor_reservas",
            permissoes=[
                ("roles", "manage_reservas"),
                ("reservas", "view"),
                ("reservas", "create"),
            ],
        )
        target_role = _role(
            "atendimento",
            permissoes=[
                ("reservas", "view"),
                ("rh", "view"),
            ],
        )
        supervisor = _funcionario("Supervisor", "11144477735", supervisor_role)
        target_id = target_role.id
        supervisor_id = supervisor.id

    _login(client, supervisor_id)
    response = client.post(
        f"/admin/configuracoes/roles/{target_id}/permissoes",
        data={
            "perm_reservas_create": "on",
            "perm_rh_edit": "on",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        target_role = _db.session.get(Role, target_id)
        assert _permissoes(target_role) == {
            ("reservas", "create"),
            ("rh", "view"),
        }


def test_supervisor_de_reservas_nao_edita_role_super_admin(app, client, db):
    with app.app_context():
        supervisor_role = _role(
            "supervisor_reservas",
            permissoes=[
                ("roles", "manage_reservas"),
                ("reservas", "view"),
            ],
        )
        target_role = _role("super_admin", is_super_admin=True)
        supervisor = _funcionario("Supervisor", "39053344705", supervisor_role)
        target_id = target_role.id
        supervisor_id = supervisor.id

    _login(client, supervisor_id)
    response = client.get(f"/admin/configuracoes/roles/{target_id}/permissoes")

    assert response.status_code == 403


def test_supervisor_de_reservas_nao_edita_proprio_role(app, client, db):
    with app.app_context():
        supervisor_role = _role(
            "supervisor_reservas",
            permissoes=[
                ("roles", "manage_reservas"),
                ("reservas", "view"),
            ],
        )
        supervisor = _funcionario("Supervisor", "39053344705", supervisor_role)
        supervisor_role_id = supervisor_role.id
        supervisor_id = supervisor.id

    _login(client, supervisor_id)
    response = client.post(
        f"/admin/configuracoes/roles/{supervisor_role_id}/permissoes",
        data={"perm_reservas_view": "on"},
    )

    assert response.status_code == 403


def test_super_admin_continua_editando_catalogo_completo(app, client, db):
    with app.app_context():
        super_role = _role("super_admin", is_super_admin=True)
        target_role = _role("atendimento")
        super_admin = _funcionario("Super Admin", "52998224725", super_role)
        target_id = target_role.id
        super_admin_id = super_admin.id

    _login(client, super_admin_id)
    response = client.post(
        f"/admin/configuracoes/roles/{target_id}/permissoes",
        data={
            "perm_reservas_view": "on",
            "perm_rh_edit": "on",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        target_role = _db.session.get(Role, target_id)
        assert ("reservas", "view") in _permissoes(target_role)
        assert ("rh", "edit") in _permissoes(target_role)


def test_super_admin_salva_permissao_reservas_escopada_por_venue(app, client, db):
    with app.app_context():
        super_role = _role("super_admin", is_super_admin=True)
        target_role = _role("atendimento")
        source = _source("Cachoeira")
        super_admin = _funcionario("Super Admin", "64792940557", super_role)
        target_id = target_role.id
        source_id = source.id
        super_admin_id = super_admin.id

    _login(client, super_admin_id)
    response = client.post(
        f"/admin/configuracoes/roles/{target_id}/permissoes",
        data={
            f"scoped_reservas_view_{source_id}": "on",
            f"scoped_reservas_view_contact_{source_id}": "on",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        target_role = _db.session.get(Role, target_id)
        assert ("reservas", "view") not in _permissoes(target_role)
        assert _permissoes_escopadas(target_role) == {
            ("reservas", "view", source_id),
            ("reservas", "view_contact", source_id),
        }


def test_supervisor_escopado_concede_apenas_mesma_venue(app, client, db):
    with app.app_context():
        cachoeira = _source("Cachoeira")
        vinhedo = _source("Vinhedo")
        supervisor_role = _role(
            "supervisor_cachoeira",
            permissoes=[("roles", "manage_reservas")],
        )
        _db.session.add(
            RolePermission(
                role_id=supervisor_role.id,
                modulo="reservas",
                acao="view",
                source_id=cachoeira.id,
            )
        )
        target_role = _role("atendimento")
        _db.session.commit()
        supervisor = _funcionario("Supervisor", "37948473690", supervisor_role)
        supervisor_id = supervisor.id
        target_id = target_role.id
        cachoeira_id = cachoeira.id
        vinhedo_nome = vinhedo.name

    _login(client, supervisor_id)
    response_get = client.get(f"/admin/configuracoes/roles/{target_id}/permissoes")
    body = response_get.data.decode()

    assert response_get.status_code == 200
    assert "Reservas por venue" in body
    assert "scoped_reservas_view_" in body
    assert vinhedo_nome not in body

    response_post = client.post(
        f"/admin/configuracoes/roles/{target_id}/permissoes",
        data={
            f"scoped_reservas_view_{cachoeira_id}": "on",
        },
    )

    assert response_post.status_code == 302
    with app.app_context():
        target_role = _db.session.get(Role, target_id)
        assert _permissoes_escopadas(target_role) == {
            ("reservas", "view", cachoeira_id),
        }


def test_permissao_global_remove_escopo_redundante_de_reservas(app, client, db):
    with app.app_context():
        super_role = _role("super_admin", is_super_admin=True)
        target_role = _role("atendimento")
        source = _source("Cachoeira")
        _db.session.add(
            RolePermission(
                role_id=target_role.id,
                modulo="reservas",
                acao="view",
                source_id=source.id,
            )
        )
        _db.session.commit()
        super_admin = _funcionario("Super Admin", "72377126489", super_role)
        target_id = target_role.id
        source_id = source.id
        super_admin_id = super_admin.id

    _login(client, super_admin_id)
    response = client.post(
        f"/admin/configuracoes/roles/{target_id}/permissoes",
        data={
            "perm_reservas_view": "on",
            f"scoped_reservas_view_{source_id}": "on",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        target_role = _db.session.get(Role, target_id)
        assert ("reservas", "view") in _permissoes(target_role)
        assert ("reservas", "view", source_id) not in _permissoes_escopadas(target_role)
