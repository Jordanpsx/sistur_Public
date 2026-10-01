"""add_daily_prorated_hourly_to_billing_type_enum

Revision ID: d8f5ffd6a80c
Revises: d9e0f1a2b3c4
Create Date: 2026-03-18 10:17:52.898121

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8f5ffd6a80c'
down_revision = 'd9e0f1a2b3c4'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE sistur_reserva_items MODIFY COLUMN billing_type ENUM('FIXED', 'PER_DAY', 'PER_HOUR', 'DAILY_PRORATED_HOURLY') NOT NULL")

def downgrade():
    # Voltando para o estado anterior (cuidado: se houver dados novos, o MariaDB pode dar erro)
    op.execute("ALTER TABLE sistur_reserva_items MODIFY COLUMN billing_type ENUM('FIXED', 'PER_DAY', 'PER_HOUR') NOT NULL")
