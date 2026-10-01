"""Add min_stay_days to CalendarDiscountAdvanceTier (2D tiers)

Revision ID: z5a6b7c8d9e0
Revises: y4z5a6b7c8d9
Create Date: 2026-05-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "z5a6b7c8d9e0"
down_revision = "y4z5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    # Add nullable min_stay_days column
    op.add_column(
        "sistur_calendar_discount_stay_tiers",
        sa.Column("min_stay_days", sa.Integer(), nullable=True),
    )

    # Replace 2-column unique constraint with 3-column one
    op.drop_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        ["calendar_discount_id", "min_days_in_advance", "min_stay_days"],
    )


def downgrade():
    op.drop_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        ["calendar_discount_id", "min_days_in_advance"],
    )
    op.drop_column("sistur_calendar_discount_stay_tiers", "min_stay_days")
