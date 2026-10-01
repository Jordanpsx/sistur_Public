"""merge_bot_and_logs_branch

Revision ID: m8n9o0p1q2r3
Revises: 60e78ad2f3da, l7m8n9o0p1q2
Create Date: 2026-04-01 12:30:00.000000

Contexto:
  Dois ramos divergiram a partir de j4k5l6m7n8o9:
    - Ramo A: k5l6m7n8o9p0 → 60e78ad2f3da (merge anterior)
    - Ramo B: bc3bd95200ab → k6l7m8n9o0p1 → l7m8n9o0p1q2 (integration_logs + bot_intelligence)

  Este merge une os dois ramos sem nenhuma mudança de schema.
  Após aplicar, o head único será m8n9o0p1q2r3.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm8n9o0p1q2r3'
down_revision = ('60e78ad2f3da', 'l7m8n9o0p1q2')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
