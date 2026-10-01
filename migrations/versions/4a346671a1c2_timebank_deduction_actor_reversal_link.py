"""timebank deduction: actor, reversal link

Adiciona à `sistur_timebank_deductions`:
  - `criado_por_id` — quem lançou. Fica na própria linha, e não só no AuditLog,
    porque o extrato do colaborador mostra o responsável junto do lançamento.
  - `estorno_de_id` — auto-referência usada pelo contra-lançamento. A linha de
    estorno aponta para o lançamento que desfaz e carrega minutos/valor
    invertidos; nada é apagado.

Ambas nullable: as linhas existentes (nenhuma, hoje) e todo lançamento normal
ficam com NULL em `estorno_de_id`.

As FKs recebem nome explícito — sem isso o `downgrade()` gerado pelo Alembic sai
com `drop_constraint(None, ...)` e falha no PostgreSQL.

Revision ID: 4a346671a1c2
Revises: c48e2f716a03
Create Date: 2026-09-15 08:46:34.941869

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4a346671a1c2'
down_revision = 'c48e2f716a03'
branch_labels = None
depends_on = None

FK_CRIADO_POR = 'fk_timebank_deductions_criado_por_id'
FK_ESTORNO_DE = 'fk_timebank_deductions_estorno_de_id'


def upgrade():
    with op.batch_alter_table('sistur_timebank_deductions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('criado_por_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('estorno_de_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_sistur_timebank_deductions_criado_por_id'),
            ['criado_por_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_sistur_timebank_deductions_estorno_de_id'),
            ['estorno_de_id'],
            unique=False,
        )
        batch_op.create_foreign_key(
            FK_CRIADO_POR,
            'sistur_funcionarios',
            ['criado_por_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_foreign_key(
            FK_ESTORNO_DE,
            'sistur_timebank_deductions',
            ['estorno_de_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('sistur_timebank_deductions', schema=None) as batch_op:
        batch_op.drop_constraint(FK_ESTORNO_DE, type_='foreignkey')
        batch_op.drop_constraint(FK_CRIADO_POR, type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_sistur_timebank_deductions_estorno_de_id'))
        batch_op.drop_index(batch_op.f('ix_sistur_timebank_deductions_criado_por_id'))
        batch_op.drop_column('estorno_de_id')
        batch_op.drop_column('criado_por_id')
