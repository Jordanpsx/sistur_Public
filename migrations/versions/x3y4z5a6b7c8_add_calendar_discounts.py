"""Add calendar_discounts and calendar_discount_stay_tiers tables

Revision ID: x3y4z5a6b7c8
Revises: w2x3y4z5a6b7
Create Date: 2026-05-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "x3y4z5a6b7c8"
down_revision = "w2x3y4z5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sistur_calendar_discounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("discount_date", sa.Date(), nullable=False),
        sa.Column("discount_percentage", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sistur_reserva_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["sistur_reserva_categories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "category_id", "discount_date",
            name="uq_calendar_discount_source_cat_date",
        ),
    )
    op.create_index("ix_calendar_discount_source", "sistur_calendar_discounts", ["source_id"])
    op.create_index("ix_calendar_discount_date", "sistur_calendar_discounts", ["discount_date"])

    op.create_table(
        "sistur_calendar_discount_stay_tiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("calendar_discount_id", sa.Integer(), nullable=False),
        sa.Column("min_stay_days", sa.Integer(), nullable=False),
        sa.Column("discount_percentage", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["calendar_discount_id"],
            ["sistur_calendar_discounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "calendar_discount_id", "min_stay_days",
            name="uq_cal_stay_tier_discount_days",
        ),
    )
    op.create_index(
        "ix_cal_stay_tier_discount_id",
        "sistur_calendar_discount_stay_tiers",
        ["calendar_discount_id"],
    )


def downgrade():
    op.drop_index("ix_cal_stay_tier_discount_id", table_name="sistur_calendar_discount_stay_tiers")
    op.drop_table("sistur_calendar_discount_stay_tiers")
    op.drop_index("ix_calendar_discount_date", table_name="sistur_calendar_discounts")
    op.drop_index("ix_calendar_discount_source", table_name="sistur_calendar_discounts")
    op.drop_table("sistur_calendar_discounts")
