"""add feedback fields to sistur_bot_conversation_contexts

Campos de auditoria de qualidade do seletor de contextos: feedback (good/bad/...)
e feedback_note (texto livre do operador ou do sistema).

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-06-10
"""

revision = "i4j5k6l7m8n9"
down_revision = "h3i4j5k6l7m8"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column(
        "sistur_bot_conversation_contexts",
        sa.Column("feedback", sa.String(30), nullable=True),
    )
    op.add_column(
        "sistur_bot_conversation_contexts",
        sa.Column("feedback_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sistur_bot_conversation_contexts", "feedback_note")
    op.drop_column("sistur_bot_conversation_contexts", "feedback")
