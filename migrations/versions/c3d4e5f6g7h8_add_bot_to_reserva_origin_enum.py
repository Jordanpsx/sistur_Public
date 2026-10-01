"""Add BOT to ReservaOrigin enum and set origin=BOT for bot reservations.

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-06-04

"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Ampliar o ENUM para incluir BOT
    op.execute(
        "ALTER TABLE sistur_reservas "
        "MODIFY COLUMN origin ENUM('WEB','BALCAO','BOT') NOT NULL"
    )

    # 2. Corrigir historicamente reservas do bot que foram gravadas como WEB.
    #    Critério seguro: metadata_info contém "origem_canal" = "bot"
    #    (gravado automaticamente desde o início pela bot_api).
    op.execute(
        """
        UPDATE sistur_reservas
        SET origin = 'BOT'
        WHERE origin = 'WEB'
          AND JSON_UNQUOTE(JSON_EXTRACT(metadata_info, '$.origem_canal')) = 'bot'
        """
    )


def downgrade():
    # Reverter reservas BOT para WEB antes de remover o valor do ENUM
    op.execute(
        "UPDATE sistur_reservas SET origin = 'WEB' WHERE origin = 'BOT'"
    )
    op.execute(
        "ALTER TABLE sistur_reservas "
        "MODIFY COLUMN origin ENUM('WEB','BALCAO') NOT NULL"
    )
