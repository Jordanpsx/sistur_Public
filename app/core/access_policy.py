# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Policies de acesso por ator para uso em services, IA/tools e relatórios.

As policies não importam Flask e não leem sessão. O chamador deve informar o
ator explicitamente para que a IA opere com permissões iguais ou menores que as
do operador logado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import false

from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.reservas import Reserva, ReservaSource
from app.models.role import Role, RolePermission


DOMAIN_PERMISSION_MODULES: dict[str, frozenset[str]] = {
    "reservas": frozenset({"reservas"}),
    "rh": frozenset({"rh", "funcionarios", "folha_ponto", "ajuste_ponto", "avisos"}),
}


@dataclass(frozen=True)
class ActorContext:
    """Contexto de autorização do operador atual.

    Attributes:
        funcionario_id: ID do funcionário logado.
        role_id: ID do role atribuído ao funcionário.
        is_super_admin: Se True, bypassa permissões explícitas.
        permissions: Conjunto de pares ``(modulo, acao)`` do role.
        scoped_permissions: Conjunto de triplas ``(modulo, acao, source_id)``.
        area_id: Área organizacional do funcionário, quando existir.
    """

    funcionario_id: int
    role_id: int | None
    is_super_admin: bool
    permissions: frozenset[tuple[str, str]]
    scoped_permissions: frozenset[tuple[str, str, int]] = frozenset()
    area_id: int | None = None

    @classmethod
    def from_funcionario_id(cls, funcionario_id: int) -> "ActorContext":
        """Monta o contexto de autorização a partir de um funcionário.

        Args:
            funcionario_id: PK do funcionário autenticado.

        Returns:
            Contexto de autorização pronto para policies.

        Raises:
            ValueError: Se o funcionário não existir ou estiver inativo.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario or not funcionario.ativo:
            raise ValueError("Funcionário inválido ou inativo.")

        role = db.session.get(Role, funcionario.role_id) if funcionario.role_id else None
        if not role or not role.ativo:
            return cls(
                funcionario_id=funcionario.id,
                role_id=None,
                is_super_admin=False,
                permissions=frozenset(),
                scoped_permissions=frozenset(),
                area_id=funcionario.area_id,
            )

        rows = (
            db.session.query(RolePermission.modulo, RolePermission.acao, RolePermission.source_id)
            .filter(RolePermission.role_id == role.id)
            .all()
        )
        global_permissions = frozenset(
            (modulo, acao)
            for modulo, acao, source_id in rows
            if source_id is None
        )
        scoped_permissions = frozenset(
            (modulo, acao, int(source_id))
            for modulo, acao, source_id in rows
            if source_id is not None
        )
        return cls(
            funcionario_id=funcionario.id,
            role_id=role.id,
            is_super_admin=bool(role.is_super_admin),
            permissions=global_permissions,
            scoped_permissions=scoped_permissions,
            area_id=funcionario.area_id,
        )

    def has(self, modulo: str, acao: str, source_id: int | None = None) -> bool:
        """Verifica se o ator possui uma permissão.

        Args:
            modulo: Módulo da permissão.
            acao: Ação da permissão.
            source_id: Venue/fonte para avaliar permissão escopada.

        Returns:
            True se super admin, se há permissão global ou, quando `source_id`
            é informado, se há permissão escopada para aquela venue.
        """
        if self.is_super_admin or (modulo, acao) in self.permissions:
            return True
        return (
            source_id is not None
            and (modulo, acao, int(source_id)) in self.scoped_permissions
        )


def reserva_source_permission_modulo(source_name: str) -> str:
    """Gera o módulo dinâmico legado de permissão por fonte de reserva.

    Args:
        source_name: Nome da fonte/venue.

    Returns:
        Slug no formato ``reservas_<nome>`` usado pelo listener existente.
    """
    return f"reservas_{source_name.lower().replace(' ', '_').replace('-', '_')}"


class ReservaAccessPolicy:
    """Policy de leitura/escopo/redação para Reservas."""

    @staticmethod
    def can_view(actor: ActorContext, reserva: Reserva) -> bool:
        """Indica se o ator pode visualizar a reserva.

        Args:
            actor: Contexto do operador.
            reserva: Reserva a validar.

        Returns:
            True quando o ator tem `reservas.view` global ou permissão dinâmica
            da fonte da reserva.
        """
        if actor.has("reservas", "view", reserva.source_id):
            return True
        source = db.session.get(ReservaSource, reserva.source_id) if reserva.source_id else None
        if not source:
            return False
        return actor.has(reserva_source_permission_modulo(source.name), "view")

    @staticmethod
    def visible_source_ids(actor: ActorContext) -> list[int] | None:
        """Retorna fontes visíveis pelo ator.

        Args:
            actor: Contexto do operador.

        Returns:
            None quando o ator pode ver todas as fontes; lista de IDs quando
            possui apenas permissões específicas; lista vazia quando não vê nada.
        """
        if actor.has("reservas", "view"):
            return None

        sources = (
            db.session.query(ReservaSource)
            .filter(
                ReservaSource.deleted_at.is_(None),
                ReservaSource.is_active.is_(True),
            )
            .all()
        )
        return [
            source.id
            for source in sources
            if actor.has("reservas", "view", source.id)
            or actor.has(reserva_source_permission_modulo(source.name), "view")
        ]

    @staticmethod
    def visible_source_ids_for_action(actor: ActorContext, acao: str) -> list[int] | None:
        """Retorna fontes onde uma ação granular de Reservas é permitida.

        Args:
            actor: Contexto do operador.
            acao: Ação dentro do módulo `reservas`.

        Returns:
            None quando a ação é global; lista de IDs quando escopada; lista
            vazia quando a ação não existe para nenhuma venue.
        """
        if actor.has("reservas", acao):
            return None
        return sorted(
            source_id
            for modulo, scoped_acao, source_id in actor.scoped_permissions
            if modulo == "reservas" and scoped_acao == acao
        )

    @classmethod
    def filter_query(cls, query, actor: ActorContext):
        """Aplica escopo de fontes a uma query de Reserva.

        Args:
            query: Query SQLAlchemy sobre `Reserva`.
            actor: Contexto do operador.

        Returns:
            Query filtrada pelo escopo do ator.
        """
        source_ids = cls.visible_source_ids(actor)
        if source_ids is None:
            return query
        if not source_ids:
            return query.filter(false())
        return query.filter(Reserva.source_id.in_(source_ids))

    @staticmethod
    def redact_dict(actor: ActorContext, data: dict[str, Any]) -> dict[str, Any]:
        """Remove campos sensíveis de uma reserva serializada.

        Args:
            actor: Contexto do operador.
            data: Dicionário serializado da reserva.

        Returns:
            Cópia do dicionário com campos removidos conforme permissão.
        """
        out = dict(data)

        source_id = out.get("source_id")

        if not actor.has("reservas", "view_contact", source_id):
            for key in ("customer_document", "customer_email", "customer_phone"):
                out.pop(key, None)
            metadata = out.get("metadata_info")
            if isinstance(metadata, dict):
                metadata = dict(metadata)
                for key in ("cpf", "documento", "email", "telefone", "phone"):
                    metadata.pop(key, None)
                out["metadata_info"] = metadata

        if not actor.has("reservas", "view_financial", source_id):
            for key in (
                "total_amount",
                "subtotal_amount",
                "discount_amount",
                "service_fee_amount",
                "security_deposit_amount",
            ):
                out.pop(key, None)

        if not actor.has("reservas", "view_payments", source_id):
            for key in ("payment_id", "payment_data", "payment_transactions", "pix_data", "deposits"):
                out.pop(key, None)

        return out


class RoleDelegationPolicy:
    """Policy para administração delegada de permissões por domínio.

    Um supervisor de domínio pode gerenciar permissões do seu domínio, desde
    que não edite super admins, não edite o próprio role e não conceda uma
    permissão que ele próprio não possua. O super admin mantém bypass total.
    """

    @staticmethod
    def domain_for_permission(modulo: str, acao: str) -> str | None:
        """Resolve a qual domínio pertence uma permissão.

        Args:
            modulo: Módulo da permissão.
            acao: Ação da permissão.

        Returns:
            Slug do domínio ou None se a permissão não for delegável.
        """
        del acao
        for domain, modules in DOMAIN_PERMISSION_MODULES.items():
            if modulo in modules:
                return domain
        return None

    @classmethod
    def can_manage_domain(cls, actor: ActorContext, domain: str) -> bool:
        """Indica se o ator pode administrar permissões de um domínio.

        Args:
            actor: Contexto do operador.
            domain: Domínio delegável (ex.: ``reservas``).

        Returns:
            True se super admin ou se possuir ``roles.manage_<domain>``.
        """
        return actor.is_super_admin or actor.has("roles", f"manage_{domain}")

    @classmethod
    def can_manage_role(cls, actor: ActorContext, target_role: Role) -> bool:
        """Indica se o ator pode editar um role alvo.

        Args:
            actor: Contexto do operador.
            target_role: Role que seria editado.

        Returns:
            True quando a edição é permitida.
        """
        if actor.is_super_admin:
            return True
        if target_role.is_super_admin:
            return False
        if actor.role_id == target_role.id:
            return False
        return True

    @classmethod
    def can_grant_permission(
        cls,
        actor: ActorContext,
        target_role: Role,
        modulo: str,
        acao: str,
    ) -> bool:
        """Verifica se o ator pode conceder/remover uma permissão.

        Args:
            actor: Contexto do operador.
            target_role: Role que receberia/perderia a permissão.
            modulo: Módulo da permissão.
            acao: Ação da permissão.

        Returns:
            True quando a alteração é permitida.
        """
        if actor.is_super_admin:
            return True
        if not cls.can_manage_role(actor, target_role):
            return False

        domain = cls.domain_for_permission(modulo, acao)
        if not domain or not cls.can_manage_domain(actor, domain):
            return False

        if modulo == "roles":
            return False

        return actor.has(modulo, acao)
