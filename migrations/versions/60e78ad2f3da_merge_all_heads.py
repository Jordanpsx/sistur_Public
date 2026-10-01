"""merge all heads

Revision ID: 60e78ad2f3da
Revises: 1ed6bb9514f2, d6550a28fe62, k5l6m7n8o9p0
Create Date: 2026-03-30 12:47:22.055703

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '60e78ad2f3da'
down_revision = ('1ed6bb9514f2', 'd6550a28fe62', 'k5l6m7n8o9p0')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
