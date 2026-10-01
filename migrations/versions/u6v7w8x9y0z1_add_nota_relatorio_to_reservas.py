"""Add nota_relatorio to Reserva

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-05-11

nota_relatorio — texto livre escrito pelo operador na tela de detalhe
                 da reserva, exibido nos cards do relatório diário.
                 Nullable: reservas sem nota simplesmente não exibem nada.
"""
from alembic import op
import sqlalchemy as sa


revision = 'u6v7w8x9y0z1'
down_revision = 't5u6v7w8x9y0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_reservas', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('nota_relatorio', sa.Text(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table('sistur_reservas', schema=None) as batch_op:
        batch_op.drop_column('nota_relatorio')
