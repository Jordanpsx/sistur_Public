"""add_seen_notes_operator_to_reservas

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-03-18 16:00:00.000000

Adiciona três colunas à tabela sistur_reservas:
  - criado_por_id: FK para sistur_funcionarios — operador que criou/editou a versão
  - visualizada_em: DATETIME — registra quando a reserva foi aberta pela 1ª vez
  - notas_operador: TEXT — anotações internas do operador
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0a1b2c3d4e5'
down_revision = 'e9f0a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE sistur_reservas "
        "ADD COLUMN criado_por_id INT NULL, "
        "ADD COLUMN visualizada_em DATETIME NULL, "
        "ADD COLUMN notas_operador TEXT NULL, "
        "ADD INDEX ix_sistur_reservas_criado_por_id (criado_por_id), "
        "ADD CONSTRAINT fk_reservas_criado_por "
        "  FOREIGN KEY (criado_por_id) REFERENCES sistur_funcionarios(id) ON DELETE SET NULL"
    )


def downgrade():
    op.execute(
        "ALTER TABLE sistur_reservas "
        "DROP FOREIGN KEY fk_reservas_criado_por, "
        "DROP INDEX ix_sistur_reservas_criado_por_id, "
        "DROP COLUMN criado_por_id, "
        "DROP COLUMN visualizada_em, "
        "DROP COLUMN notas_operador"
    )
