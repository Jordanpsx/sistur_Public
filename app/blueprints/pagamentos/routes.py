# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Rotas de Pagamentos — Mercado Pago.

POST /api/v1/payments/process  — Recebe dados do Bricks (via proxy WordPress) e processa pagamento.
POST /api/v1/payments/webhook  — Recebe notificações assíncronas do Mercado Pago.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from app.blueprints.api_internal.routes import require_internal_token
from app.blueprints.pagamentos import bp
from app.services.integration_log_service import IntegrationLogService

logger = logging.getLogger(__name__)

# Throttle do log de "public key ausente": o WordPress consulta /config a cada 5 min
# por site. Sem throttle isso inunda /admin/logs com centenas de linhas idênticas.
_CONFIG_LOG_THROTTLE_SEG = 3600
_ultimo_log_public_key: float = 0.0


# ---------------------------------------------------------------------------
# Helpers internos de logging de erros
# ---------------------------------------------------------------------------


def _obter_customer_name(reserva_id) -> str | None:
    """Busca o nome do cliente da reserva de forma segura, sem propagar exceções.

    Args:
        reserva_id: ID da reserva (int ou string). None retorna None.

    Returns:
        Nome do cliente ou None se não encontrado.
    """
    return IntegrationLogService.buscar_customer_name(reserva_id)


def _registrar_log_erro(
    reserva_id,
    customer_name: str | None,
    error_source: str,
    error_code: str,
    error_message: str,
    raw_payload: dict | None = None,
    status_detail: str | None = None,
) -> None:
    """Persiste um IntegrationLog de erro de pagamento/QR code no banco de dados.

    Wrapper fino sobre IntegrationLogService — mantido porque os handlers deste
    blueprint já usam esta assinatura posicional. Falhas são silenciadas para não
    mascarar o erro original que disparou o log.

    Args:
        reserva_id:    ID da reserva afetada (pode ser None).
        customer_name: Nome do cliente da reserva para diagnóstico sem JOIN.
        error_source:  Origem do erro (SISTUR_QR, MP_BRICKS, etc.).
        error_code:    Código curto identificador do tipo de erro.
        error_message: Descrição legível do problema ocorrido.
        raw_payload:   Dict com dados brutos para diagnóstico (opcional).
        status_detail: status_detail retornado pelo MP (ex: cc_rejected_insufficient_amount).
    """
    IntegrationLogService.registrar(
        error_source=error_source,
        error_code=error_code,
        error_message=error_message,
        reserva_id=reserva_id,
        customer_name=customer_name,
        raw_payload=raw_payload,
        status_detail=status_detail,
    )



@bp.route("/process", methods=["POST"])
@require_internal_token
def process_payment():
    """Processa pagamento via Mercado Pago.

    Autenticado com Bearer token (QR_SERVICE_TOKEN) — mesmo mecanismo dos endpoints
    internos do Sistur. O WordPress envia este token ao fazer o proxy da requisição.

    JSON body:
        reserva_id (int): ID da Reserva no Sistur.
        transaction_amount (float): Valor total do pagamento.
        payment_method_id (str): Ex: "pix", "visa", "master".
        payer (dict): Dados do pagador:
            - email (str)
            - first_name (str)
            - last_name (str)
            - identification (dict): { type: "CPF", number: str }
        token (str, somente cartão): Token gerado pelo MP Bricks.
        installments (int, somente cartão): Número de parcelas.
        issuer_id (str, opcional): ID da bandeira emissora.

    Returns:
        200: Pagamento aprovado — { payment_id, status, status_detail, payment_method, message }
        200: Pagamento pendente (PIX) — inclui adicionalmente { qr_code, qr_code_base64, ticket_url }
        400: Dados inválidos ou Reserva não encontrada.
        500: Erro interno ou MP_ACCESS_TOKEN não configurado.
    """
    from app.services.payment_service import MercadoPagoService

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"erro": "Corpo da requisição inválido ou ausente."}), 400

    reserva_id = data.get("reserva_id")
    if not reserva_id:
        return jsonify({"erro": "Campo 'reserva_id' é obrigatório."}), 400

    if not data.get("transaction_amount"):
        return jsonify({"erro": "Campo 'transaction_amount' é obrigatório."}), 400

    # payment_method_id é validado no MercadoPagoService — aqui aceitamos qualquer valor
    # (incluindo vazio) para que o service possa gerar um ValueError com mensagem útil.

    try:
        mp_response = MercadoPagoService.processar_pagamento(int(reserva_id), data)
    except ValueError as exc:
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=_obter_customer_name(reserva_id),
            error_source="SISTUR_QR",
            error_code="PAYMENT_VALIDATION_ERROR",
            error_message=str(exc),
        )
        return jsonify({"erro": str(exc)}), 400
    except RuntimeError as exc:
        logger.error("Erro de configuração ao processar pagamento: %s", exc)
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=_obter_customer_name(reserva_id),
            error_source="SISTUR_QR",
            error_code="PAYMENT_CONFIG_ERROR",
            error_message=str(exc),
        )
        return jsonify({"erro": str(exc)}), 500
    except Exception as exc:
        logger.exception("Erro inesperado ao processar pagamento para reserva_id=%s", reserva_id)
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=_obter_customer_name(reserva_id),
            error_source="SISTUR_QR",
            error_code="PAYMENT_UNEXPECTED_ERROR",
            error_message=f"Erro interno ao processar pagamento: {exc}",
            raw_payload={"exception": str(exc)},
        )
        return jsonify({"erro": "Erro interno ao processar pagamento.", "detalhe": str(exc)}), 500

    # _transaction_id é campo interno; não expor ao WordPress
    mp_response.pop("_transaction_id", None)

    mp_status = mp_response.get("status", "unknown")
    is_pix = data.get("payment_method_id") == "pix"

    response_body: dict = {
        "payment_id": mp_response.get("id", ""),
        "status": mp_status,
        "status_detail": mp_response.get("status_detail", ""),
        "payment_method": "pix" if is_pix else "card",
        "message": mp_response.get("status_detail", mp_status),
    }

    # Dados do PIX para exibição do QR Code no frontend
    point_of_interaction = mp_response.get("point_of_interaction", {})
    transaction_data = point_of_interaction.get("transaction_data", {})
    if is_pix and transaction_data:
        response_body["qr_code"] = transaction_data.get("qr_code", "")
        response_body["qr_code_base64"] = transaction_data.get("qr_code_base64", "")
        response_body["ticket_url"] = transaction_data.get("ticket_url", "")

    # Pagamentos aprovados ou pendentes são retornados com sucesso
    if mp_status in ("approved", "pending", "in_process"):
        return jsonify({"success": True, **response_body}), 200

    # Registra o erro no log para garantir rastreabilidade, mesmo que o frontend falhe em enviar.
    # Pode ser cartão rejeitado (status="rejected") ou erro de API 400 (ex: formato de CPF).
    msg = mp_response.get("message") or response_body.get("message") or f"Pagamento não aprovado. Status: {mp_status}"
    _mp_status_detail = mp_response.get("status_detail") or ""

    _registrar_log_erro(
        reserva_id=reserva_id,
        customer_name=_obter_customer_name(reserva_id),
        error_source="SISTUR_QR",
        # status_detail tem o motivo real da recusa (cc_rejected_*); fallback para error ou genérico
        error_code=str(_mp_status_detail or mp_response.get("error") or "PAYMENT_REJECTED_OR_FAILED")[:100],
        error_message=str(msg)[:2000],
        raw_payload=mp_response,
        status_detail=_mp_status_detail or None,
    )

    # Rejeitado ou cancelado
    return jsonify({"success": False, **response_body}), 422


@bp.route("/generate-manual-pix", methods=["POST"])
@require_internal_token
def generate_manual_pix():
    """Gera um pagamento PIX manualmente para uma reserva existente.

    Destinado ao uso por operadores no balcão via painel admin do Sistur.
    Autentica com o mesmo Bearer token dos endpoints internos (QR_SERVICE_TOKEN).

    JSON body:
        reserva_id (int): ID da Reserva no Sistur.
        payer_email (str, opcional): E-mail do pagador. Padrão: "<id>@sistur.local".

    Returns:
        200: { success, payment_id, qr_code, qr_code_base64, ticket_url, status }
        400: Dados inválidos ou Reserva não encontrada.
        422: MP não retornou QR Code (ex.: reserva sem total calculado).
        500: Erro interno ou MP_ACCESS_TOKEN não configurado.
    """
    from app.extensions import db
    from app.models.reservas import Reserva
    from app.services.payment_service import MercadoPagoService

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"erro": "Corpo da requisição inválido ou ausente."}), 400

    reserva_id = data.get("reserva_id")
    if not reserva_id:
        return jsonify({"erro": "Campo 'reserva_id' é obrigatório."}), 400

    reserva = db.session.get(Reserva, int(reserva_id))
    if not reserva or reserva.deleted_at is not None:
        return jsonify({"erro": f"Reserva {reserva_id} não encontrada."}), 404

    if not reserva.total_amount or float(reserva.total_amount) <= 0:
        return jsonify({"erro": "Reserva não possui valor total calculado. Edite e salve a reserva primeiro."}), 400

    # Monta dados do pagador a partir do corpo ou da reserva
    payer_email = data.get("payer_email") or f"{reserva.id}@sistur.local"
    nome_parts = (reserva.customer_name or "Cliente").split(" ", 1)
    payer: dict = {
        "email": payer_email,
        "first_name": nome_parts[0],
        "last_name": nome_parts[1] if len(nome_parts) > 1 else "",
    }
    cpf = reserva.customer_document
    if cpf:
        payer["identification"] = {
            "type": "CPF",
            "number": cpf.replace(".", "").replace("-", "").replace("/", ""),
        }

    payment_dados: dict = {
        "payment_method_id": "pix",
        "transaction_amount": float(reserva.total_amount),
        "payer": payer,
    }

    try:
        mp_response = MercadoPagoService.processar_pagamento(int(reserva_id), payment_dados)
    except ValueError as exc:
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=reserva.customer_name,
            error_source="SISTUR_QR",
            error_code="PIX_VALIDATION_ERROR",
            error_message=str(exc),
        )
        return jsonify({"erro": str(exc)}), 400
    except RuntimeError as exc:
        logger.error("Erro de configuração ao gerar PIX manual: %s", exc)
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=reserva.customer_name,
            error_source="SISTUR_QR",
            error_code="PIX_CONFIG_ERROR",
            error_message=str(exc),
        )
        return jsonify({"erro": str(exc)}), 500
    except Exception as exc:
        logger.exception("Erro inesperado ao gerar PIX manual para reserva_id=%s", reserva_id)
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=reserva.customer_name,
            error_source="SISTUR_QR",
            error_code="PIX_UNEXPECTED_ERROR",
            error_message=f"Erro interno ao gerar PIX: {exc}",
            raw_payload={"exception": str(exc)},
        )
        return jsonify({"erro": "Erro interno ao gerar PIX.", "detalhe": str(exc)}), 500

    mp_response.pop("_transaction_id", None)  # campo interno, não expor
    point_of_interaction = mp_response.get("point_of_interaction", {})
    transaction_data = point_of_interaction.get("transaction_data", {})
    qr_code = transaction_data.get("qr_code", "")
    qr_code_base64 = transaction_data.get("qr_code_base64", "")

    if not qr_code:
        _registrar_log_erro(
            reserva_id=reserva_id,
            customer_name=reserva.customer_name,
            error_source="SISTUR_QR",
            error_code="PIX_NO_QR_CODE",
            error_message=(
                f"Mercado Pago não retornou QR Code para a reserva #{reserva_id} "
                f"(cliente: {reserva.customer_name or 'desconhecido'}). "
                f"MP status: {mp_response.get('status')} / {mp_response.get('status_detail')}"
            ),
            raw_payload={
                "mp_status": mp_response.get("status"),
                "mp_detail": mp_response.get("status_detail"),
            },
        )
        return jsonify({
            "erro": "Mercado Pago não retornou QR Code.",
            "mp_status": mp_response.get("status"),
            "mp_detail": mp_response.get("status_detail"),
        }), 422

    return jsonify({
        "success": True,
        "payment_id": str(mp_response.get("id", "")),
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
        "ticket_url": transaction_data.get("ticket_url", ""),
        "status": mp_response.get("status"),
    }), 200


@bp.route("/status/<string:payment_id>", methods=["GET"])
def payment_status(payment_id: str):
    """Retorna o status atual de um pagamento Mercado Pago consultando a Reserva no banco.

    Usado pelo WordPress para polling de status PIX sem precisar chamar a API do MP
    diretamente. A fonte de verdade é o status da Reserva no Sistur, atualizado via webhook.

    Path params:
        payment_id: ID do pagamento no Mercado Pago.

    Returns:
        200: { "payment_id": str, "status": "approved|pending|rejected" }
        404: Se nenhuma Reserva com esse payment_id for encontrada.
    """
    from app.extensions import db
    from app.models.reservas import Reserva, ReservaStatus

    reserva = (
        db.session.query(Reserva)
        .filter(Reserva.payment_id == payment_id, Reserva.deleted_at.is_(None))
        .first()
    )

    if not reserva:
        # Reserva ainda não recebeu o webhook — retorna 404 para o WP continuar o polling
        return jsonify({"erro": f"Pagamento {payment_id} não encontrado."}), 404

    # PARTIALLY_PAID retorna "pending" — o WordPress continua o polling até PAID
    status_map = {
        ReservaStatus.PAID: "approved",
        ReservaStatus.CANCELED: "rejected",
    }
    mp_status = status_map.get(reserva.status, "pending")

    return jsonify({"payment_id": payment_id, "status": mp_status}), 200


@bp.route("/config", methods=["GET"])
def payment_config():
    """Retorna configurações públicas de pagamento para o frontend WordPress.

    Endpoint público (sem autenticação) — expõe apenas a Public Key do Mercado Pago,
    necessária para inicializar o MP Bricks no frontend. O Access Token nunca é exposto.

    Sem public key o MP Bricks não inicializa e **nenhum** cliente consegue pagar
    pelo site. Como a falha acontece no browser, ela não deixaria rastro algum —
    por isso a ausência da chave é registrada em /admin/logs (com throttle de 1h
    para não inundar o painel, já que o WordPress consulta este endpoint a cada
    5 minutos).

    Returns:
        200: { "enabled": bool, "public_key": str }
    """
    import time

    from app.services.configuracao_service import CHAVE_MP_PUBLIC_KEY, ConfiguracaoService

    global _ultimo_log_public_key

    public_key = ConfiguracaoService.get(CHAVE_MP_PUBLIC_KEY) or ""

    if not public_key:
        agora = time.time()
        if agora - _ultimo_log_public_key > _CONFIG_LOG_THROTTLE_SEG:
            _ultimo_log_public_key = agora
            _registrar_log_erro(
                reserva_id=None,
                customer_name=None,
                error_source="MP_BRICKS",
                error_code="MP_PUBLIC_KEY_MISSING",
                error_message=(
                    "Public Key do Mercado Pago não configurada em Configurações. "
                    "O checkout do site não carrega o formulário de pagamento para "
                    "nenhum cliente até que a chave seja preenchida."
                ),
            )

    return jsonify({
        "enabled": bool(public_key),
        "public_key": public_key,
    }), 200


@bp.route("/webhook", methods=["POST"])
def webhook():
    """Recebe notificações de webhook do Mercado Pago.

    Endpoint público chamado diretamente pelo Mercado Pago quando o status de
    um pagamento muda (ex: PIX confirmado, cartão aprovado/rejeitado).

    O MP envia a notificação no corpo como JSON:
        { "type": "payment", "data": { "id": "12345678" } }

    O Sistur valida a notificação consultando o MP pela API para obter o status
    real (nunca confiar apenas no payload do webhook).

    Returns:
        200: Notificação processada (o MP exige HTTP 200 mesmo em casos de "nada feito").
        400: Payload ausente.
        500: Erro interno ou MP_ACCESS_TOKEN não configurado.
    """
    from app.services.payment_service import MercadoPagoService

    data = request.get_json(silent=True)
    if not data:
        # MP pode enviar query params em vez de body — verificar também
        payment_id = request.args.get("data.id") or request.args.get("id")
        if payment_id:
            data = {"type": "payment", "data": {"id": payment_id}}
        else:
            logger.warning("Webhook MP recebido sem payload válido.")
            return jsonify({"erro": "Payload ausente."}), 400

    logger.info("Webhook MP recebido: type=%s", data.get("type") or data.get("action"))

    # payment_id da notificação — usado apenas para enriquecer o log de falha.
    _notif_payment_id = str((data.get("data") or {}).get("id", "") or data.get("data_id", ""))

    try:
        result = MercadoPagoService.processar_webhook(data)
    except RuntimeError as exc:
        logger.error("Erro de configuração no webhook MP: %s", exc)
        # Falha crítica e silenciosa: o cliente pagou no MP e a reserva nunca
        # sai de PENDING, porque o Sistur não consegue confirmar o pagamento.
        _registrar_log_erro(
            reserva_id=None,
            customer_name=None,
            error_source="SISTUR_SYNC",
            error_code="WEBHOOK_CONFIG_ERROR",
            error_message=(
                f"Webhook do Mercado Pago não pôde ser processado por erro de "
                f"configuração: {exc}. Pagamentos confirmados no MP não estão "
                f"atualizando as reservas."
            ),
            raw_payload={"mp_payment_id": _notif_payment_id, "notification": data},
        )
        return jsonify({"erro": str(exc)}), 500
    except Exception as exc:
        logger.exception("Erro inesperado no webhook MP")
        _registrar_log_erro(
            reserva_id=None,
            customer_name=None,
            error_source="SISTUR_SYNC",
            error_code="WEBHOOK_UNEXPECTED_ERROR",
            error_message=(
                f"Erro inesperado ao processar webhook do Mercado Pago "
                f"(mp_payment_id={_notif_payment_id or 'desconhecido'}): {exc}. "
                f"A reserva correspondente pode ter ficado sem confirmação de pagamento."
            ),
            raw_payload={
                "mp_payment_id": _notif_payment_id,
                "notification": data,
                "exception": str(exc),
            },
        )
        return jsonify({"erro": "Erro interno.", "detalhe": str(exc)}), 500

    # O MP exige HTTP 200 para considerar a notificação processada com sucesso
    return jsonify(result), 200
