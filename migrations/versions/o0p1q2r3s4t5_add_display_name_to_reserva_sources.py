"""add_display_name_to_reserva_sources

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2026-04-13 00:00:00.000000

Mudanças:
  - Adiciona coluna display_name (VARCHAR 100) em sistur_reserva_sources.
  - Retrocompatível: coluna inicializada com o valor de name para todos os
    registros existentes (backfill via UPDATE).
  - Permite que operadores alterem o nome de exibição sem afetar o identificador
    de sistema usado por integrações externas (name permanece imutável).
"""
from alembic import op
import sqlalchemy as sa


revision = 'o0p1q2r3s4t5'
down_revision = 'n9o0p1q2r3s4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'sistur_reserva_sources',
        sa.Column('display_name', sa.String(100), nullable=False, server_default=''),
    )
    # Retrocompatibilidade: inicializa display_name com o valor atual de name.
    op.execute(
        "UPDATE sistur_reserva_sources SET display_name = name "
        "WHERE display_name = '' OR display_name IS NULL"
    )


def downgrade():
    op.drop_column('sistur_reserva_sources', 'display_name')
