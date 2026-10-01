# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Mascara CPF já gravado nas trilhas de auditoria.

A partir de ``BaseService._snapshot`` os snapshots novos já nascem com o
documento mascarado. Esta migração trata o passivo: as linhas gravadas antes
disso, que guardavam o CPF íntegro em ``previous_state`` / ``new_state``.

O mascaramento reusa ``app.core.dados_sensiveis.mascarar_documento`` — a mesma
função do caminho de execução — para que histórico e futuro fiquem idênticos.

Revision ID: b7c31d0a94e2
Revises: 456f68a91a26
Create Date: 2026-09-03
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

from app.core.dados_sensiveis import CAMPOS_DOCUMENTO, mascarar_documento

revision = "b7c31d0a94e2"
down_revision = "456f68a91a26"
branch_labels = None
depends_on = None

_COLUNAS = ("previous_state", "new_state")
_LOTE = 500


def _mascarar_coluna(conn, coluna: str) -> int:
    """Reescreve uma coluna de estado, mascarando os campos de documento.

    Args:
        conn: Conexão ativa da migração.
        coluna: ``previous_state`` ou ``new_state``.

    Returns:
        Quantidade de linhas efetivamente alteradas.
    """
    campos = tuple(CAMPOS_DOCUMENTO)
    linhas = conn.execute(
        text(
            f"""
            SELECT id, {coluna} AS estado
              FROM sistur_audit_logs
             WHERE {coluna} IS NOT NULL
               AND json_typeof({coluna}) = 'object'
               AND EXISTS (
                     SELECT 1
                       FROM json_each_text({coluna}) AS e(k, v)
                      WHERE e.k = ANY(:campos)
                        AND v ~ '[0-9]'
                   )
             ORDER BY id
            """
        ),
        {"campos": list(campos)},
    ).fetchall()

    alteradas = 0
    pendentes: list[dict] = []
    for linha in linhas:
        estado = linha.estado
        if isinstance(estado, str):          # driver sem parse automático de json
            estado = json.loads(estado)
        if not isinstance(estado, dict):
            continue

        mudou = False
        for campo in CAMPOS_DOCUMENTO & estado.keys():
            novo = mascarar_documento(estado[campo])
            if novo != estado[campo]:
                estado[campo] = novo
                mudou = True
        if not mudou:
            continue

        pendentes.append({"id": linha.id, "estado": json.dumps(estado, ensure_ascii=False)})
        if len(pendentes) >= _LOTE:
            alteradas += _gravar(conn, coluna, pendentes)
            pendentes = []

    if pendentes:
        alteradas += _gravar(conn, coluna, pendentes)
    return alteradas


def _gravar(conn, coluna: str, lote: list[dict]) -> int:
    """Persiste um lote de estados já mascarados."""
    conn.execute(
        text(f"UPDATE sistur_audit_logs SET {coluna} = CAST(:estado AS json) WHERE id = :id"),
        lote,
    )
    return len(lote)


def upgrade() -> None:
    conn = op.get_bind()
    for coluna in _COLUNAS:
        total = _mascarar_coluna(conn, coluna)
        print(f"[mascarar-cpf-auditoria] {coluna}: {total} linhas reescritas")


def downgrade() -> None:
    """Sem volta.

    O mascaramento descarta os dígitos — não há de onde restaurá-los, e
    restaurá-los seria o oposto do objetivo. O downgrade é deliberadamente
    inócuo para que um rollback de esquema não falhe por causa desta revisão.
    """
    pass
