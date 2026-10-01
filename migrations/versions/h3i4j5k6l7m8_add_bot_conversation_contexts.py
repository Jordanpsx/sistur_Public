"""add sistur_bot_conversation_contexts

Rastreamento de contextos ativos por conversa: associa BotConversationState
a versões específicas do BotContextTopic com estado (active/historical/expired),
origem, confiança e timestamps para auditoria e invalidação.

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-06-09
"""

revision = "h3i4j5k6l7m8"
down_revision = "g2h3i4j5k6l7"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade():
    op.create_table(
        "sistur_bot_conversation_contexts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_state_id",
            sa.Integer(),
            sa.ForeignKey(
                "sistur_bot_conversation_states.id",
                ondelete="CASCADE",
                name="fk_bot_conv_ctx_state",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "topic_id",
            sa.Integer(),
            sa.ForeignKey(
                "sistur_bot_context_topics.id",
                ondelete="RESTRICT",
                name="fk_bot_conv_ctx_topic",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "reserva_id",
            sa.Integer(),
            sa.ForeignKey(
                "sistur_reservas.id",
                ondelete="SET NULL",
                name="fk_bot_conv_ctx_reserva",
            ),
            nullable=True,
            index=True,
        ),
        sa.Column("state", sa.String(20), nullable=False, server_default="active", index=True),
        sa.Column("origin", sa.String(20), nullable=False, server_default="selector"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("expiry_reason", sa.String(80), nullable=True),
        sa.Column("raw_selection_json", sa.Text(), nullable=True),
        sa.Column("selected_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("expired_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("sistur_bot_conversation_contexts")
