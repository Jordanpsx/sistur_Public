"""Refactor reservas: add 3-tier inventory (resources), dynamic config, refactor items

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-03-09 00:00:00.000000

Changes:
  1. Create sistur_reserva_resources — physical inventory (stock pool)
  2. Create sistur_reserva_config — key-value JSON configuration store
  3. Refactor sistur_reserva_items: add category_id + resource_id FKs, remove stock_quantity
  4. Add back_populate relationships in ReservaCategory to ReservaItem
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1a2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Tabela sistur_reserva_resources — recurso físico (inventory) compartilhado
    op.create_table(
        'sistur_reserva_resources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('stock_quantity', sa.Integer(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['source_id'],
            ['sistur_reserva_sources.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_id', 'name', name='uq_reserva_resource_source_name'),
    )
    op.create_index('ix_sistur_reserva_resources_source_id', 'sistur_reserva_resources', ['source_id'])

    # Tabela sistur_reserva_config — configuração dinâmica (key-value JSON)
    op.create_table(
        'sistur_reserva_config',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('config_key', sa.String(length=100), nullable=False),
        sa.Column('config_value', sa.JSON(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('atualizado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['source_id'],
            ['sistur_reserva_sources.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_id', 'config_key', name='uq_reserva_config_source_key'),
    )
    op.create_index('ix_sistur_reserva_config_source_id', 'sistur_reserva_config', ['source_id'])

    # Refactor sistur_reserva_items
    # 1. Add new FKs (category_id, resource_id)
    op.add_column(
        'sistur_reserva_items',
        sa.Column('category_id', sa.Integer(), nullable=True),
    )
    op.add_column(
        'sistur_reserva_items',
        sa.Column('resource_id', sa.Integer(), nullable=True),
    )

    # 2. Create indexes on new FKs
    op.create_index('ix_sistur_reserva_items_category_id', 'sistur_reserva_items', ['category_id'])
    op.create_index('ix_sistur_reserva_items_resource_id', 'sistur_reserva_items', ['resource_id'])

    # 3. Add foreign key constraints
    op.create_foreign_key(
        'fk_sistur_reserva_items_category_id',
        'sistur_reserva_items',
        'sistur_reserva_categories',
        ['category_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_sistur_reserva_items_resource_id',
        'sistur_reserva_items',
        'sistur_reserva_resources',
        ['resource_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # 4. Add unique constraint (category_id, name)
    op.create_unique_constraint(
        'uq_reserva_item_category_name',
        'sistur_reserva_items',
        ['category_id', 'name'],
    )

    # 5. Drop stock_quantity column (moved to resources)
    op.drop_column('sistur_reserva_items', 'stock_quantity')


def downgrade():
    # Restore stock_quantity column to sistur_reserva_items
    op.add_column(
        'sistur_reserva_items',
        sa.Column('stock_quantity', sa.Integer(), nullable=True),
    )

    # Drop unique constraint (category_id, name)
    op.drop_constraint(
        'uq_reserva_item_category_name',
        'sistur_reserva_items',
        type_='unique',
    )

    # Drop foreign key constraints
    op.drop_constraint(
        'fk_sistur_reserva_items_resource_id',
        'sistur_reserva_items',
        type_='foreignkey',
    )
    op.drop_constraint(
        'fk_sistur_reserva_items_category_id',
        'sistur_reserva_items',
        type_='foreignkey',
    )

    # Drop indexes
    op.drop_index('ix_sistur_reserva_items_resource_id', table_name='sistur_reserva_items')
    op.drop_index('ix_sistur_reserva_items_category_id', table_name='sistur_reserva_items')

    # Drop columns
    op.drop_column('sistur_reserva_items', 'resource_id')
    op.drop_column('sistur_reserva_items', 'category_id')

    # Drop sistur_reserva_config table
    op.drop_index('ix_sistur_reserva_config_source_id', table_name='sistur_reserva_config')
    op.drop_table('sistur_reserva_config')

    # Drop sistur_reserva_resources table
    op.drop_index('ix_sistur_reserva_resources_source_id', table_name='sistur_reserva_resources')
    op.drop_table('sistur_reserva_resources')
