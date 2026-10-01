"""add color_index to sistur_areas

Revision ID: 969bbd4eb078
Revises: 67424a6c5f2c
Create Date: 2026-07-02 13:53:22.715528

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '969bbd4eb078'
down_revision = '67424a6c5f2c'
branch_labels = None
depends_on = None


def upgrade():
    # Índice da paleta categórica (0-7) escolhido pelo operador.
    # NULL = cor derivada do id (id % 8), comportamento anterior preservado.
    with op.batch_alter_table('sistur_areas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('color_index', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('sistur_areas', schema=None) as batch_op:
        batch_op.drop_column('color_index')
