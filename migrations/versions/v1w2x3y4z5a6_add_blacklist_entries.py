"""add blacklist entries

Revision ID: v1w2x3y4z5a6
Revises: 912f1334334c
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "v1w2x3y4z5a6"
down_revision = "912f1334334c"
branch_labels = None
depends_on = None


def upgrade():
    """Cria tabelas da Lista Negra de Reservas."""
    op.create_table(
        "sistur_blacklist_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("cpf", sa.String(length=20), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("block_by_name", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("block_by_cpf", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("block_by_phone", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("block_by_email", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "block_by_name = 1 OR block_by_cpf = 1 OR block_by_phone = 1 OR block_by_email = 1",
            name="ck_blacklist_entry_has_rule",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_blacklist_entries_cpf",
        "sistur_blacklist_entries",
        ["cpf"],
        unique=False,
    )
    op.create_index(
        "ix_blacklist_entries_deleted_at",
        "sistur_blacklist_entries",
        ["deleted_at"],
        unique=False,
    )
    op.create_table(
        "sistur_blacklist_entry_sources",
        sa.Column("blacklist_entry_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["blacklist_entry_id"],
            ["sistur_blacklist_entries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sistur_reserva_sources.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("blacklist_entry_id", "source_id"),
    )


def downgrade():
    """Remove tabelas da Lista Negra de Reservas."""
    op.drop_table("sistur_blacklist_entry_sources")
    op.drop_index("ix_blacklist_entries_deleted_at", table_name="sistur_blacklist_entries")
    op.drop_index("ix_blacklist_entries_cpf", table_name="sistur_blacklist_entries")
    op.drop_table("sistur_blacklist_entries")
