"""add pdv preferences

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sistur_reserva_preferencias", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pdv_default_status", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("pdv_favorite_item_ids", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("sistur_reserva_preferencias", schema=None) as batch_op:
        batch_op.drop_column("pdv_favorite_item_ids")
        batch_op.drop_column("pdv_default_status")
