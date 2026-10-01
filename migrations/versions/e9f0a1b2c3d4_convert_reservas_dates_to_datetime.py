"""convert_reservas_dates_to_datetime

Revision ID: e9f0a1b2c3d4
Revises: d8f5ffd6a80c
Create Date: 2026-03-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e9f0a1b2c3d4'
down_revision = 'd8f5ffd6a80c'
branch_labels = None
depends_on = None


def upgrade():
    # Converte check_in_date e check_out_date de DATE para DATETIME,
    # preservando dados existentes (MySQL/MariaDB converte DATE → DATETIME com hora 00:00:00).
    op.execute(
        "ALTER TABLE sistur_reservas "
        "MODIFY COLUMN check_in_date DATETIME NOT NULL, "
        "MODIFY COLUMN check_out_date DATETIME NULL"
    )


def downgrade():
    op.execute(
        "ALTER TABLE sistur_reservas "
        "MODIFY COLUMN check_in_date DATE NOT NULL, "
        "MODIFY COLUMN check_out_date DATE NULL"
    )
