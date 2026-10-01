# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint de Pagamentos — endpoints para processamento via Mercado Pago.

POST /api/v1/payments/process  — Proxy autenticado para criação de pagamento (Bearer token).
POST /api/v1/payments/webhook  — Webhook público para notificações do Mercado Pago.
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint("pagamentos", __name__)

from app.blueprints.pagamentos import routes  # noqa: E402
