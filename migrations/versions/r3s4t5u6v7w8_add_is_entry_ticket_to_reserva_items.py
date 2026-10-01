"""Add is_entry_ticket to sistur_reserva_items

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-05-11

is_entry_ticket — flag que marca se cada unidade deste item representa uma pessoa
                  entrando no local. Permite calcular "pessoas esperadas" e "novas
                  entradas" na visão semanal do dashboard de forma genérica (SaaS),
                  sem lógica hardcoded sobre o que constitui uma entrada.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'r3s4t5u6v7w8'
down_revision = 'q2r3s4t5u6v7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_reserva_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'is_entry_ticket',
                sa.Boolean(),
                nullable=False,
                server_default='0',
            )
        )


def downgrade():
    with op.batch_alter_table('sistur_reserva_items', schema=None) as batch_op:
        batch_op.drop_column('is_entry_ticket')
