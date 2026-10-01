# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
SystemSetting — configurações globais do sistema armazenadas no banco de dados.

Modelo key-value com tipagem explícita (bool, string, int).
Chaves conhecidas são documentadas como constantes em ConfiguracaoService.

Auditoria obrigatória: toda mutação é auditada via ConfiguracaoService.set().
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from app.extensions import db


class SettingType(enum.Enum):
    """Tipo do valor armazenado na configuração."""

    bool = "bool"
    string = "string"
    int = "int"


class SystemSetting(db.Model):
    """
    Configuração global do sistema no formato chave-valor.

    Cada linha representa uma configuração única identificada por 'chave'.
    O campo 'valor' sempre armazena texto; a conversão para o tipo nativo
    é feita pelo ConfiguracaoService.get() com base no campo 'tipo'.

    Exemplos de chaves:
        modulo.ponto        → bool  → habilita/desabilita o módulo Ponto
        modulo.estoque      → bool  → habilita/desabilita o módulo Estoque
        branding.empresa_nome → string → sobrescreve COMPANY_NAME do env
    """

    __tablename__ = "sistur_system_settings"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    chave = db.Column(
        db.String(100),
        unique=True,
        nullable=False,
        index=True,
    )
    valor = db.Column(db.Text, nullable=True)
    tipo = db.Column(
        db.Enum(SettingType, native_enum=False),
        nullable=False,
        default=SettingType.string,
    )
    descricao = db.Column(db.String(255), nullable=True)

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
        return (
            f"<SystemSetting chave={self.chave!r} valor={self.valor!r}"
            f" tipo={self.tipo.value}>"
        )
