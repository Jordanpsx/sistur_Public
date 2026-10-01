"""add_resource_notices

Revision ID: h2i3j4k5l6m7
Revises: 30dcc4b30a87
Create Date: 2026-03-26 12:00:00.000000

Mudanças:
  - Nova tabela sistur_resource_notices (avisos globais e locais com dois níveis de bloqueio)
  - Nova coluna sistur_reservas.agreed_notices_json (JSON nullable — prova legal de aceite)
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h2i3j4k5l6m7'
down_revision = '30dcc4b30a87'
branch_labels = None
depends_on = None


def upgrade():
    # --- Nova tabela: sistur_resource_notices ---
    op.create_table(
        'sistur_resource_notices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('event_name', sa.String(length=200), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('blocks_source', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('blocks_items', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['source_id'],
            ['sistur_reserva_sources.id'],
            name='fk_resource_notice_source',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sistur_resource_notices_source_id', 'sistur_resource_notices', ['source_id'])
    op.create_index('ix_sistur_resource_notices_start_date', 'sistur_resource_notices', ['start_date'])
    op.create_index('ix_sistur_resource_notices_end_date', 'sistur_resource_notices', ['end_date'])

    # --- Nova coluna: sistur_reservas.agreed_notices_json ---
    op.add_column(
        'sistur_reservas',
        sa.Column('agreed_notices_json', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('sistur_reservas', 'agreed_notices_json')
    op.drop_index('ix_sistur_resource_notices_end_date', table_name='sistur_resource_notices')
    op.drop_index('ix_sistur_resource_notices_start_date', table_name='sistur_resource_notices')
    op.drop_index('ix_sistur_resource_notices_source_id', table_name='sistur_resource_notices')
    op.drop_table('sistur_resource_notices')
