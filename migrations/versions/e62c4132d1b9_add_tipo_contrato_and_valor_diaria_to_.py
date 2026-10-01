"""add tipo_contrato and valor_diaria to funcionarios

Revision ID: e62c4132d1b9
Revises: c817d83bd6ee
Create Date: 2026-06-25 10:01:40.651841

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e62c4132d1b9'
down_revision = 'c817d83bd6ee'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_funcionarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tipo_contrato', sa.Enum('CLT', 'DIARISTA', native_enum=False), server_default='CLT', nullable=False))
        batch_op.add_column(sa.Column('valor_diaria', sa.Numeric(precision=10, scale=2), nullable=True))


def downgrade():
    with op.batch_alter_table('sistur_funcionarios', schema=None) as batch_op:
        batch_op.drop_column('valor_diaria')
        batch_op.drop_column('tipo_contrato')
