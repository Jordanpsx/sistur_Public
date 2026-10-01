# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint PDV — API do Ponto de Venda de balcão.

Registrado em /api/v1/pdv. Ver docs/pdv/README.md.
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint("pdv", __name__)

from app.blueprints.pdv import routes  # noqa: E402, F401
