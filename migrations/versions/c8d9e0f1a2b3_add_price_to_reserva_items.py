"""Add price column to sistur_reserva_items

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-03-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8d9e0f1a2b3'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'sistur_reserva_items',
        sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False, server_default='0.00'),
    )


def downgrade():
    op.drop_column('sistur_reserva_items', 'price')
