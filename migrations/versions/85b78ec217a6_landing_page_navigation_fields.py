"""landing page navigation fields

Revision ID: 85b78ec217a6
Revises: 54fb7d898c6d
Create Date: 2026-08-14

ATENÇÃO — o autogenerate propôs uma migration DESTRUTIVA e foi descartado.

Ele detectou como "removidos" todos os objetos criados por DDL manual em
b937ac8c5a82, porque nenhum deles estava declarado no modelo SQLAlchemy:

    ck_landing_status · ck_landing_slug_shape · ck_landing_content_shape
    ck_landing_no_markup · ck_outbox_tags_nonempty
    uq_landing_slug_locale_live · ix_landing_published · ix_landing_content_gin
    ix_outbox_pending · ix_outbox_stuck

Aplicar aquilo teria removido a rejeição de markup, a validação de slug e a
unicidade parcial — silenciosamente, porque nada quebra na hora.

Esta revisão contém APENAS as duas colunas novas. Para que o problema não se
repita, os mesmos objetos passaram a ser declarados em
``LandingPage.__table_args__`` / ``RevalidationOutbox.__table_args__``; um
``flask db migrate`` seguinte deve reportar "No changes in schema detected".
"""
from alembic import op
import sqlalchemy as sa

revision = '85b78ec217a6'
down_revision = '54fb7d898c6d'
branch_labels = None
depends_on = None


def upgrade():
    # server_default é obrigatório: a coluna é NOT NULL e a tabela já tem linhas.
    op.add_column(
        "sistur_landing_pages",
        sa.Column("nav_label", sa.Text(), nullable=True),
    )
    op.add_column(
        "sistur_landing_pages",
        sa.Column("nav_order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("sistur_landing_pages", "nav_order")
    op.drop_column("sistur_landing_pages", "nav_label")
