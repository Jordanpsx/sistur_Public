"""widen_bot_intelligence_category

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2026-04-01 14:00:00.000000

Mudanças:
  - Amplia sistur_bot_intelligence.category de VARCHAR(50) para VARCHAR(100)
    para acomodar slugs de categorias customizadas criadas dinamicamente.

Motivação:
  O campo era VARCHAR(50) originado da lista hardcoded de categorias.
  Com o modelo no-code, operadores podem criar categorias com nomes mais
  longos (ex: 'atendimento_vip', 'pacotes-hospedagem-eventos').
  VARCHAR(100) preserva compatibilidade total com dados existentes.
"""
from alembic import op
import sqlalchemy as sa


revision = 'n9o0p1q2r3s4'
down_revision = 'm8n9o0p1q2r3'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'sistur_bot_intelligence',
        'category',
        existing_type=sa.String(length=50),
        type_=sa.String(length=100),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        'sistur_bot_intelligence',
        'category',
        existing_type=sa.String(length=100),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
