# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Modelo ORM para Solicitação de Ajuste de Ponto (Self-Service).

Tabela criada:
    sistur_ajuste_ponto_requests — solicitações de ajuste enviadas pelo colaborador

Arquitetura em camadas: modelos só contêm definição ORM e propriedades simples.
Toda lógica de negócio reside em AjustePontoService.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from app.extensions import db


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

class StatusAjuste(str, enum.Enum):
    """Status da solicitação de ajuste de ponto."""

    PENDENTE  = "PENDENTE"   # Aguardando análise do supervisor
    APROVADO  = "APROVADO"   # Aprovado — TimeEntry criada/atualizada
    REJEITADO = "REJEITADO"  # Rejeitado com motivo pelo supervisor


# ---------------------------------------------------------------------------
# AjustePontoRequest — solicitação self-service de ajuste de ponto
# ---------------------------------------------------------------------------

class AjustePontoRequest(db.Model):
    """
    Solicitação de ajuste de ponto enviada pelo colaborador ao supervisor.

    Fluxo:
        1. Colaborador cria solicitação via POST /ponto/solicitar-ajuste.
        2. Supervisor vê em /avisos/ (tab Aprovações) e aprova ou rejeita.
        3. Ao aprovar, AjustePontoService cria/edita TimeEntry e recalcula TimeDay.
        4. Colaborador recebe Aviso com o resultado.

    Regras de negócio:
        - time_entry_id = None significa pedido de batida NOVA (tipo e horário indicados pelo campo).
        - time_entry_id preenchido = pedido de CORREÇÃO de batida existente.
        - Apenas PENDENTE pode ser aprovado ou rejeitado (idempotência).
        - Permissão necessária para aprovar: ajuste_ponto.aprovar (RBAC).
    """

    __tablename__ = "sistur_ajuste_ponto_requests"

    id = db.Column(db.Integer, primary_key=True)

    # Funcionário que fez a solicitação
    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Batida existente a corrigir — None se a solicitação é de batida nova
    time_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_time_entries.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Data do turno que precisa de ajuste
    data_solicitada = db.Column(db.Date, nullable=False, index=True)

    # Horário solicitado pelo colaborador (UTC)
    horario_solicitado = db.Column(db.DateTime, nullable=False)

    # Tipo de batida que o colaborador quer registrar/corrigir
    tipo_ponto = db.Column(db.String(20), nullable=False)  # ex: "clock_in"

    # Justificativa obrigatória do colaborador
    motivo = db.Column(db.String(500), nullable=False)

    status = db.Column(
        db.Enum(StatusAjuste, native_enum=False),
        nullable=False,
        default=StatusAjuste.PENDENTE,
        index=True,
    )

    # Supervisor que resolveu a solicitação (None enquanto PENDENTE)
    supervisor_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Preenchido quando REJEITADO
    motivo_rejeicao = db.Column(db.String(300), nullable=True)

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    resolvido_em = db.Column(db.DateTime, nullable=True)

    # Relationships
    funcionario = db.relationship(
        "Funcionario",
        foreign_keys=[funcionario_id],
        lazy="select",
    )
    supervisor = db.relationship(
        "Funcionario",
        foreign_keys=[supervisor_id],
        lazy="select",
    )
    time_entry = db.relationship(
        "TimeEntry",
        foreign_keys=[time_entry_id],
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<AjustePontoRequest id={self.id} func={self.funcionario_id}"
            f" status={self.status.value} data={self.data_solicitada}>"
        )
