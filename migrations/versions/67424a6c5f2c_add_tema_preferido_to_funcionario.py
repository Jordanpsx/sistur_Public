"""add tema_preferido to funcionario

Revision ID: 67424a6c5f2c
Revises: 9e1cd1afe63d
Create Date: 2026-06-26 11:33:57.568531

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '67424a6c5f2c'
down_revision = '9e1cd1afe63d'
branch_labels = None
depends_on = None


def upgrade():
    # Preferência de tema (claro/escuro) por conta de colaborador.
    # NOTA: a autogeneração também detectou um churn de índices em
    # sistur_ia_usage_log (drift pré-existente, não relacionado a esta mudança);
    # foi removido de propósito para manter a migração atômica.
    with op.batch_alter_table('sistur_funcionarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tema_preferido', sa.Enum('light', 'dark', native_enum=False), nullable=True))


def downgrade():
    with op.batch_alter_table('sistur_funcionarios', schema=None) as batch_op:
        batch_op.drop_column('tema_preferido')
