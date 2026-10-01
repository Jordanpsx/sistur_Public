"""Add emoji_label to ReservaItem and delivery fields to ReservaItemLink

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-05-11

emoji_label        — emoji associado ao item para o relatório simplificado WhatsApp
                     (ex: "🍖", "💧"). Nullable: items sem emoji usam nome como fallback.

entregue_em        — timestamp de quando o operador confirmou a entrega do item.
                     NULL = pendente. Preenchido = entregue.

entregue_por_id    — FK para sistur_funcionarios: quem confirmou a entrega.
                     SET NULL ao deletar o funcionário.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't5u6v7w8x9y0'
down_revision = 's4t5u6v7w8x9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sistur_reserva_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('emoji_label', sa.String(length=10), nullable=True)
        )

    with op.batch_alter_table('sistur_reserva_item_links', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('entregue_em', sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'entregue_por_id',
                sa.Integer(),
                sa.ForeignKey('sistur_funcionarios.id', ondelete='SET NULL'),
                nullable=True,
            )
        )
        batch_op.create_index(
            'ix_sistur_reserva_item_links_entregue_por_id',
            ['entregue_por_id'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('sistur_reserva_item_links', schema=None) as batch_op:
        batch_op.drop_index('ix_sistur_reserva_item_links_entregue_por_id')
        batch_op.drop_column('entregue_por_id')
        batch_op.drop_column('entregue_em')

    with op.batch_alter_table('sistur_reserva_items', schema=None) as batch_op:
        batch_op.drop_column('emoji_label')
