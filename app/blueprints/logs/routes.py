# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Rotas de log de integração — recebe erros do frontend WordPress/JS.

POST /api/logs/payment-error  → log_payment_error()
"""

from __future__ import annotations

from flask import request, jsonify
from flask_cors import cross_origin

from app.blueprints.logs import bp
from app.services.integration_log_service import FONTES_VALIDAS, IntegrationLogService

# Conjunto fechado de origens aceitas para evitar poluição de dados
_VALID_SOURCES = FONTES_VALIDAS

# Limites de tamanho para evitar payload abusivo
_MAX_MESSAGE_LEN = 2000
_MAX_CODE_LEN = 100
_MAX_UA_LEN = 255
_MAX_NAME_LEN = 255


@bp.route("/payment-error", methods=["POST", "OPTIONS"])
@cross_origin(origins="*", methods=["POST", "OPTIONS"], allow_headers=["Content-Type"])
def log_payment_error():
    """Persiste um erro de integração de pagamento vindo do frontend WordPress ou do próprio Sistur.

    Endpoint público — sem autenticação, pois é chamado diretamente pelo browser
    do cliente (não há como incluir o Bearer token no JS sem expô-lo).
    O CORS do Flask já restringe as origens permitidas a cachoeiradogirassol.com.br.
    A rota é write-only e não expõe nenhum dado existente.
    Também pode ser chamada internamente pelo Sistur para registrar erros de
    pagamento e geração de QR Code (source=SISTUR_QR).

    Request body (JSON):
        error_source (str): Origem do erro. Um de: MP_BRICKS, WP_INTERNAL, SISTUR_SYNC, SISTUR_QR.
        error_message (str): Descrição legível do erro. Obrigatório.
        reservation_id (int, opcional): ID da reserva no Sistur (se disponível).
        customer_name (str, opcional): Nome do cliente da reserva que apresentou erro.
        error_code (str, opcional): Código de erro retornado pelo MP ou sistema.
        raw_data (dict, opcional): Objeto de erro bruto para diagnóstico.

    Returns:
        201: {"logged": true, "id": <int>} — log persistido com sucesso.
        400: {"erro": "<mensagem>"} — payload inválido.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"erro": "Payload JSON inválido ou ausente."}), 400

    # --- Validação de error_source ---
    error_source = str(data.get("error_source", "")).strip().upper()
    if error_source not in _VALID_SOURCES:
        return jsonify({
            "erro": f"error_source inválido. Valores aceitos: {', '.join(sorted(_VALID_SOURCES))}",
        }), 400

    # --- Validação de error_message ---
    error_message = str(data.get("error_message", "")).strip()
    if not error_message:
        return jsonify({"erro": "error_message é obrigatório."}), 400
    error_message = error_message[:_MAX_MESSAGE_LEN]

    # --- reservation_id (opcional, coerce para int) ---
    reservation_id: int | None = None
    raw_id = data.get("reservation_id")
    if raw_id is not None:
        try:
            reservation_id = int(raw_id)
        except (ValueError, TypeError):
            reservation_id = None

    # --- error_code (opcional) ---
    error_code: str | None = None
    raw_code = data.get("error_code")
    if raw_code is not None:
        error_code = str(raw_code).strip()[:_MAX_CODE_LEN] or None

    # --- raw_payload — aceita apenas dict para evitar tipos inesperados ---
    raw_payload = data.get("raw_data")
    if not isinstance(raw_payload, dict):
        raw_payload = None

    # --- customer_name (opcional) ---
    customer_name: str | None = None
    raw_name = data.get("customer_name")
    if raw_name is not None:
        customer_name = str(raw_name).strip()[:_MAX_NAME_LEN] or None

    # --- mp_status_detail (opcional) — aceita campo direto ou dentro de raw_data ---
    mp_status_detail: str | None = None
    raw_detail = data.get("mp_status_detail") or (
        raw_payload.get("status_detail") if isinstance(raw_payload, dict) else None
    )
    if raw_detail is not None:
        mp_status_detail = str(raw_detail).strip()[:100] or None

    log_id = IntegrationLogService.registrar(
        error_source=error_source,
        error_code=error_code,
        error_message=error_message,
        reserva_id=reservation_id,
        customer_name=customer_name,
        raw_payload=raw_payload,
        status_detail=mp_status_detail,
        user_agent=(request.headers.get("User-Agent", "")[:_MAX_UA_LEN] or None),
    )

    if log_id is None:
        return jsonify({"erro": "Falha ao persistir o log."}), 500

    return jsonify({"logged": True, "id": log_id}), 201


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
