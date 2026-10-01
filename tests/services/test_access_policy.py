# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes da fundação de autorização para IA/tools."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.core.access_policy import (
    ActorContext,
    ReservaAccessPolicy,
    RoleDelegationPolicy,
    reserva_source_permission_modulo,
)
from app.core.permission_catalog import PERMISSOES_DO_SISTEMA
from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.reservas import Reserva, ReservaOrigin, ReservaSource, ReservaStatus
from app.models.role import Role, RolePermission


def _role(nome: str, *, super_admin: bool = False, perms: list[tuple[str, str]] | None = None) -> Role:
    role = Role(nome=nome, is_super_admin=super_admin, ativo=True)
    db.session.add(role)
    db.session.flush()
    for modulo, acao in perms or []:
        db.session.add(RolePermission(role_id=role.id, modulo=modulo, acao=acao))
    db.session.flush()
    return role


def _funcionario(cpf: str, role: Role | None = None) -> Funcionario:
    f = Funcionario(nome=f"Pessoa {cpf[-2:]}", cpf=cpf, ativo=True, role_id=role.id if role else None)
    db.session.add(f)
    db.session.flush()
    return f


def _source(nome: str) -> ReservaSource:
    src = ReservaSource(name=nome, display_name=nome, is_active=True)
    db.session.add(src)
    db.session.flush()
    return src


def _reserva(source: ReservaSource, nome: str = "Cliente") -> Reserva:
    r = Reserva(
        group_id=f"g-{source.id}-{nome}",
        version=1,
        source_id=source.id,
        customer_name=nome,
        customer_document="12345678901",
        origin=ReservaOrigin.BALCAO,
        status=ReservaStatus.PENDING,
        check_in_date=datetime(2026, 6, 18, tzinfo=timezone.utc),
        metadata_info={"telefone": "11999999999", "email": "cliente@example.com"},
        total_amount=Decimal("100.00"),
        payment_data={"payment_type": "pix"},
    )
    db.session.add(r)
    db.session.flush()
    return r


def test_permission_catalog_inclui_permissoes_granulares_de_reservas():
    assert "view_contact" in PERMISSOES_DO_SISTEMA["reservas"]
    assert "view_financial" in PERMISSOES_DO_SISTEMA["reservas"]
    assert "view_payments" in PERMISSOES_DO_SISTEMA["reservas"]
    assert "export" in PERMISSOES_DO_SISTEMA["reservas"]
    assert "manage_reservas" in PERMISSOES_DO_SISTEMA["roles"]


def test_actor_context_carrega_permissoes_do_role(db):
    role = _role("reservas_basico", perms=[("reservas", "view")])
    funcionario = _funcionario("52998224725", role)
    db.session.commit()

    actor = ActorContext.from_funcionario_id(funcionario.id)

    assert actor.funcionario_id == funcionario.id
    assert actor.role_id == role.id
    assert actor.has("reservas", "view") is True
    assert actor.has("reservas", "view_financial") is False


def test_reserva_policy_filtra_por_fonte_dinamica(db):
    cachoeira = _source("Cachoeira")
    vinhedo = _source("Vinhedo Girassol")
    r1 = _reserva(cachoeira, "Cliente Cachoeira")
    _reserva(vinhedo, "Cliente Vinhedo")

    role = _role(
        "operador_cachoeira",
        perms=[(reserva_source_permission_modulo("Cachoeira"), "view")],
    )
    funcionario = _funcionario("39053344705", role)
    db.session.commit()

    actor = ActorContext.from_funcionario_id(funcionario.id)
    query = ReservaAccessPolicy.filter_query(db.session.query(Reserva), actor)

    assert [r.id for r in query.order_by(Reserva.id).all()] == [r1.id]


def test_reserva_policy_filtra_por_permissao_escopada_de_source(db):
    cachoeira = _source("Cachoeira")
    vinhedo = _source("Vinhedo Girassol")
    r1 = _reserva(cachoeira, "Cliente Cachoeira")
    _reserva(vinhedo, "Cliente Vinhedo")

    role = _role("operador_cachoeira")
    db.session.add(
        RolePermission(
            role_id=role.id,
            modulo="reservas",
            acao="view",
            source_id=cachoeira.id,
        )
    )
    funcionario = _funcionario("64110585900", role)
    db.session.commit()

    actor = ActorContext.from_funcionario_id(funcionario.id)
    query = ReservaAccessPolicy.filter_query(db.session.query(Reserva), actor)

    assert actor.has("reservas", "view") is False
    assert actor.has("reservas", "view", cachoeira.id) is True
    assert actor.has("reservas", "view", vinhedo.id) is False
    assert [r.id for r in query.order_by(Reserva.id).all()] == [r1.id]


def test_reserva_policy_redacta_campos_sem_permissao_granular(db):
    role = _role("reservas_view", perms=[("reservas", "view")])
    funcionario = _funcionario("28625587887", role)
    db.session.commit()
    actor = ActorContext.from_funcionario_id(funcionario.id)

    data = {
        "id": 1,
        "customer_name": "Cliente",
        "customer_document": "12345678901",
        "metadata_info": {"telefone": "11999999999", "email": "cliente@example.com"},
        "total_amount": "100.00",
        "payment_data": {"payment_type": "pix"},
    }

    out = ReservaAccessPolicy.redact_dict(actor, data)

    assert "customer_document" not in out
    assert out["metadata_info"] == {}
    assert "total_amount" not in out
    assert "payment_data" not in out


def test_reserva_policy_redacta_campos_conforme_source_id(db):
    cachoeira = _source("Cachoeira")
    vinhedo = _source("Vinhedo")
    role = _role("reservas_contato_cachoeira", perms=[("reservas", "view")])
    db.session.add(
        RolePermission(
            role_id=role.id,
            modulo="reservas",
            acao="view_contact",
            source_id=cachoeira.id,
        )
    )
    funcionario = _funcionario("07699220030", role)
    db.session.commit()
    actor = ActorContext.from_funcionario_id(funcionario.id)

    base = {
        "customer_document": "12345678901",
        "metadata_info": {"telefone": "11999999999"},
    }

    out_cachoeira = ReservaAccessPolicy.redact_dict(
        actor,
        {"source_id": cachoeira.id, **base},
    )
    out_vinhedo = ReservaAccessPolicy.redact_dict(
        actor,
        {"source_id": vinhedo.id, **base},
    )

    assert out_cachoeira["customer_document"] == "12345678901"
    assert "customer_document" not in out_vinhedo
    assert out_vinhedo["metadata_info"] == {}


def test_reserva_policy_preserva_campos_com_permissoes_granulares(db):
    role = _role(
        "reservas_completo",
        perms=[
            ("reservas", "view"),
            ("reservas", "view_contact"),
            ("reservas", "view_financial"),
            ("reservas", "view_payments"),
        ],
    )
    funcionario = _funcionario("11144477735", role)
    db.session.commit()
    actor = ActorContext.from_funcionario_id(funcionario.id)

    data = {
        "customer_document": "12345678901",
        "metadata_info": {"telefone": "11999999999"},
        "total_amount": "100.00",
        "payment_data": {"payment_type": "pix"},
    }

    out = ReservaAccessPolicy.redact_dict(actor, data)

    assert out == data


def test_role_delegation_permite_supervisor_gerenciar_apenas_dominio_e_permissoes_que_possui(db):
    supervisor_role = _role(
        "supervisor_reservas",
        perms=[
            ("roles", "manage_reservas"),
            ("reservas", "view"),
            ("reservas", "create"),
        ],
    )
    target_role = _role("operador_reservas")
    supervisor = _funcionario("52998224725", supervisor_role)
    db.session.commit()

    actor = ActorContext.from_funcionario_id(supervisor.id)

    assert RoleDelegationPolicy.can_grant_permission(
        actor, target_role, "reservas", "view"
    ) is True
    assert RoleDelegationPolicy.can_grant_permission(
        actor, target_role, "reservas", "delete"
    ) is False
    assert RoleDelegationPolicy.can_grant_permission(
        actor, target_role, "folha_ponto", "view"
    ) is False


def test_role_delegation_bloqueia_edicao_de_super_admin_e_autoedicao(db):
    supervisor_role = _role(
        "supervisor_rh",
        perms=[
            ("roles", "manage_rh"),
            ("funcionarios", "view"),
        ],
    )
    target_super = _role("super_admin", super_admin=True)
    supervisor = _funcionario("39053344705", supervisor_role)
    db.session.commit()

    actor = ActorContext.from_funcionario_id(supervisor.id)

    assert RoleDelegationPolicy.can_grant_permission(
        actor, target_super, "funcionarios", "view"
    ) is False
    assert RoleDelegationPolicy.can_grant_permission(
        actor, supervisor_role, "funcionarios", "view"
    ) is False


def test_role_delegation_super_admin_bypassa_restricoes(db):
    super_role = _role("super_admin", super_admin=True)
    target_role = _role("operador_rh")
    super_user = _funcionario("28625587887", super_role)
    db.session.commit()

    actor = ActorContext.from_funcionario_id(super_user.id)

    assert RoleDelegationPolicy.can_grant_permission(
        actor, target_role, "folha_ponto", "deducao"
    ) is True
