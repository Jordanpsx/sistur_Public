# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
wordpress_sync — Dispatcher assíncrono para sincronização de Avisos com o WordPress via REST API.

Arquitetura
-----------
Idêntica ao n8n_sync: cada operação dispara um ``threading.Thread`` daemon que
envia uma requisição HTTP ao endpoint customizado do plugin WordPress sem bloquear
o thread principal do Flask.
Todos os erros são capturados silenciosamente e logados — uma falha na sincronização
WordPress jamais deve interromper operações do Sistur.

Endpoint WordPress
------------------
POST /wp-json/rcg/v1/avisos/sync
Header: X-Sistur-Key: <chave-secreta>
Body: {"slug": str, "title": str, "content": str}

Mapeamento de slugs (Sistur → WordPress rcg_avisos option):
  termo-de-uso-camping  → rcg_avisos['termo_camping']
  termo-de-uso-day-use  → rcg_avisos['termo_dayuse']

Slugs não mapeados são enviados ao WordPress e recebem resposta {"success": false}
sem causar erro — o operador pode adicionar novos mapeamentos no plugin conforme necessário.

Variáveis de ambiente
---------------------
WP_API_URL         — Base URL do WordPress, sem barra final.
                     Exemplo: http://cachoeira-wordpress-test (Docker interno) ou
                              https://cachoeiradogirassol.com.br (produção).
                     Se não configurada, todos os pushes são ignorados silenciosamente.
WP_SISTUR_SYNC_KEY — Chave secreta compartilhada enviada no header X-Sistur-Key.
                     Deve corresponder à opção rcg_sistur_sync_key no WordPress.

Configuração WordPress
----------------------
No painel WordPress, defina a opção rcg_sistur_sync_key via WP-CLI ou diretamente
na tabela wp_options:
  wp option update rcg_sistur_sync_key "sua-chave-secreta-aqui"
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_WP_API_URL: str = os.environ.get("WP_API_URL", "").rstrip("/")
_WP_SISTUR_SYNC_KEY: str = os.environ.get("WP_SISTUR_SYNC_KEY", "")
_TIMEOUT: int = 10


def _push_worker(slug: str, title: str, content: str) -> None:
    """Envia slug, título e conteúdo ao endpoint rcg/v1/avisos/sync do WordPress.

    Executa em thread daemon — nunca bloqueia o thread principal do Flask.
    Todos os erros são logados silenciosamente; jamais propagam exceções.

    Args:
        slug: Slug do Informativo (ex: 'termo-de-uso-camping').
        title: Título a gravar no aviso WordPress.
        content: Conteúdo HTML a gravar no aviso WordPress.
    """
    if not _WP_API_URL:
        logger.debug("WP_API_URL não configurado — push WordPress ignorado.")
        return

    if not _WP_SISTUR_SYNC_KEY:
        logger.warning("WP_SISTUR_SYNC_KEY não configurado — push WordPress ignorado.")
        return

    try:
        import requests  # noqa: PLC0415

        url = f"{_WP_API_URL}/wp-json/rcg/v1/avisos/sync"
        resp = requests.post(
            url,
            json={"slug": slug, "title": title, "content": content},
            headers={"X-Sistur-Key": _WP_SISTUR_SYNC_KEY},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()

        data: dict[str, Any] = resp.json()
        if data.get("success"):
            logger.info(
                "wordpress_sync: aviso slug='%s' sincronizado → chave='%s'",
                slug,
                data.get("chave"),
            )
        else:
            logger.info(
                "wordpress_sync: slug='%s' não mapeado no WordPress (ignorado).",
                slug,
            )

    except Exception:
        logger.warning(
            "wordpress_sync falhou (silencioso): slug='%s' url='%s'",
            slug,
            _WP_API_URL,
            exc_info=True,
        )


def push_page(slug: str, title: str, content: str) -> None:
    """Sincroniza um Informativo com o aviso correspondente no WordPress.

    Executa em thread daemon para não bloquear o request Flask.
    Se WP_API_URL ou WP_SISTUR_SYNC_KEY não estiverem configurados, é no-op silencioso.

    Args:
        slug: Slug do Informativo e chave de roteamento no WordPress.
        title: Título a gravar no aviso WordPress.
        content: Conteúdo HTML a gravar no aviso WordPress.
    """
    if not _WP_API_URL or not _WP_SISTUR_SYNC_KEY:
        return

    t = threading.Thread(
        target=_push_worker,
        args=(slug, title, content),
        daemon=True,
        name=f"wp-sync-{slug}",
    )
    t.start()
