"""reserva_status_varchar

Revision ID: ae9fa1403163
Revises: i3j4k5l6m7n8
Create Date: 2026-03-27 11:17:37.969907

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'ae9fa1403163'
down_revision = 'i3j4k5l6m7n8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_reservas', schema=None) as batch_op:
        batch_op.alter_column('status',
               existing_type=mysql.ENUM('PENDING', 'PARTIALLY_PAID', 'PAID', 'CANCELED', 'COMPLETED', 'ARCHIVED_VERSION'),
               type_=sa.String(length=50),
               existing_nullable=False,
               existing_server_default=sa.text("'PENDING'"))


def downgrade():
    with op.batch_alter_table('sistur_reservas', schema=None) as batch_op:
        batch_op.alter_column('status',
               existing_type=sa.String(length=50),
               type_=mysql.ENUM('PENDING', 'PARTIALLY_PAID', 'PAID', 'CANCELED', 'COMPLETED', 'ARCHIVED_VERSION'),
               existing_nullable=False,
               existing_server_default=sa.text("'PENDING'"))
