"""scope bot intelligence by source

Revision ID: e6f7g8h9i0j1
Revises: d5e6f7g8h9i0
Create Date: 2026-06-09
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "e6f7g8h9i0j1"
down_revision = "d5e6f7g8h9i0"
branch_labels = None
depends_on = None


def upgrade():
    """Cria instâncias independentes do bot e migra as categorias existentes."""
    op.create_table(
        "sistur_bot_venue_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("global_rules_json", sa.Text(), nullable=True),
        sa.Column("agent_routing_json", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sistur_reserva_sources.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", name="uq_bot_venue_configs_source_id"),
    )
    op.create_index(
        op.f("ix_sistur_bot_venue_configs_source_id"),
        "sistur_bot_venue_configs",
        ["source_id"],
        unique=True,
    )

    with op.batch_alter_table("sistur_bot_intelligence") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_id",
                sa.Integer(),
                nullable=True,
                comment="Local proprietário desta categoria do Bot Intelligence",
            )
        )
        batch_op.drop_constraint("uq_bot_intelligence_category", type_="unique")

    with op.batch_alter_table("sistur_bot_context_topics") as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch_op.drop_constraint(
            "uq_bot_context_topics_slug_version",
            type_="unique",
        )

    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sources = bind.execute(sa.text(
        "SELECT id FROM sistur_reserva_sources "
        "WHERE deleted_at IS NULL AND is_active = 1 ORDER BY id"
    )).fetchall()
    if not sources:
        raise RuntimeError("Não existe ReservaSource ativo para migrar o Bot Intelligence.")

    first_source_id = int(sources[0][0])
    bind.execute(sa.text(
        "UPDATE sistur_bot_context_topics SET source_id = :source_id "
        "WHERE source_id IS NULL"
    ), {"source_id": first_source_id})
    settings = dict(bind.execute(sa.text(
        "SELECT chave, valor FROM sistur_system_settings "
        "WHERE chave IN ('bot.global_rules', 'bot.agent_routing', 'bot.ativo')"
    )).fetchall())
    global_rules = settings.get("bot.global_rules")
    agent_routing = settings.get("bot.agent_routing")
    active_raw = str(settings.get("bot.ativo", "true")).strip().lower()
    is_active = active_raw in {"1", "true", "yes", "on"}

    for source in sources:
        source_id = int(source[0])
        inherits_legacy = source_id == first_source_id
        bind.execute(sa.text(
            "INSERT INTO sistur_bot_venue_configs "
            "(source_id, global_rules_json, agent_routing_json, is_active, created_at, updated_at) "
            "VALUES (:source_id, :global_rules, :agent_routing, :is_active, :created_at, :updated_at)"
        ), {
            "source_id": source_id,
            "global_rules": global_rules if inherits_legacy else None,
            "agent_routing": agent_routing if inherits_legacy else None,
            "is_active": is_active if inherits_legacy else False,
            "created_at": now,
            "updated_at": now,
        })

    contexts = bind.execute(sa.text(
        "SELECT id, category, identity_json, business_rules_text, last_updated "
        "FROM sistur_bot_intelligence ORDER BY id"
    )).fetchall()
    for context in contexts:
        source_rows = bind.execute(sa.text(
            "SELECT source_id FROM sistur_bot_intelligence_locations "
            "WHERE bot_intelligence_id = :context_id ORDER BY source_id"
        ), {"context_id": int(context[0])}).fetchall()
        source_ids = [int(row[0]) for row in source_rows] or [first_source_id]
        bind.execute(sa.text(
            "UPDATE sistur_bot_intelligence SET source_id = :source_id WHERE id = :context_id"
        ), {"source_id": source_ids[0], "context_id": int(context[0])})

        for extra_source_id in source_ids[1:]:
            bind.execute(sa.text(
                "INSERT INTO sistur_bot_intelligence "
                "(source_id, category, identity_json, business_rules_text, last_updated) "
                "VALUES (:source_id, :category, :identity_json, :business_rules_text, :last_updated)"
            ), {
                "source_id": extra_source_id,
                "category": context[1],
                "identity_json": context[2],
                "business_rules_text": context[3],
                "last_updated": context[4] or now,
            })

    with op.batch_alter_table("sistur_bot_intelligence") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_bot_intelligence_source_id",
            "sistur_reserva_sources",
            ["source_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_bot_intelligence_source_category",
            ["source_id", "category"],
        )
        batch_op.create_index(
            "ix_sistur_bot_intelligence_source_id",
            ["source_id"],
            unique=False,
        )

    with op.batch_alter_table("sistur_bot_context_topics") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_bot_context_topics_source_id",
            "sistur_reserva_sources",
            ["source_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_bot_context_topics_source_slug_version",
            ["source_id", "slug", "version"],
        )
        batch_op.create_index(
            "ix_sistur_bot_context_topics_source_id",
            ["source_id"],
            unique=False,
        )


def downgrade():
    """Restaura o modelo global, abortando se houver slugs duplicados."""
    bind = op.get_bind()
    duplicate = bind.execute(sa.text(
        "SELECT category FROM sistur_bot_intelligence "
        "GROUP BY category HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError(
            "Downgrade bloqueado: existem categorias duplicadas entre locais."
        )

    with op.batch_alter_table("sistur_bot_context_topics") as batch_op:
        batch_op.drop_index("ix_sistur_bot_context_topics_source_id")
        batch_op.drop_constraint(
            "uq_bot_context_topics_source_slug_version",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_bot_context_topics_source_id",
            type_="foreignkey",
        )
        batch_op.drop_column("source_id")
        batch_op.create_unique_constraint(
            "uq_bot_context_topics_slug_version",
            ["slug", "version"],
        )

    with op.batch_alter_table("sistur_bot_intelligence") as batch_op:
        batch_op.drop_index("ix_sistur_bot_intelligence_source_id")
        batch_op.drop_constraint(
            "uq_bot_intelligence_source_category",
            type_="unique",
        )
        batch_op.drop_constraint("fk_bot_intelligence_source_id", type_="foreignkey")
        batch_op.drop_column("source_id")
        batch_op.create_unique_constraint(
            "uq_bot_intelligence_category",
            ["category"],
        )

    op.drop_index(
        op.f("ix_sistur_bot_venue_configs_source_id"),
        table_name="sistur_bot_venue_configs",
    )
    op.drop_table("sistur_bot_venue_configs")
