# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
InformativoService — CRUD de snippets de informação com sincronização WordPress.

Design constraints (Arquitetura em camadas + compatibilidade com Cython):
- SEM imports Flask (sem request, session, current_app, g).
- Recebe parâmetros explícitos; nunca acessa o contexto HTTP.
- Chama AuditService em toda mutação (Auditoria obrigatória).

Fluxo de sincronização pós-commit
----------------------------------
Após cada mutação bem-sucedida um sidecar assíncrono é disparado em thread
daemon — nunca bloqueia o request principal:

wordpress_sync.push_page(slug, title, content)  [somente se sync_wordpress=True]
   → atualiza a página WordPress correspondente via REST API.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.core.audit import AuditService
from app.extensions import db
from app.models.informativo import Informativo
from app.services.base import BaseService

logger = logging.getLogger(__name__)

# Slug válido: letras minúsculas, dígitos, hífen, underscore — máx. 100 chars.
_SLUG_RE = re.compile(r'^[a-z0-9_-]{1,100}$')


class InformativoService(BaseService):
    """Serviço de gerenciamento de Informativos (snippets de conteúdo centralizado).

    Cada Informativo pode ser sincronizado automaticamente com uma página
    WordPress (via REST API). Toda mutação é auditada e dispara o push
    assíncrono ao WordPress.
    """

    # ------------------------------------------------------------------
    # LEITURA
    # ------------------------------------------------------------------

    @staticmethod
    def listar_todos() -> list[Informativo]:
        """Retorna todos os Informativos ordenados por título.

        Returns:
            Lista de Informativo ordenada alfabeticamente pelo título.
        """
        return (
            db.session.query(Informativo)
            .order_by(Informativo.title)
            .all()
        )

    @staticmethod
    def obter(informativo_id: int) -> Informativo:
        """Retorna um Informativo pelo ID.

        Args:
            informativo_id: Chave primária do Informativo.

        Returns:
            Instância de Informativo.

        Raises:
            ValueError: Se não existir Informativo com este ID.
        """
        registro = db.session.get(Informativo, informativo_id)
        if registro is None:
            raise ValueError(f"Informativo id={informativo_id} não encontrado.")
        return registro

    # ------------------------------------------------------------------
    # ESCRITA — Criar
    # ------------------------------------------------------------------

    @staticmethod
    def criar(
        title: str,
        slug: str,
        content: str | None = None,
        *,
        sync_wordpress: bool = False,
        ator_id: int | None = None,
    ) -> Informativo:
        """Cria um novo Informativo e dispara sincronização assíncrona.

        Args:
            title: Título legível do snippet (ex: "Termos de Uso").
            slug: Identificador único em slug-case (ex: "termos-de-uso").
                Deve conter apenas letras minúsculas, dígitos, hífen ou underscore.
            content: Corpo do conteúdo em texto livre / HTML. Pode ser None.
            sync_wordpress: Se True, sincroniza automaticamente com o WordPress.
            ator_id: ID do funcionário que realizou a ação (para auditoria).

        Returns:
            Instância de Informativo recém-criada e persistida.

        Raises:
            ValueError: Se o slug for inválido.
            ValueError: Se já existir um Informativo com este slug.
            ValueError: Se o título estiver vazio.
        """
        title = title.strip()
        slug = slug.strip().lower()

        BaseService._require(title, "title")

        if not _SLUG_RE.match(slug):
            raise ValueError(
                "Slug inválido. Use apenas letras minúsculas, dígitos, hífen ou "
                "underscore (máx. 100 caracteres)."
            )

        existente = (
            db.session.query(Informativo)
            .filter_by(slug=slug)
            .first()
        )
        if existente is not None:
            raise ValueError(f"Já existe um Informativo com o slug '{slug}'.")

        registro = Informativo(
            title=title,
            slug=slug,
            content=content or None,
            sync_wordpress=sync_wordpress,
            updated_at=datetime.now(timezone.utc),
        )
        db.session.add(registro)
        db.session.flush()

        new_state = BaseService._snapshot(
            registro, ["id", "title", "slug", "sync_wordpress", "sync_bot", "updated_at"]
        )
        AuditService.log_create("informativo", registro.id, new_state, actor_id=ator_id)

        db.session.commit()

        InformativoService._disparar_sidecars("create", registro)

        return registro

    # ------------------------------------------------------------------
    # ESCRITA — Atualizar
    # ------------------------------------------------------------------

    @staticmethod
    def atualizar(
        informativo_id: int,
        title: str,
        content: str | None = None,
        *,
        sync_wordpress: bool = False,
        ator_id: int | None = None,
    ) -> Informativo:
        """Atualiza um Informativo existente e dispara sincronização assíncrona.

        Args:
            informativo_id: Chave primária do Informativo a atualizar.
            title: Novo título.
            content: Novo conteúdo. None limpa o campo.
            sync_wordpress: Novo valor do flag de sincronização WordPress.
            ator_id: ID do funcionário que realizou a ação (para auditoria).

        Returns:
            Instância de Informativo atualizada e persistida.

        Raises:
            ValueError: Se o Informativo não for encontrado.
            ValueError: Se o título estiver vazio.
        """
        title = title.strip()
        BaseService._require(title, "title")

        registro = db.session.get(Informativo, informativo_id)
        if registro is None:
            raise ValueError(f"Informativo id={informativo_id} não encontrado.")

        previous_state = BaseService._snapshot(
            registro, ["id", "title", "slug", "sync_wordpress", "sync_bot", "updated_at"]
        )

        registro.title = title
        registro.content = content or None
        registro.sync_wordpress = sync_wordpress
        registro.updated_at = datetime.now(timezone.utc)

        new_state = BaseService._snapshot(
            registro, ["id", "title", "slug", "sync_wordpress", "sync_bot", "updated_at"]
        )
        AuditService.log_update(
            "informativo", registro.id, previous_state, new_state, actor_id=ator_id
        )

        db.session.commit()

        InformativoService._disparar_sidecars("update", registro)

        return registro

    # ------------------------------------------------------------------
    # ESCRITA — Deletar
    # ------------------------------------------------------------------

    @staticmethod
    def deletar(informativo_id: int, *, ator_id: int | None = None) -> None:
        """Remove permanentemente um Informativo.

        Args:
            informativo_id: Chave primária do Informativo a remover.
            ator_id: ID do funcionário que realizou a ação (para auditoria).

        Raises:
            ValueError: Se o Informativo não for encontrado.
        """
        registro = db.session.get(Informativo, informativo_id)
        if registro is None:
            raise ValueError(f"Informativo id={informativo_id} não encontrado.")

        snapshot = BaseService._snapshot(
            registro, ["id", "title", "slug", "sync_wordpress", "sync_bot", "updated_at"]
        )
        entity_id = registro.id

        db.session.delete(registro)
        db.session.commit()

        AuditService.log_delete("informativo", entity_id, snapshot, actor_id=ator_id)

    # ------------------------------------------------------------------
    # Sidecar interno — pings assíncronos pós-commit
    # ------------------------------------------------------------------

    @staticmethod
    def _disparar_sidecars(action: str, registro: Informativo) -> None:
        """Dispara o push assíncrono ao WordPress após cada mutação.

        Executado sempre após db.session.commit() — nunca antes.
        Todos os erros são capturados silenciosamente: falha nos sidecars
        jamais deve afetar o resultado da operação principal.

        Args:
            action: Operação realizada: "create" ou "update".
            registro: Instância de Informativo já persistida.
        """
        # WordPress — atualiza página somente se o flag estiver ativo
        if registro.sync_wordpress and registro.content is not None:
            try:
                from app.services.wordpress_sync import push_page
                push_page(registro.slug, registro.title, registro.content)
            except Exception:
                logger.warning(
                    "informativo_service: wp push falhou (silencioso) para slug='%s'.",
                    registro.slug,
                    exc_info=True,
                )
