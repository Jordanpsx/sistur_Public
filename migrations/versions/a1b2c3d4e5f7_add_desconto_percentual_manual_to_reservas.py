"""add_desconto_percentual_manual_to_reservas

Revision ID: a1b2c3d4e5f7
Revises: z5a6b7c8d9e0
Create Date: 2026-05-27 00:00:00.000000

Adiciona coluna `desconto_percentual_manual` em `sistur_reservas`.

Motivação: O desconto manual era armazenado como valor absoluto (R$) em
metadata_info["_manual_discount_amount"], o que causava perda do desconto
em novas versões da reserva (mudança de status, adição de itens), pois
`ReservaService.atualizar()` não recuperava o valor do metadata.

A solução é salvar o desconto como percentual em uma coluna dedicada, de
modo que toda nova versão herde e re-aplique o desconto proporcionalmente
ao subtotal correto (incluindo novos itens).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f7"
down_revision = "bec65ee5179c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sistur_reservas",
        sa.Column(
            "desconto_percentual_manual",
            sa.Numeric(precision=5, scale=2),
            nullable=True,
            comment="Percentual de desconto manual aplicado pelo operador (ex: 10.00 = 10%). "
                    "Herdado em todas as versões para manter proporcionalidade do desconto.",
        ),
    )


def downgrade():
    op.drop_column("sistur_reservas", "desconto_percentual_manual")
