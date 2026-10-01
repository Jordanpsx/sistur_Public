"""Separação dos calendários: intervalo + origem no GlobalEvent, permissão reservas.configurar

Revision ID: 54fb7d898c6d
Revises: 8050557b5039
Create Date: 2026-08-12 00:00:00.000000

Três mudanças:

1. `sistur_global_events.data_fim` — permite um evento cobrir um período
   (alta temporada de julho vira 1 linha, não 31).
2. `sistur_global_events.origem` — 'global' ou 'reservas'. Registra quem
   cadastrou o evento e é o que autoriza o módulo de Reservas a editá-lo.
3. Backfill de `reservas.configurar` para toda role que já tem `reservas.edit`
   globalmente — sem isso, quem administra o módulo hoje perderia o acesso às
   telas de configuração no deploy.

Nota: `sistur_global_events` nunca teve migration própria — a tabela nasceu de
`db.create_all()` (app/cli.py). Por isso o upgrade cria a tabela inteira quando
ela não existe, em vez de assumir que está lá.
"""

from alembic import op
import sqlalchemy as sa

revision = "54fb7d898c6d"
down_revision = "8050557b5039"
branch_labels = None
depends_on = None

_TABELA = "sistur_global_events"

_TIPOS_EVENTO = ("NATIONAL_HOLIDAY", "LOCAL_HOLIDAY", "MAINTENANCE", "HIGH_SEASON")


def _tabela_existe(nome: str) -> bool:
    return nome in sa.inspect(op.get_bind()).get_table_names()


def _colunas(nome: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(nome)}


def upgrade():
    # ── 1 + 2. Colunas novas em sistur_global_events ──────────────────────
    if not _tabela_existe(_TABELA):
        op.create_table(
            _TABELA,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("titulo", sa.String(length=150), nullable=False),
            sa.Column("data_evento", sa.Date(), nullable=False),
            sa.Column("data_fim", sa.Date(), nullable=True),
            sa.Column(
                "tipo",
                sa.Enum(*_TIPOS_EVENTO, native_enum=False, name="tipoevento"),
                nullable=False,
            ),
            sa.Column("afeta_precificacao", sa.Boolean(), nullable=False),
            sa.Column("afeta_folha", sa.Boolean(), nullable=False),
            sa.Column("recorrente_anual", sa.Boolean(), nullable=False),
            sa.Column(
                "origem",
                sa.String(length=20),
                nullable=False,
                server_default="global",
            ),
            sa.Column("criado_em", sa.DateTime(), nullable=False),
            sa.Column("atualizado_em", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_sistur_global_events_data_evento"),
            _TABELA,
            ["data_evento"],
        )
    else:
        existentes = _colunas(_TABELA)
        if "data_fim" not in existentes:
            op.add_column(_TABELA, sa.Column("data_fim", sa.Date(), nullable=True))
        if "origem" not in existentes:
            op.add_column(
                _TABELA,
                sa.Column(
                    "origem",
                    sa.String(length=20),
                    nullable=False,
                    server_default="global",
                ),
            )

    # ── 3. Backfill RBAC: reservas.edit (global) ⇒ reservas.configurar ────
    # Evita lockout das telas /reservas/admin/config/*, que trocaram de
    # `reservas.edit` para `reservas.configurar`. Só permissões globais
    # (source_id IS NULL) são replicadas: has_permission exige escopo global
    # para qualquer ação que não seja `view`.
    op.execute(
        sa.text(
            """
            INSERT INTO sistur_role_permissions
                   (role_id, modulo, acao, source_id, criado_em)
            SELECT rp.role_id, 'reservas', 'configurar', NULL, CURRENT_TIMESTAMP
              FROM sistur_role_permissions rp
             WHERE rp.modulo = 'reservas'
               AND rp.acao = 'edit'
               AND rp.source_id IS NULL
               AND NOT EXISTS (
                     SELECT 1 FROM sistur_role_permissions ex
                      WHERE ex.role_id = rp.role_id
                        AND ex.modulo = 'reservas'
                        AND ex.acao = 'configurar'
                        AND ex.source_id IS NULL
                   )
            """
        )
    )


def downgrade():
    op.execute(
        sa.text(
            """
            DELETE FROM sistur_role_permissions
             WHERE modulo = 'reservas' AND acao = 'configurar'
            """
        )
    )

    if _tabela_existe(_TABELA):
        existentes = _colunas(_TABELA)
        if "origem" in existentes:
            op.drop_column(_TABELA, "origem")
        if "data_fim" in existentes:
            op.drop_column(_TABELA, "data_fim")
