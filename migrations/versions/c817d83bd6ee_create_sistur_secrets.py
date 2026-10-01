"""Create sistur_secrets table (cofre interno de segredos)

Revision ID: c817d83bd6ee
Revises: l6m7n8o9p0q1
Create Date: 2026-06-19 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = 'c817d83bd6ee'
down_revision = 'l6m7n8o9p0q1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sistur_secrets',
        sa.Column('id',              sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column('key',             sa.String(120), nullable=False),
        sa.Column('provider',        sa.String(50),  nullable=True),
        sa.Column('label',           sa.String(120), nullable=True),
        sa.Column('value_encrypted', sa.Text(),      nullable=False),
        sa.Column('last4',           sa.String(8),   nullable=True),
        sa.Column('ativo',           sa.Boolean(),   nullable=False, server_default=sa.true()),
        sa.Column('updated_by_id',   sa.Integer(),   nullable=True),
        sa.Column('criado_em',       sa.DateTime(),  nullable=False),
        sa.Column('atualizado_em',   sa.DateTime(),  nullable=False),
    )
    op.create_index('ix_sistur_secrets_key',           'sistur_secrets', ['key'], unique=True)
    op.create_index('ix_sistur_secrets_provider',      'sistur_secrets', ['provider'])
    op.create_index('ix_sistur_secrets_ativo',         'sistur_secrets', ['ativo'])
    op.create_index('ix_sistur_secrets_updated_by_id', 'sistur_secrets', ['updated_by_id'])


def downgrade():
    op.drop_index('ix_sistur_secrets_updated_by_id', 'sistur_secrets')
    op.drop_index('ix_sistur_secrets_ativo',         'sistur_secrets')
    op.drop_index('ix_sistur_secrets_provider',      'sistur_secrets')
    op.drop_index('ix_sistur_secrets_key',           'sistur_secrets')
    op.drop_table('sistur_secrets')
