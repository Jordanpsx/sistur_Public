"""scope role permissions by reserva source

Revision ID: l6m7n8o9p0q1
Revises: fca5b8a12020
Create Date: 2026-06-18
"""

from alembic import op
import sqlalchemy as sa


revision = "l6m7n8o9p0q1"
down_revision = "fca5b8a12020"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sistur_role_permissions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch_op.drop_constraint("uq_role_modulo_acao", type_="unique")
        batch_op.create_foreign_key(
            "fk_role_permissions_source_id",
            "sistur_reserva_sources",
            ["source_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_sistur_role_permissions_source_id", ["source_id"])

    op.create_index(
        "uq_role_modulo_acao_global",
        "sistur_role_permissions",
        ["role_id", "modulo", "acao"],
        unique=True,
        postgresql_where=sa.text("source_id IS NULL"),
        sqlite_where=sa.text("source_id IS NULL"),
    )
    op.create_index(
        "uq_role_modulo_acao_source",
        "sistur_role_permissions",
        ["role_id", "modulo", "acao", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
        sqlite_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_role_modulo_acao_source", table_name="sistur_role_permissions")
    op.drop_index("uq_role_modulo_acao_global", table_name="sistur_role_permissions")

    with op.batch_alter_table("sistur_role_permissions", schema=None) as batch_op:
        batch_op.drop_index("ix_sistur_role_permissions_source_id")
        batch_op.drop_constraint("fk_role_permissions_source_id", type_="foreignkey")
        batch_op.create_unique_constraint(
            "uq_role_modulo_acao",
            ["role_id", "modulo", "acao"],
        )
        batch_op.drop_column("source_id")
