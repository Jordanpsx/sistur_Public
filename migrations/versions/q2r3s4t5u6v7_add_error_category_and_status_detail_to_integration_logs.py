"""Add error_category and mp_status_detail to sistur_integration_logs

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-04-17

error_category — classifies each log as 'system', 'client_bank' or 'invalid_data'
                 for fast UI filtering without inspecting raw_payload.
mp_status_detail — stores the MP status_detail string (e.g. cc_rejected_insufficient_amount)
                   as an indexed column instead of burying it inside raw_payload JSON.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'q2r3s4t5u6v7'
down_revision = 'p1q2r3s4t5u6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_integration_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('error_category', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('mp_status_detail', sa.String(length=100), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_sistur_integration_logs_error_category'),
            ['error_category'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('sistur_integration_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sistur_integration_logs_error_category'))
        batch_op.drop_column('mp_status_detail')
        batch_op.drop_column('error_category')
