"""seed PARTIALLY_PAID into sistur_reserva_status_config

Insere a linha PARTIALLY_PAID na tabela de configuração dinâmica de status,
que foi omitida do banco de dados de produção porque o seed via after_create
só roda na criação da tabela — não em instalações que já existiam.

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-03-30
"""

revision = "k5l6m7n8o9p0"
down_revision = "j4k5l6m7n8o9"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    """Insere PARTIALLY_PAID como status reservado do sistema, se ainda não existir."""
    op.execute(
        """
        INSERT INTO sistur_reserva_status_config
            (internal_name, label, color_hex, blocks_resource, show_on_dashboard, is_system_reserved)
        SELECT 'PARTIALLY_PAID', 'Parcialmente Pago', '#3B82F6', TRUE, TRUE, TRUE
        WHERE NOT EXISTS (
            SELECT 1 FROM sistur_reserva_status_config
            WHERE internal_name = 'PARTIALLY_PAID'
        )
        """
    )


def downgrade():
    """Remove a linha PARTIALLY_PAID (somente se não houver reservas usando o status)."""
    op.execute(
        """
        DELETE FROM sistur_reserva_status_config
        WHERE internal_name = 'PARTIALLY_PAID'
          AND is_system_reserved = TRUE
        """
    )
