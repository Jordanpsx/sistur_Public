"""add_customer_name_to_integration_logs

Revision ID: k6l7m8n9o0p1
Revises: j4k5l6m7n8o9
Create Date: 2026-03-30 13:00:00.000000

Mudanças:
  - Adiciona coluna customer_name (VARCHAR 255, nullable) à tabela
    sistur_integration_logs para registrar o nome do cliente da reserva
    que apresentou erro de pagamento ou geração de QR Code.

  Motivação:
    Os logs de erro de integração (IntegrationLog) registravam apenas o
    reserva_id, exigindo um JOIN com sistur_reservas para identificar o
    cliente afetado. Como a tabela não possui FK intencional (para aceitar
    logs mesmo com reserva inválida), o JOIN pode falhar se a reserva foi
    deletada. Armazenar customer_name na própria linha garante rastreabilidade
    completa sem dependências externas.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'k6l7m8n9o0p1'
down_revision = 'j4k5l6m7n8o9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'sistur_integration_logs',
        sa.Column('customer_name', sa.String(length=255), nullable=True),
    )


def downgrade():
    op.drop_column('sistur_integration_logs', 'customer_name')
