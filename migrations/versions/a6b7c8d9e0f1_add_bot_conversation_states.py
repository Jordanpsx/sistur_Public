"""Add bot conversation states

Revision ID: a6b7c8d9e0f1
Revises: z5a6b7c8d9e0
Create Date: 2026-05-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "a6b7c8d9e0f1"
down_revision = "z5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sistur_bot_conversation_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("conversation_id", sa.String(length=100), nullable=False),
        sa.Column("inbox_id", sa.Integer(), nullable=True),
        sa.Column("contact_id", sa.String(length=100), nullable=True),
        sa.Column("customer_phone", sa.String(length=30), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("customer_document", sa.String(length=30), nullable=True),
        sa.Column("current_intent", sa.String(length=80), nullable=True),
        sa.Column("current_step", sa.String(length=80), nullable=True),
        sa.Column("draft_json", sa.Text(), nullable=True),
        sa.Column("last_quote_json", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("last_reservation_id", sa.Integer(), nullable=True),
        sa.Column("awaiting_confirmation", sa.Boolean(), nullable=False),
        sa.Column("awaiting_payment_method", sa.Boolean(), nullable=False),
        sa.Column("handoff_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "conversation_id",
            name="uq_bot_conversation_state_provider_conversation",
        ),
    )
    op.create_index("ix_bot_conversation_state_inbox_id", "sistur_bot_conversation_states", ["inbox_id"])
    op.create_index("ix_bot_conversation_state_customer_phone", "sistur_bot_conversation_states", ["customer_phone"])
    op.create_index("ix_bot_conversation_state_updated_at", "sistur_bot_conversation_states", ["updated_at"])


def downgrade():
    op.drop_index("ix_bot_conversation_state_updated_at", table_name="sistur_bot_conversation_states")
    op.drop_index("ix_bot_conversation_state_customer_phone", table_name="sistur_bot_conversation_states")
    op.drop_index("ix_bot_conversation_state_inbox_id", table_name="sistur_bot_conversation_states")
    op.drop_table("sistur_bot_conversation_states")
