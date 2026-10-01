"""Add same_day_cutoff_time to ReservaCategory

Revision ID: 8050557b5039
Revises: f9b2fb31698b
Create Date: 2026-08-07 10:38:11.848250

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8050557b5039'
down_revision = 'f9b2fb31698b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_reserva_categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('same_day_cutoff_time', sa.Time(), nullable=True))


def downgrade():
    with op.batch_alter_table('sistur_reserva_categories', schema=None) as batch_op:
        batch_op.drop_column('same_day_cutoff_time')
