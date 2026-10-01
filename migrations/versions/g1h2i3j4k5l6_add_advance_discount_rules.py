"""add_advance_discount_rules

Revision ID: g1h2i3j4k5l6
Revises: f0a1b2c3d4e5
Create Date: 2026-03-19 10:00:00.000000

Cria a tabela sistur_advance_discount_rules para o motor de desconto
por reserva antecipada (Early Bird).

Cada regra define:
  - source_id      — FK para ReservaSource (NOT NULL, CASCADE DELETE)
  - category_id    — FK para ReservaCategory (NULL = todas categorias da fonte)
  - min_days_in_advance — mínimo de dias de antecedência para ativar o desconto
  - discount_percentage — percentual de desconto sobre o subtotal (DECIMAL 5,2)
  - is_active      — flag de ativação; permite suspensão temporária sem exclusão
  - criado_em / atualizado_em — timestamps de auditoria

Lógica de seleção na engine de precificação:
  - Filtra regras ativas onde days_in_advance >= min_days_in_advance
  - Prefere regra category-specific sobre regra source-wide (category_id IS NULL)
  - Seleciona a regra com maior min_days_in_advance no nível vencedor
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g1h2i3j4k5l6'
down_revision = 'f0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE sistur_advance_discount_rules ("
        "  id INT NOT NULL AUTO_INCREMENT, "
        "  source_id INT NOT NULL, "
        "  category_id INT NULL, "
        "  min_days_in_advance INT NOT NULL, "
        "  discount_percentage DECIMAL(5,2) NOT NULL, "
        "  is_active TINYINT(1) NOT NULL DEFAULT 1, "
        "  criado_em DATETIME NOT NULL, "
        "  atualizado_em DATETIME NOT NULL, "
        "  PRIMARY KEY (id), "
        "  INDEX ix_advance_discount_rules_source (source_id), "
        "  INDEX ix_advance_discount_rules_category (category_id), "
        "  CONSTRAINT fk_adr_source FOREIGN KEY (source_id) "
        "    REFERENCES sistur_reserva_sources(id) ON DELETE CASCADE, "
        "  CONSTRAINT fk_adr_category FOREIGN KEY (category_id) "
        "    REFERENCES sistur_reserva_categories(id) ON DELETE CASCADE"
        ")"
    )


def downgrade():
    op.execute("DROP TABLE sistur_advance_discount_rules")
