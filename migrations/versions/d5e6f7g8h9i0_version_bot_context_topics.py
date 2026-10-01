"""version bot context topics

Revision ID: d5e6f7g8h9i0
Revises: c4d5e6f7g8h9
Create Date: 2026-06-09
"""

from alembic import op


revision = "d5e6f7g8h9i0"
down_revision = "c4d5e6f7g8h9"
branch_labels = None
depends_on = None


def upgrade():
    """Permite preservar várias versões do mesmo slug."""
    op.drop_constraint(
        "uq_bot_context_topics_slug",
        "sistur_bot_context_topics",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_bot_context_topics_slug_version",
        "sistur_bot_context_topics",
        ["slug", "version"],
    )


def downgrade():
    """Restaura a unicidade simples por slug."""
    op.drop_constraint(
        "uq_bot_context_topics_slug_version",
        "sistur_bot_context_topics",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_bot_context_topics_slug",
        "sistur_bot_context_topics",
        ["slug"],
    )
