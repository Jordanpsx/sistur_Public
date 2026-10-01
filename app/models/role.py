# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Role e RolePermission — sistema de controle de acesso baseado em papéis (RBAC).

Cada Funcionario possui um Role.  O Role define quais módulos e ações
estão disponíveis no Portal do Colaborador.

Papéis iniciais (seeded via `flask seed-roles`):
    super_admin  — is_super_admin=True, bypass em todas as verificações
    funcionario  — acesso básico ao dashboard

Novos roles e permissões devem ser adicionados à medida que novos
módulos são implementados no portal.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db


class Role(db.Model):
    """
    Papel atribuído a um Funcionario para controlar acesso ao portal.

    Campos:
        nome:          Slug único do papel (e.g. "super_admin", "funcionario").
        descricao:     Descrição legível do papel.
        is_super_admin: Se True, o portador recebe acesso total automático
                        sem necessidade de permissões explícitas.
        ativo:         False desabilita o papel sem excluí-lo do banco.
    """

    __tablename__ = "sistur_roles"

    id = db.Column(db.Integer, primary_key=True)

    nome = db.Column(db.String(50), unique=True, nullable=False)
    descricao = db.Column(db.String(255), nullable=True)

    # Super admin bypass — portadores com este flag têm acesso a tudo
    is_super_admin = db.Column(db.Boolean, default=False, nullable=False)

    ativo = db.Column(db.Boolean, default=True, nullable=False)

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    permissoes = db.relationship(
        "RolePermission",
        back_populates="role",
        cascade="all, delete-orphan",
        lazy="select",
    )
    funcionarios = db.relationship(
        "Funcionario",
        back_populates="role",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Role id={self.id} nome={self.nome!r} super={self.is_super_admin}>"


class RolePermission(db.Model):
    """
    Permissão granular concedida a um Role para um módulo e ação específicos.
    Quando `source_id` é NULL, a permissão vale globalmente no módulo. Quando
    preenchido, vale apenas para aquela venue/fonte de reserva.

    O par (modulo, acao) segue o padrão "modulo.acao" do sistema legado:
        modulo="dashboard",     acao="view"
        modulo="funcionarios",  acao="create"
        modulo="funcionarios",  acao="edit"

    Novas permissões são inseridas aqui à medida que novos módulos são
    implementados — não há necessidade de alterar o código da service layer.
    """

    __tablename__ = "sistur_role_permissions"

    id = db.Column(db.Integer, primary_key=True)

    role_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    modulo = db.Column(db.String(50), nullable=False)
    acao = db.Column(db.String(50), nullable=False)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.Index(
            "uq_role_modulo_acao_global",
            "role_id",
            "modulo",
            "acao",
            unique=True,
            postgresql_where=source_id.is_(None),
            sqlite_where=source_id.is_(None),
        ),
        db.Index(
            "uq_role_modulo_acao_source",
            "role_id",
            "modulo",
            "acao",
            "source_id",
            unique=True,
            postgresql_where=source_id.isnot(None),
            sqlite_where=source_id.isnot(None),
        ),
    )

    # Relationships
    role = db.relationship("Role", back_populates="permissoes")
    source = db.relationship("ReservaSource", lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<RolePermission role_id={self.role_id}"
            f" modulo={self.modulo!r} acao={self.acao!r}"
            f" source_id={self.source_id!r}>"
        )
