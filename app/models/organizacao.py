# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Modelos organizacionais operacionais: Funcao, Equipe e EquipeAgendamento.

Distinto do RBAC (Role): Role define permissões de sistema; Funcao define
o papel operacional que um colaborador exerce num turno (Recepção, Bar,
Salva-vidas). Equipe agrupa colaboradores com funcoes atribuídas.
EquipeAgendamento (2.1b) adiciona a camada temporal: uma equipe passa a
ser agendada por período (dia/semana/mês/intervalo custom).

Tabelas:
    sistur_funcoes             — funções operacionais por área
    sistur_equipes             — equipes de trabalho
    sistur_equipe_membros      — associação N:N Equipe ↔ Funcionario + funcao_id
    sistur_equipe_agendamentos — vigência temporal de uma equipe (2.1b)
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db


class Funcao(db.Model):
    """Papel operacional que um colaborador exerce num turno ou equipe.

    Exemplo: "Recepção", "Bar", "Limpeza", "Salva-vidas".
    Associada opcionalmente a uma Area para filtros e relatórios.
    """

    __tablename__ = "sistur_funcoes"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    area_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_areas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = db.Column(db.Boolean, default=True, nullable=False)
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

    area = db.relationship("Area", foreign_keys=[area_id], lazy="select")
    membros = db.relationship("EquipeMembro", back_populates="funcao", lazy="dynamic")

    __table_args__ = (
        db.UniqueConstraint("nome", "area_id", name="uq_funcao_nome_area"),
    )


class Equipe(db.Model):
    """Grupo operacional de colaboradores, opcionalmente vinculado a uma área.

    Uma equipe pode ser permanente (turno fixo) ou temporária (evento/escala).
    A composição real é feita via EquipeMembro com funcao_id por membro.
    """

    __tablename__ = "sistur_equipes"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    area_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_areas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = db.Column(db.Boolean, default=True, nullable=False)
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

    area = db.relationship("Area", foreign_keys=[area_id], lazy="select")
    membros = db.relationship(
        "EquipeMembro",
        back_populates="equipe",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        db.UniqueConstraint("nome", "area_id", name="uq_equipe_nome_area"),
    )


class EquipeMembro(db.Model):
    """Associação N:N entre Equipe e Funcionario com função opcional.

    funcao_id é nullable: um membro pode estar numa equipe sem função
    específica atribuída (útil para equipes informais ou listas de escala).
    """

    __tablename__ = "sistur_equipe_membros"

    id = db.Column(db.Integer, primary_key=True)
    equipe_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_equipes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    funcao_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcoes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    equipe = db.relationship("Equipe", back_populates="membros")
    funcionario = db.relationship("Funcionario", foreign_keys=[funcionario_id], lazy="select")
    funcao = db.relationship("Funcao", back_populates="membros", lazy="select")

    __table_args__ = (
        db.UniqueConstraint("equipe_id", "funcionario_id", name="uq_equipe_membro"),
    )


class EquipeAgendamento(db.Model):
    """Vigência temporal de uma equipe: "esta equipe opera neste período".

    Camada aditiva sobre Equipe/EquipeMembro (2.1b) — a granularidade é o
    dia; turno/horário continua sendo assunto da Escala (EscalaEntrada).
    Soft-delete via deleted_at (cancelamento preserva histórico).
    """

    __tablename__ = "sistur_equipe_agendamentos"

    id = db.Column(db.Integer, primary_key=True)
    equipe_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_equipes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data_inicio = db.Column(db.Date, nullable=False, index=True)
    data_fim = db.Column(db.Date, nullable=False, index=True)
    observacao = db.Column(db.Text, nullable=True)
    criado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at = db.Column(db.DateTime, nullable=True)
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

    equipe = db.relationship("Equipe", foreign_keys=[equipe_id], lazy="select")
