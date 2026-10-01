# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint de API pública — endpoints acessíveis externamente sem autenticação tradicional.

Destinado a integrações headless, como o catálogo de reservas e webhooks.
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint("api_public", __name__)

from app.blueprints.api_public import routes  # noqa: E402
