"""Add single_day_only to reserva categories.

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f7
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6g7"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sistur_reserva_categories",
        sa.Column(
            "single_day_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.execute(
        """
        UPDATE sistur_reserva_categories
           SET single_day_only = 1
         WHERE deleted_at IS NULL
           AND LOWER(name) IN ('day use', 'dayuse', 'day-use', 'day use');
        """
    )
    op.execute(
        """
        UPDATE sistur_reservas r
        JOIN sistur_reserva_categories c ON c.id = r.category_id
           SET r.check_out_date = NULL
         WHERE c.single_day_only = 1
           AND r.check_out_date IS NOT NULL
           AND DATE(r.check_out_date) > DATE(r.check_in_date);
        """
    )

    with op.batch_alter_table("sistur_reserva_categories") as batch_op:
        batch_op.alter_column("single_day_only", server_default=None)


def downgrade():
    op.drop_column("sistur_reserva_categories", "single_day_only")
