"""Refactor CalendarDiscount: advance tiers replace stay tiers

Revision ID: y4z5a6b7c8d9
Revises: x3y4z5a6b7c8
Create Date: 2026-05-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "y4z5a6b7c8d9"
down_revision = "x3y4z5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    # Make discount_percentage nullable — no longer used as base rate
    op.alter_column(
        "sistur_calendar_discounts",
        "discount_percentage",
        existing_type=sa.Numeric(precision=5, scale=2),
        nullable=True,
    )

    # Rename min_stay_days → min_days_in_advance in tier table.
    # MySQL 5.7/MariaDB 10.3 don't support RENAME COLUMN, so we add+copy+drop.
    op.add_column(
        "sistur_calendar_discount_stay_tiers",
        sa.Column("min_days_in_advance", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE sistur_calendar_discount_stay_tiers "
        "SET min_days_in_advance = min_stay_days"
    )
    op.alter_column(
        "sistur_calendar_discount_stay_tiers",
        "min_days_in_advance",
        existing_type=sa.Integer(),
        nullable=False,
    )

    # Drop unique constraint that references the old column name
    op.drop_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        type_="unique",
    )
    op.drop_column("sistur_calendar_discount_stay_tiers", "min_stay_days")

    # Re-add unique constraint with the new column name
    op.create_unique_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        ["calendar_discount_id", "min_days_in_advance"],
    )


def downgrade():
    op.drop_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        type_="unique",
    )
    op.add_column(
        "sistur_calendar_discount_stay_tiers",
        sa.Column("min_stay_days", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE sistur_calendar_discount_stay_tiers "
        "SET min_stay_days = min_days_in_advance"
    )
    op.alter_column(
        "sistur_calendar_discount_stay_tiers",
        "min_stay_days",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("sistur_calendar_discount_stay_tiers", "min_days_in_advance")
    op.create_unique_constraint(
        "uq_cal_stay_tier_discount_days",
        "sistur_calendar_discount_stay_tiers",
        ["calendar_discount_id", "min_stay_days"],
    )
    op.alter_column(
        "sistur_calendar_discounts",
        "discount_percentage",
        existing_type=sa.Numeric(precision=5, scale=2),
        nullable=False,
    )
