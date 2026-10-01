"""add_bot_intelligence_table

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-04-01 12:00:00.000000

Mudanças:
  - Cria tabela sistur_bot_intelligence para armazenar contexto dinâmico
    do AI Bot (Gemini 1.5 Flash via n8n).

  Motivação:
    O módulo Bot Intelligence precisa de um repositório persistente para
    a identidade e as regras de negócio de cada categoria de conversa
    ('reserva', 'gastronomia', 'vinhedo', 'geral').  Esses dados são
    servidos pelo endpoint GET /api/v1/bot/context?category=<cat> e
    são editáveis pelo super_admin via /admin/configuracoes/bot.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'l7m8n9o0p1q2'
down_revision = 'k6l7m8n9o0p1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sistur_bot_intelligence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'category',
            sa.String(length=50),
            nullable=False,
            comment="Identificador único da categoria: 'reserva', 'gastronomia', 'vinhedo', 'geral'",
        ),
        sa.Column(
            'identity_json',
            sa.Text(),
            nullable=True,
            comment="JSON com personalidade do bot para esta categoria (nome, tom, instruções de sistema)",
        ),
        sa.Column(
            'business_rules_text',
            sa.Text(),
            nullable=True,
            comment="Regras de negócio em texto livre (horários, restrições, políticas)",
        ),
        sa.Column(
            'last_updated',
            sa.DateTime(),
            nullable=False,
            comment="Timestamp UTC da última atualização do registro",
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category', name='uq_bot_intelligence_category'),
    )
    op.create_index(
        'ix_sistur_bot_intelligence_category',
        'sistur_bot_intelligence',
        ['category'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        'ix_sistur_bot_intelligence_category',
        table_name='sistur_bot_intelligence',
    )
    op.drop_table('sistur_bot_intelligence')
