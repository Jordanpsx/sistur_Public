"""Add advance_discount_stay_tiers table

Revision ID: w2x3y4z5a6b7
Revises: v1w2x3y4z5a6
Create Date: 2026-05-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "w2x3y4z5a6b7"
down_revision = "v1w2x3y4z5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sistur_advance_discount_stay_tiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("min_stay_days", sa.Integer(), nullable=False),
        sa.Column("discount_percentage", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["sistur_advance_discount_rules.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "min_stay_days", name="uq_stay_tier_rule_days"),
    )
    op.create_index(
        "ix_stay_tier_rule_id",
        "sistur_advance_discount_stay_tiers",
        ["rule_id"],
    )


def downgrade():
    op.drop_index("ix_stay_tier_rule_id", table_name="sistur_advance_discount_stay_tiers")
    op.drop_table("sistur_advance_discount_stay_tiers")
