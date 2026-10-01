"""Create sistur_bot_escalation_logs table

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-06-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = 'j5k6l7m8n9o0'
down_revision = 'i4j5k6l7m8n9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sistur_bot_escalation_logs',
        sa.Column('id',               sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column('source_id',        sa.Integer(),  nullable=True),
        sa.Column('inbox_id',         sa.Integer(),  nullable=True),
        sa.Column('conversation_id',  sa.String(64), nullable=False),
        sa.Column('pergunta_cliente', sa.Text(),     nullable=True),
        sa.Column('descricao_bot',    sa.Text(),     nullable=True),
        sa.Column('status',           sa.String(32), nullable=False, server_default='pending'),
        sa.Column('created_at',       sa.DateTime(), nullable=True),
        sa.Column('updated_at',       sa.DateTime(), nullable=True),
    )
    op.create_index('ix_bot_escalation_source',       'sistur_bot_escalation_logs', ['source_id'])
    op.create_index('ix_bot_escalation_inbox',        'sistur_bot_escalation_logs', ['inbox_id'])
    op.create_index('ix_bot_escalation_conversation', 'sistur_bot_escalation_logs', ['conversation_id'])
    op.create_index('ix_bot_escalation_status',       'sistur_bot_escalation_logs', ['status'])


def downgrade():
    op.drop_index('ix_bot_escalation_status',       'sistur_bot_escalation_logs')
    op.drop_index('ix_bot_escalation_conversation', 'sistur_bot_escalation_logs')
    op.drop_index('ix_bot_escalation_inbox',        'sistur_bot_escalation_logs')
    op.drop_index('ix_bot_escalation_source',       'sistur_bot_escalation_logs')
    op.drop_table('sistur_bot_escalation_logs')
