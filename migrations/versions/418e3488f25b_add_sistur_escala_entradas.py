"""add sistur_escala_entradas

Revision ID: 418e3488f25b
Revises: 7c610bec3808
Create Date: 2026-06-25 10:51:46.663948

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '418e3488f25b'
down_revision = '7c610bec3808'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sistur_escala_entradas',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('funcionario_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('turno', sa.Enum('MANHA', 'TARDE', 'NOITE', 'PLANTAO', 'FOLGA', native_enum=False), nullable=False),
    sa.Column('equipe_id', sa.Integer(), nullable=True),
    sa.Column('observacao', sa.String(length=255), nullable=True),
    sa.Column('criado_por_id', sa.Integer(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['criado_por_id'], ['sistur_funcionarios.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['equipe_id'], ['sistur_equipes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['funcionario_id'], ['sistur_funcionarios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('funcionario_id', 'data', name='uq_escala_func_data')
    )
    with op.batch_alter_table('sistur_escala_entradas', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sistur_escala_entradas_data'), ['data'], unique=False)
        batch_op.create_index(batch_op.f('ix_sistur_escala_entradas_equipe_id'), ['equipe_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sistur_escala_entradas_funcionario_id'), ['funcionario_id'], unique=False)


def downgrade():
    with op.batch_alter_table('sistur_escala_entradas', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sistur_escala_entradas_funcionario_id'))
        batch_op.drop_index(batch_op.f('ix_sistur_escala_entradas_equipe_id'))
        batch_op.drop_index(batch_op.f('ix_sistur_escala_entradas_data'))

    op.drop_table('sistur_escala_entradas')
