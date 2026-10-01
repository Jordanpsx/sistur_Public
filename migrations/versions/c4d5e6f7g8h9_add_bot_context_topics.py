"""add bot context topics

Revision ID: c4d5e6f7g8h9
Revises: 79ca2dfbe2ac
Create Date: 2026-06-09
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7g8h9"
down_revision = "79ca2dfbe2ac"
branch_labels = None
depends_on = None


def upgrade():
    """Cria o catálogo versionado de contextos do bot."""
    op.create_table(
        "sistur_bot_context_topics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("selector_summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topics_json", sa.Text(), nullable=True),
        sa.Column("negative_topics_json", sa.Text(), nullable=True),
        sa.Column("sample_questions_json", sa.Text(), nullable=True),
        sa.Column("applies_to_json", sa.Text(), nullable=True),
        sa.Column("dependencies_json", sa.Text(), nullable=True),
        sa.Column("conflicts_json", sa.Text(), nullable=True),
        sa.Column(
            "priority",
            sa.String(length=20),
            nullable=False,
            server_default="informational",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("source_category", sa.String(length=100), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_bot_context_topics_slug"),
    )
    op.create_index(
        op.f("ix_sistur_bot_context_topics_slug"),
        "sistur_bot_context_topics",
        ["slug"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sistur_bot_context_topics_priority"),
        "sistur_bot_context_topics",
        ["priority"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sistur_bot_context_topics_is_active"),
        "sistur_bot_context_topics",
        ["is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sistur_bot_context_topics_source_category"),
        "sistur_bot_context_topics",
        ["source_category"],
        unique=False,
    )


def downgrade():
    """Remove o catálogo versionado de contextos do bot."""
    op.drop_index(
        op.f("ix_sistur_bot_context_topics_source_category"),
        table_name="sistur_bot_context_topics",
    )
    op.drop_index(
        op.f("ix_sistur_bot_context_topics_is_active"),
        table_name="sistur_bot_context_topics",
    )
    op.drop_index(
        op.f("ix_sistur_bot_context_topics_priority"),
        table_name="sistur_bot_context_topics",
    )
    op.drop_index(
        op.f("ix_sistur_bot_context_topics_slug"),
        table_name="sistur_bot_context_topics",
    )
    op.drop_table("sistur_bot_context_topics")
