"""lift identity and tools to venue config

Revision ID: f1g2h3i4j5k6
Revises: e6f7g8h9i0j1
Create Date: 2026-06-09
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


revision = "f1g2h3i4j5k6"
down_revision = "e6f7g8h9i0j1"
branch_labels = None
depends_on = None


def upgrade():
    """Move identity e tools de BotIntelligence (por categoria) para BotVenueConfig (por local).

    Para cada source, coleta o identity_json do registro 'geral' ou do primeiro
    registro disponível.  Separa o bloco 'ai_config' em tools_json e salva o
    restante em identity_json do BotVenueConfig.  Por fim, remove a coluna
    identity_json de sistur_bot_intelligence.
    """
    bind = op.get_bind()

    # 1. Adicionar novas colunas no BotVenueConfig
    with op.batch_alter_table("sistur_bot_venue_configs") as batch_op:
        batch_op.add_column(sa.Column("identity_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("tools_json", sa.Text(), nullable=True))

    # 2. Migrar dados: para cada source, pegar o identity_json da categoria 'geral'
    #    ou do primeiro registro; separar ai_config em tools_json.
    rows = bind.execute(text(
        "SELECT DISTINCT source_id FROM sistur_bot_intelligence WHERE source_id IS NOT NULL"
    )).fetchall()

    for (source_id,) in rows:
        # Prefere a categoria 'geral', senão pega qualquer uma
        row = bind.execute(text(
            "SELECT identity_json FROM sistur_bot_intelligence "
            "WHERE source_id = :sid AND category = 'geral' AND identity_json IS NOT NULL "
            "LIMIT 1"
        ), {"sid": source_id}).fetchone()

        if row is None:
            row = bind.execute(text(
                "SELECT identity_json FROM sistur_bot_intelligence "
                "WHERE source_id = :sid AND identity_json IS NOT NULL "
                "LIMIT 1"
            ), {"sid": source_id}).fetchone()

        if row is None or not row[0]:
            continue

        try:
            full = json.loads(row[0])
        except (TypeError, ValueError):
            continue

        ai_cfg = full.pop("ai_config", None)

        identity_str = json.dumps(full, ensure_ascii=False) if full else None
        tools_str = json.dumps({"ai_config": ai_cfg}, ensure_ascii=False) if ai_cfg else None

        bind.execute(text(
            "UPDATE sistur_bot_venue_configs "
            "SET identity_json = :ident, tools_json = :tools "
            "WHERE source_id = :sid"
        ), {"ident": identity_str, "tools": tools_str, "sid": source_id})

    # 3. Remover identity_json de sistur_bot_intelligence
    with op.batch_alter_table("sistur_bot_intelligence") as batch_op:
        batch_op.drop_column("identity_json")


def downgrade():
    """Restaura identity_json em BotIntelligence e remove colunas do BotVenueConfig."""
    with op.batch_alter_table("sistur_bot_intelligence") as batch_op:
        batch_op.add_column(sa.Column("identity_json", sa.Text(), nullable=True))

    with op.batch_alter_table("sistur_bot_venue_configs") as batch_op:
        batch_op.drop_column("tools_json")
        batch_op.drop_column("identity_json")
