# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Modelos do cofre interno de segredos do Sistur.

Segredos são credenciais sensíveis como API keys, access tokens e webhook
secrets. O valor bruto nunca deve ser salvo em texto puro nem exibido pela UI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db


class Secret(db.Model):
    """Segredo criptografado usado por integrações sensíveis.

    Attributes:
        key: Identificador estável do segredo (ex.: ``mercadopago.access_token``).
        provider: Integração proprietária do segredo (ex.: ``mercadopago``).
        label: Nome legível opcional para UI.
        value_encrypted: Valor criptografado com a chave mestra do ambiente.
        last4: Últimos 4 caracteres do valor bruto para exibição mascarada.
        ativo: False desativa o segredo sem remover histórico/auditoria.
        updated_by_id: Funcionário que fez a última alteração.
    """

    __tablename__ = "sistur_secrets"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    provider = db.Column(db.String(50), nullable=True, index=True)
    label = db.Column(db.String(120), nullable=True)
    value_encrypted = db.Column(db.Text, nullable=False)
    last4 = db.Column(db.String(8), nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True, index=True)
    updated_by_id = db.Column(db.Integer, nullable=True, index=True)

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Representação segura para logs e depuração."""
        return f"<Secret key={self.key!r} provider={self.provider!r} ativo={self.ativo}>"
