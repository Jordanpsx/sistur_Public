"""add_blocked_item_ids_to_resource_notices

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-03-26 14:00:00.000000

Mudanças:
  - Nova coluna sistur_resource_notices.blocked_item_ids (JSON nullable)
    NULL = bloqueia todos os itens; lista de IDs = bloqueia apenas os listados.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'i3j4k5l6m7n8'
down_revision = 'h2i3j4k5l6m7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'sistur_resource_notices',
        sa.Column('blocked_item_ids', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('sistur_resource_notices', 'blocked_item_ids')
