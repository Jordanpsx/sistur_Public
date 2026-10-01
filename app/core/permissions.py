# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Helpers de permissão para o Portal do Colaborador.

Duas camadas independentes:

1. Supervisão por área (User → UserAreaPermission → Area)
   Controla o acesso de usuários admin a logs e dados de uma área específica.

2. Roles de funcionário (Funcionario → Role → RolePermission)
   Controla quais módulos e ações o Funcionario vê e executa no portal.
   Super admin (is_super_admin=True) bypassa todas as verificações de permissão.
"""

from __future__ import annotations

import functools

from flask import abort, session

from app.extensions import db
from app.core.models import Area, User, UserAreaPermission


# ---------------------------------------------------------------------------
# Plain helper — use this in service-layer code
# ---------------------------------------------------------------------------

def is_supervisor_of(user_id: int, area_slug: str) -> bool:
    """
    Return True if the user holds a supervisor permission for the given area.

    Args:
        user_id:   ID of the user to check.
        area_slug: Slug of the area (e.g. 'rh', 'restaurante').
    """
    return (
        db.session.query(UserAreaPermission)
        .join(Area, UserAreaPermission.area_id == Area.id)
        .filter(
            UserAreaPermission.user_id == user_id,
            UserAreaPermission.is_supervisor == True,
            Area.slug == area_slug,
            Area.is_active == True,
        )
        .first()
        is not None
    )


def get_supervised_areas(user_id: int) -> list[Area]:
    """Return all areas where the user has is_supervisor=True."""
    return (
        db.session.query(Area)
        .join(UserAreaPermission, UserAreaPermission.area_id == Area.id)
        .filter(
            UserAreaPermission.user_id == user_id,
            UserAreaPermission.is_supervisor == True,
            Area.is_active == True,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# Decorator — use this on Flask route functions
# ---------------------------------------------------------------------------

def require_supervisor(area_slug: str):
    """
    Route decorator that enforces area-specific supervisor access.

    Reads the current user from the Flask session (key: 'user_id').
    Returns 403 if the user is not logged in or is not a supervisor
    in the requested area.

    Usage::

        @bp.route("/rh/audit-logs")
        @require_supervisor("rh")
        def rh_audit_logs():
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user_id: int | None = session.get("user_id")
            if not user_id:
                abort(401)

            if not is_supervisor_of(user_id, area_slug):
                abort(403)

            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_any_supervisor(fn):
    """
    Route decorator that allows access if the user is a supervisor
    in *at least one* area (used for generic supervisor dashboards).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user_id: int | None = session.get("user_id")
        if not user_id:
            abort(401)

        has_any = (
            db.session.query(UserAreaPermission)
            .filter(
                UserAreaPermission.user_id == user_id,
                UserAreaPermission.is_supervisor == True,
            )
            .first()
            is not None
        )
        if not has_any:
            abort(403)

        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Role-based permissions — Funcionario → Role → RolePermission
# ---------------------------------------------------------------------------

def has_permission(funcionario_id: int, modulo: str, acao: str) -> bool:
    """
    Verifica se o funcionário tem permissão para executar a ação no módulo.

    Regras:
    - Se o funcionário não tiver role atribuído:
      * Se nenhum super_admin existir no sistema, retorna True (bootstrap mode).
      * Caso contrário, retorna False.
    - Se o role estiver inativo, retorna False.
    - Se o role tiver is_super_admin=True, retorna True sem consultar permissões.
    - Caso contrário, verifica a tabela sistur_role_permissions.

    Bootstrap mode:
    Quando nenhum super_admin existe, o primeiro funcionário a acessar o sistema
    recebe acesso total. Isso permite inicializar roles e permissões sem
    intervenção direta do banco.

    Args:
        funcionario_id: PK do funcionário a verificar.
        modulo:         Módulo do sistema (e.g. "dashboard", "funcionarios").
        acao:           Ação dentro do módulo (e.g. "view", "create").

    Returns:
        True se o funcionário tiver a permissão, False caso contrário.
    """
    # Importações locais para evitar circular imports (Role depende de extensions,
    # que é importado antes de permissions em alguns contextos)
    from app.models.funcionario import Funcionario
    from app.models.role import Role, RolePermission

    funcionario = db.session.get(Funcionario, funcionario_id)
    if not funcionario or not funcionario.ativo:
        return False

    # Bootstrap mode: se não houver nenhum super_admin e o usuário não tiver role,
    # ele vira super_admin implicitamente
    if not funcionario.role_id:
        super_admin_exists = (
            db.session.query(Role)
            .filter_by(is_super_admin=True, ativo=True)
            .first()
        ) is not None

        if not super_admin_exists:
            # Primeiro super_admin do sistema — acesso total
            return True
        return False

    role = db.session.get(Role, funcionario.role_id)
    if not role or not role.ativo:
        return False

    # Super admin bypass — acesso irrestrito
    if role.is_super_admin:
        return True

    query = db.session.query(RolePermission).filter_by(
        role_id=role.id,
        modulo=modulo,
        acao=acao,
    )

    if modulo == "reservas" and acao == "view":
        return query.first() is not None

    return query.filter(RolePermission.source_id.is_(None)).first() is not None


def require_permission(modulo: str, acao: str):
    """
    Decorator de rota que exige a permissão modulo.acao para o funcionário em sessão.

    Lê o funcionario_id da sessão Flask (chave: 'funcionario_id').
    Retorna 401 se não houver sessão ativa.
    Retorna 403 se o funcionário não tiver a permissão.

    Args:
        modulo: Módulo do sistema (e.g. "funcionarios").
        acao:   Ação exigida (e.g. "view").

    Usage::

        @bp.route("/funcionarios")
        @login_required
        @require_permission("funcionarios", "view")
        def listar_funcionarios():
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            funcionario_id: int | None = session.get("funcionario_id")
            if not funcionario_id:
                abort(401)
            if not has_permission(funcionario_id, modulo, acao):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
