# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Adiciona auto_cancel_hours a ReservaStatusConfig.

Permite configurar, por status, quantas horas após expires_at uma reserva
naquele status é cancelada automaticamente sem confirmação de pagamento.
NULL desativa o cancelamento automático — é o valor de todas as linhas
existentes após esta migração, então nenhum comportamento muda até um
administrador definir um prazo em /reservas/admin/config/status.

Revision ID: c48e2f716a03
Revises: b7c31d0a94e2
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c48e2f716a03"
down_revision = "b7c31d0a94e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sistur_reserva_status_config",
        sa.Column("auto_cancel_hours", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sistur_reserva_status_config", "auto_cancel_hours")
