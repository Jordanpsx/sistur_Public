"""add payment transactions

Revision ID: 39dfe6dc3fa6
Revises: g1h2i3j4k5l6
Create Date: 2026-03-20 00:00:00.000000

Cria a tabela sistur_payment_transactions (ledger multi-pagamento) e adiciona
o valor PARTIALLY_PAID ao ENUM de status da tabela sistur_reservas.

Alterações:
  1. CREATE TABLE sistur_payment_transactions
       - id VARCHAR(36) PK (UUID gerado pela aplicação)
       - reserva_group_id VARCHAR(36) NOT NULL INDEX  — cross-version aggregation
       - reserva_id INT FK(sistur_reservas.id ON DELETE SET NULL) INDEX
       - status ENUM(PENDING,APPROVED,REJECTED,CANCELLED,REFUNDED) NOT NULL INDEX
       - amount NUMERIC(10,2) NOT NULL
       - payment_method VARCHAR(50)
       - mp_payment_id VARCHAR(100) INDEX
       - mp_status / mp_status_detail VARCHAR
       - qr_code / qr_code_base64 TEXT; ticket_url VARCHAR(512)
       - created_at / approved_at DATETIME(tz)
       - mp_response_snapshot JSON

  2. ALTER TABLE sistur_reservas
       MODIFY COLUMN status ENUM — insere PARTIALLY_PAID entre PENDING e PAID.
       Operação in-place no MySQL 5.7+ (não rebuilda a tabela).

NOTA: A migration gerada automaticamente pelo Alembic detectou incorretamente
uma renomeação de índices em sistur_advance_discount_rules (de
ix_advance_discount_rules_category / _source para nomes com prefixo
ix_sistur_...). Esses índices sustentam FK constraints e o MySQL recusa o DROP
com erro 1553. Como a renomeação não é uma alteração funcional necessária,
ela foi removida desta migration manualmente.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "39dfe6dc3fa6"
down_revision = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None


def upgrade():
    """Cria tabela de transações de pagamento e adiciona status PARTIALLY_PAID."""

    # ── 1. Cria tabela sistur_payment_transactions ───────────────────────────
    op.create_table(
        "sistur_payment_transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reserva_group_id", sa.String(length=36), nullable=False),
        sa.Column("reserva_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "REJECTED",
                "CANCELLED",
                "REFUNDED",
                name="transactionstatus",
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("payment_method", sa.String(length=50), nullable=True),
        sa.Column("mp_payment_id", sa.String(length=100), nullable=True),
        sa.Column("mp_status", sa.String(length=50), nullable=True),
        sa.Column("mp_status_detail", sa.String(length=100), nullable=True),
        sa.Column("qr_code", sa.Text(), nullable=True),
        sa.Column("qr_code_base64", sa.Text(), nullable=True),
        sa.Column("ticket_url", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mp_response_snapshot", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["reserva_id"],
            ["sistur_reservas.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sistur_payment_transactions_reserva_group_id",
        "sistur_payment_transactions",
        ["reserva_group_id"],
        unique=False,
    )
    op.create_index(
        "ix_sistur_payment_transactions_reserva_id",
        "sistur_payment_transactions",
        ["reserva_id"],
        unique=False,
    )
    op.create_index(
        "ix_sistur_payment_transactions_status",
        "sistur_payment_transactions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_sistur_payment_transactions_mp_payment_id",
        "sistur_payment_transactions",
        ["mp_payment_id"],
        unique=False,
    )

    # ── 2. Adiciona PARTIALLY_PAID ao ENUM de status das Reservas ────────────
    # Operação in-place no MySQL — sem rebuild de tabela.
    op.execute(
        "ALTER TABLE sistur_reservas "
        "MODIFY COLUMN status "
        "ENUM('PENDING','PARTIALLY_PAID','PAID','CANCELED','COMPLETED','ARCHIVED_VERSION') "
        "NOT NULL DEFAULT 'PENDING'"
    )


def downgrade():
    """Remove tabela de transações e reverte ENUM de status."""

    # Remove PARTIALLY_PAID do ENUM (seguro somente se nenhuma linha usa o valor)
    op.execute(
        "ALTER TABLE sistur_reservas "
        "MODIFY COLUMN status "
        "ENUM('PENDING','PAID','CANCELED','COMPLETED','ARCHIVED_VERSION') "
        "NOT NULL DEFAULT 'PENDING'"
    )

    op.drop_index(
        "ix_sistur_payment_transactions_mp_payment_id",
        table_name="sistur_payment_transactions",
    )
    op.drop_index(
        "ix_sistur_payment_transactions_status",
        table_name="sistur_payment_transactions",
    )
    op.drop_index(
        "ix_sistur_payment_transactions_reserva_id",
        table_name="sistur_payment_transactions",
    )
    op.drop_index(
        "ix_sistur_payment_transactions_reserva_group_id",
        table_name="sistur_payment_transactions",
    )
    op.drop_table("sistur_payment_transactions")
