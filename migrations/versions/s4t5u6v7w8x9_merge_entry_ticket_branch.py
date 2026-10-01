"""Merge is_entry_ticket branch into main chain

Revision ID: s4t5u6v7w8x9
Revises: 24ee396fce11, r3s4t5u6v7w8
Create Date: 2026-05-11

Merge point: une o branch add_is_entry_ticket (r3s4t5u6v7w8) com o
head principal (24ee396fce11 — add informativo table).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 's4t5u6v7w8x9'
down_revision = ('24ee396fce11', 'r3s4t5u6v7w8')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
