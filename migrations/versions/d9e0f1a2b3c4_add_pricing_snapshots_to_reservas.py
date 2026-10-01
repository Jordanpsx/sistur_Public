"""Add pricing snapshots to reservas and item_links

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-03-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9e0f1a2b3c4'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    # Add pricing snapshot columns to sistur_reservas
    op.add_column(
        'sistur_reservas',
        sa.Column('subtotal_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        'sistur_reservas',
        sa.Column('discount_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        'sistur_reservas',
        sa.Column('service_fee_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        'sistur_reservas',
        sa.Column('total_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    )

    # Add pricing snapshot columns to sistur_reserva_item_links
    op.add_column(
        'sistur_reserva_item_links',
        sa.Column('unit_price', sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        'sistur_reserva_item_links',
        sa.Column('item_total', sa.Numeric(precision=10, scale=2), nullable=True),
    )

    # Add indexes for pricing analysis
    op.create_index(
        'ix_sistur_reservas_total_amount',
        'sistur_reservas',
        ['total_amount']
    )


def downgrade():
    # Drop indexes
    op.drop_index('ix_sistur_reservas_total_amount', table_name='sistur_reservas')

    # Remove pricing columns from sistur_reserva_item_links
    op.drop_column('sistur_reserva_item_links', 'item_total')
    op.drop_column('sistur_reserva_item_links', 'unit_price')

    # Remove pricing columns from sistur_reservas
    op.drop_column('sistur_reservas', 'total_amount')
    op.drop_column('sistur_reservas', 'service_fee_amount')
    op.drop_column('sistur_reservas', 'discount_amount')
    op.drop_column('sistur_reservas', 'subtotal_amount')
