"""merge display_name branch into main chain

Revision ID: 9573d811415c
Revises: 20a965b0ae9c, 5fe278eaecaf
Create Date: 2026-04-23 12:18:57.912039

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9573d811415c'
down_revision = '20a965b0ae9c'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
