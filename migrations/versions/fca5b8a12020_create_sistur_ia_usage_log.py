"""Create sistur_ia_usage_log table

Revision ID: fca5b8a12020
Revises: j5k6l7m8n9o0
Create Date: 2026-06-17 13:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = 'fca5b8a12020'
down_revision = 'j5k6l7m8n9o0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sistur_ia_usage_log',
        sa.Column('id',                sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column('operacao',          sa.String(20),     nullable=False),
        sa.Column('modulo',            sa.String(50),     nullable=False),
        sa.Column('ator_id',           sa.Integer(),      nullable=True),
        sa.Column('modelo',            sa.String(80),     nullable=False),
        sa.Column('prompt_tokens',     sa.Integer(),      nullable=False, server_default='0'),
        sa.Column('completion_tokens', sa.Integer(),      nullable=False, server_default='0'),
        sa.Column('total_tokens',      sa.Integer(),      nullable=False, server_default='0'),
        sa.Column('latencia_ms',       sa.Integer(),      nullable=True),
        sa.Column('sucesso',           sa.Boolean(),      nullable=False, server_default=sa.true()),
        sa.Column('erro',              sa.String(255),    nullable=True),
        sa.Column('created_at',        sa.DateTime(),     nullable=False),
    )
    op.create_index('ix_ia_usage_operacao',   'sistur_ia_usage_log', ['operacao'])
    op.create_index('ix_ia_usage_modulo',     'sistur_ia_usage_log', ['modulo'])
    op.create_index('ix_ia_usage_ator',       'sistur_ia_usage_log', ['ator_id'])
    op.create_index('ix_ia_usage_sucesso',    'sistur_ia_usage_log', ['sucesso'])
    op.create_index('ix_ia_usage_created_at', 'sistur_ia_usage_log', ['created_at'])


def downgrade():
    op.drop_index('ix_ia_usage_created_at', 'sistur_ia_usage_log')
    op.drop_index('ix_ia_usage_sucesso',    'sistur_ia_usage_log')
    op.drop_index('ix_ia_usage_ator',       'sistur_ia_usage_log')
    op.drop_index('ix_ia_usage_modulo',     'sistur_ia_usage_log')
    op.drop_index('ix_ia_usage_operacao',   'sistur_ia_usage_log')
    op.drop_table('sistur_ia_usage_log')
