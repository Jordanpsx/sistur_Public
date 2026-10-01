"""escala: replace turno with tipo + labels

Substitui a coluna única `turno` (MANHA/TARDE/NOITE/PLANTAO/FOLGA) por:
  - `tipo`  — estado base do dia: NORMAL (trabalha) ou FOLGA;
  - `labels` — rótulos opcionais de turno (JSON, multivalor) para dias NORMAL.

O backfill converte os dados existentes: turno FOLGA → tipo FOLGA; qualquer
outro turno → tipo NORMAL com aquele turno virando o único rótulo da lista.

Revision ID: 9e1cd1afe63d
Revises: 418e3488f25b
Create Date: 2026-06-25 12:19:25.204890

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9e1cd1afe63d'
down_revision = '418e3488f25b'
branch_labels = None
depends_on = None


def upgrade():
    # Novas colunas
    op.add_column(
        'sistur_escala_entradas',
        sa.Column('tipo', sa.Enum('NORMAL', 'FOLGA', native_enum=False),
                  server_default='NORMAL', nullable=False),
    )
    op.add_column('sistur_escala_entradas', sa.Column('labels', sa.JSON(), nullable=True))

    # Backfill a partir do antigo `turno`
    op.execute(
        """
        UPDATE sistur_escala_entradas
        SET tipo = CASE WHEN turno = 'FOLGA' THEN 'FOLGA' ELSE 'NORMAL' END,
            labels = CASE
                WHEN turno IN ('MANHA', 'TARDE', 'NOITE', 'PLANTAO')
                THEN json_build_array(turno)
                ELSE json_build_array()
            END
        """
    )

    op.drop_column('sistur_escala_entradas', 'turno')


def downgrade():
    op.add_column(
        'sistur_escala_entradas',
        sa.Column('turno', sa.VARCHAR(length=7), autoincrement=False, nullable=True),
    )

    # Reconstrói o antigo `turno` (best-effort): folga → FOLGA; senão o 1º rótulo,
    # ou MANHA como padrão quando não havia rótulo.
    op.execute(
        """
        UPDATE sistur_escala_entradas
        SET turno = CASE
            WHEN tipo = 'FOLGA' THEN 'FOLGA'
            WHEN labels IS NOT NULL AND json_array_length(labels) > 0 THEN labels->>0
            ELSE 'MANHA'
        END
        """
    )

    op.alter_column('sistur_escala_entradas', 'turno', nullable=False)
    op.drop_column('sistur_escala_entradas', 'labels')
    op.drop_column('sistur_escala_entradas', 'tipo')
