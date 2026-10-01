# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Modelo Informativo — snippets de informação gerenciados centralmente.

Cada registro representa um bloco de conteúdo (Termos de Uso, FAQ, Regras)
que pode ser sincronizado automaticamente com o WordPress e/ou injetado no
contexto dinâmico do AI Bot.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Informativo(db.Model):
    """Snippet de informação gerenciado centralmente pelo operador.

    Permite publicar conteúdo (Termos de Uso, FAQ, Regras) em dois destinos
    simultâneos: WordPress (via REST API) e o contexto dinâmico do AI Bot.

    Attributes:
        id: Chave primária.
        title: Título legível do snippet (ex: "Termos de Uso").
        slug: Identificador único em formato slug (ex: "termos-de-uso").
            Usado para localizar a página correspondente no WordPress.
        content: Corpo do conteúdo em texto livre / HTML.
        sync_wordpress: Quando True, o conteúdo é enviado automaticamente
            para a página WordPress com o mesmo slug a cada atualização.
        sync_bot: Quando True, o snippet é injetado no bloco de contexto
            dinâmico do AI Bot (endpoint /api/v1/bot/context).
        updated_at: Timestamp UTC da última atualização.
    """

    __tablename__ = "sistur_informativos"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_informativos_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Título legível do snippet (ex: Termos de Uso, FAQ, Regras do Parque)",
    )

    slug: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Slug único para matching com página WordPress e identificação interna",
    )

    content: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Conteúdo em texto livre / HTML; enviado ao WordPress e/ou injetado no Bot",
    )

    sync_wordpress: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Se True, atualiza automaticamente a página WordPress com o mesmo slug",
    )

    sync_bot: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Se True, injeta o conteúdo no bloco de contexto dinâmico do AI Bot",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="Timestamp UTC da última atualização",
    )

    def __repr__(self) -> str:
        return f"<Informativo slug={self.slug!r} wp={self.sync_wordpress} bot={self.sync_bot}>"
