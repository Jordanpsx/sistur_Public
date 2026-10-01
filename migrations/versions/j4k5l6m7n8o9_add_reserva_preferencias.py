"""add_reserva_preferencias

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-03-30 12:00:00.000000

Mudanças:
  - Nova tabela sistur_reserva_preferencias
    Armazena as predefinições de Local, Categoria e Status de Balcão
    por funcionário para pré-preencher o formulário Nova Reserva (PDV).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'j4k5l6m7n8o9'
down_revision = 'i3j4k5l6m7n8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sistur_reserva_preferencias',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'funcionario_id',
            sa.Integer(),
            sa.ForeignKey('sistur_funcionarios.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'default_source_id',
            sa.Integer(),
            sa.ForeignKey('sistur_reserva_sources.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'default_category_id',
            sa.Integer(),
            sa.ForeignKey('sistur_reserva_categories.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('default_balcao_status', sa.String(length=50), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('atualizado_em', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('funcionario_id', name='uq_reserva_pref_funcionario'),
    )
    op.create_index(
        'ix_sistur_reserva_preferencias_funcionario_id',
        'sistur_reserva_preferencias',
        ['funcionario_id'],
    )


def downgrade():
    op.drop_index(
        'ix_sistur_reserva_preferencias_funcionario_id',
        table_name='sistur_reserva_preferencias',
    )
    op.drop_table('sistur_reserva_preferencias')
