"""add pdv dual cash reconciliation fields

Revision ID: 2a7c9d0e4f11
Revises: 9794af438eb5
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2a7c9d0e4f11"
down_revision = "9794af438eb5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sistur_pdv_shifts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "opening_float_pix",
                sa.Numeric(precision=10, scale=2),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("closing_counted_pix", sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column("closing_expected_pix", sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column("discrepancy_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("approved_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum("OPEN", "CLOSED", name="pdvshiftstatus", native_enum=False),
            type_=sa.Enum("OPEN", "PENDING_APPROVAL", "CLOSED", name="pdvshiftstatus", native_enum=False, length=20),
            existing_nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_sistur_pdv_shifts_approved_by_id_funcionarios",
            "sistur_funcionarios",
            ["approved_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("sistur_pdv_sales", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "change_source",
                sa.Enum("CASH", "PIX", name="pdvchangesource", native_enum=False, length=10),
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table("sistur_pdv_sales", schema=None) as batch_op:
        batch_op.drop_column("change_source")

    with op.batch_alter_table("sistur_pdv_shifts", schema=None) as batch_op:
        batch_op.drop_constraint("fk_sistur_pdv_shifts_approved_by_id_funcionarios", type_="foreignkey")
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum("OPEN", "PENDING_APPROVAL", "CLOSED", name="pdvshiftstatus", native_enum=False, length=20),
            type_=sa.Enum("OPEN", "CLOSED", name="pdvshiftstatus", native_enum=False),
            existing_nullable=False,
        )
        batch_op.drop_column("approved_at")
        batch_op.drop_column("approved_by_id")
        batch_op.drop_column("discrepancy_reason")
        batch_op.drop_column("closing_expected_pix")
        batch_op.drop_column("closing_counted_pix")
        batch_op.drop_column("opening_float_pix")
