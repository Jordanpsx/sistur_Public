"""Add avisos, ausencias_justificadas, horario_entrada_padrao, auto_debit_aplicado

Revision ID: a1b2c3d4e5f6
Revises: 52a3e2238608
Create Date: 2026-03-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '52a3e2238608'
branch_labels = None
depends_on = None


def upgrade():
    # Tabela sistur_avisos — notificações internas do sistema
    op.create_table(
        'sistur_avisos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('destinatario_id', sa.Integer(), nullable=False),
        sa.Column('remetente_id', sa.Integer(), nullable=True),
        sa.Column('titulo', sa.String(length=200), nullable=False),
        sa.Column('mensagem', sa.Text(), nullable=True),
        sa.Column(
            'tipo',
            sa.Enum('ATRASO', 'FALTA', 'SISTEMA', name='tipoaviso'),
            nullable=False,
        ),
        sa.Column('is_lido', sa.Boolean(), nullable=False),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['destinatario_id'],
            ['sistur_funcionarios.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['remetente_id'],
            ['sistur_funcionarios.id'],
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sistur_avisos_destinatario_id', 'sistur_avisos', ['destinatario_id'])
    op.create_index('ix_sistur_avisos_is_lido', 'sistur_avisos', ['is_lido'])
    op.create_index('ix_sistur_avisos_criado_em', 'sistur_avisos', ['criado_em'])

    # Tabela sistur_ausencias_justificadas — folgas, atestados, férias
    op.create_table(
        'sistur_ausencias_justificadas',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('funcionario_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column(
            'tipo',
            sa.Enum('FOLGA', 'ATESTADO', 'FERIAS', name='tipoausencia'),
            nullable=False,
        ),
        sa.Column('aprovado', sa.Boolean(), nullable=False),
        sa.Column('criado_por_id', sa.Integer(), nullable=True),
        sa.Column('observacao', sa.Text(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['funcionario_id'],
            ['sistur_funcionarios.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['criado_por_id'],
            ['sistur_funcionarios.id'],
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('funcionario_id', 'data', name='uq_ausencia_funcionario_data'),
    )
    op.create_index('ix_sistur_ausencias_justificadas_funcionario_id', 'sistur_ausencias_justificadas', ['funcionario_id'])
    op.create_index('ix_sistur_ausencias_justificadas_data', 'sistur_ausencias_justificadas', ['data'])

    # Coluna horario_entrada_padrao em sistur_funcionarios
    op.add_column(
        'sistur_funcionarios',
        sa.Column('horario_entrada_padrao', sa.Time(), nullable=True),
    )

    # Coluna auto_debit_aplicado em sistur_time_days
    op.add_column(
        'sistur_time_days',
        sa.Column('auto_debit_aplicado', sa.Boolean(), nullable=False, server_default='0'),
    )


def downgrade():
    op.drop_column('sistur_time_days', 'auto_debit_aplicado')
    op.drop_column('sistur_funcionarios', 'horario_entrada_padrao')

    op.drop_index('ix_sistur_ausencias_justificadas_data', table_name='sistur_ausencias_justificadas')
    op.drop_index('ix_sistur_ausencias_justificadas_funcionario_id', table_name='sistur_ausencias_justificadas')
    op.drop_table('sistur_ausencias_justificadas')

    op.drop_index('ix_sistur_avisos_criado_em', table_name='sistur_avisos')
    op.drop_index('ix_sistur_avisos_is_lido', table_name='sistur_avisos')
    op.drop_index('ix_sistur_avisos_destinatario_id', table_name='sistur_avisos')
    op.drop_table('sistur_avisos')
