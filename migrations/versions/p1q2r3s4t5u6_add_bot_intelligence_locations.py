"""add_bot_intelligence_locations

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
Create Date: 2026-04-13 00:00:00.000000

Mudanças:
  - Cria tabela de associação sistur_bot_intelligence_locations (Many-to-Many).
  - Vincula BotIntelligence (categoria de contexto do bot) a zero ou mais
    ReservaSource (venues/inboxes), permitindo que um concierge multi-venue
    saiba quais locais cada contexto atende.
  - Cascade DELETE em ambas as FKs: deletar um contexto ou um venue remove
    automaticamente as associações sem deixar registros órfãos.
"""
from alembic import op
import sqlalchemy as sa


revision = 'p1q2r3s4t5u6'
down_revision = 'o0p1q2r3s4t5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sistur_bot_intelligence_locations',
        sa.Column('bot_intelligence_id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['bot_intelligence_id'],
            ['sistur_bot_intelligence.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['source_id'],
            ['sistur_reserva_sources.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('bot_intelligence_id', 'source_id'),
    )


def downgrade():
    op.drop_table('sistur_bot_intelligence_locations')
