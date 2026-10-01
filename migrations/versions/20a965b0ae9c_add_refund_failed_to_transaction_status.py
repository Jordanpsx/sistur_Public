"""Add REFUND_FAILED to TransactionStatus

Revision ID: 20a965b0ae9c
Revises: 69546d499593
Create Date: 2026-04-23

"""
from alembic import op

revision = '20a965b0ae9c'
down_revision = '69546d499593'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE sistur_payment_transactions "
        "MODIFY COLUMN status ENUM("
        "'PENDING','APPROVED','REJECTED','CANCELLED','REFUNDED','REFUND_FAILED'"
        ") NOT NULL DEFAULT 'PENDING'"
    )


def downgrade():
    op.execute(
        "ALTER TABLE sistur_payment_transactions "
        "MODIFY COLUMN status ENUM("
        "'PENDING','APPROVED','REJECTED','CANCELLED','REFUNDED'"
        ") NOT NULL DEFAULT 'PENDING'"
    )
