# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint de API interna — endpoints machine-to-machine.

Autenticação via Bearer token (QR_SERVICE_TOKEN), não via sessão/cookie.
Destinado a serviços internos da rede Docker (ex: qr-reader).
"""

from __future__ import annotations

import hmac

import os
import logging
from datetime import date
from functools import wraps
from flask import Blueprint, abort, current_app, jsonify, request
from app.extensions import db, socketio

bp = Blueprint("api_internal", __name__)

logger = logging.getLogger(__name__)


def require_internal_token(f):
    """Autentica requisições via Bearer token (QR_SERVICE_TOKEN).

    Lê o token de `current_app.config["QR_SERVICE_TOKEN"]` em cada request
    em vez de capturar o env var na importação do módulo. Isso permite que
    o TestConfig injete um token de teste e que reloads da configuração
    tenham efeito sem precisar reimportar o módulo.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        expected_token = current_app.config.get("QR_SERVICE_TOKEN") or os.environ.get("QR_SERVICE_TOKEN")
        if not expected_token:
            logger.warning("QR_SERVICE_TOKEN is undefined. Denying access.")
            abort(401)

        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {expected_token}":
            abort(401)

        return f(*args, **kwargs)
    return decorated_function


@bp.route("/ponto/qr-scan", methods=["POST"])
@require_internal_token
def qr_scan():
    """Recebe leitura de QR code do scanner e registra batida de ponto.

    JSON body:
        qr_payload (str): Token Fernet criptografado lido pelo scanner.

    Returns:
        JSON com resultado da operação.
        201: Batida registrada com sucesso.
        400: Payload ausente ou formato inválido.
        401: Token de serviço inválido (tratado pelo before_request).
        422: QR code inválido, funcionário não encontrado ou token revogado.
    """
    from app.models.funcionario import Funcionario
    from app.services.ponto_service import GeofenceViolationError, PontoService
    from app.services.qr_service import QRService

    data = request.get_json(silent=True)
    if not data or "qr_payload" not in data:
        return jsonify({"erro": "Campo 'qr_payload' é obrigatório."}), 400

    qr_payload = data["qr_payload"]
    qr_secret_key = current_app.config["QR_SECRET_KEY"]

    # 1. Descriptografar o payload Fernet
    try:
        payload = QRService.descriptografar_payload(qr_payload, qr_secret_key)
    except ValueError:
        return jsonify({"erro": "QR code inválido ou adulterado."}), 422

    funcionario_id = payload.get("id")
    token_payload = payload.get("token")

    if not funcionario_id or not token_payload:
        return jsonify({"erro": "Payload do QR code incompleto."}), 422

    # 2. Validar funcionário e anti-revogação
    from app.extensions import db

    funcionario = db.session.get(Funcionario, funcionario_id)
    if not funcionario or not funcionario.ativo:
        return jsonify({"erro": "Funcionário não encontrado ou inativo."}), 422

    if not hmac.compare_digest(funcionario.token_qr or "", token_payload):
        return jsonify({"erro": "QR code revogado. Solicite um novo ao gestor."}), 422

    # 3. Registrar batida (sem GPS — kiosk é em local fixo autorizado).
    # source="QR" porque a origem é uma leitura do QR scanner (ponto.py).
    # "KIOSK" é reservado para batidas via portal touch em terminal fixo.
    try:
        entrada = PontoService.registrar_batida(
            funcionario_id=funcionario_id,
            source="QR",
            ator_id=funcionario_id,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 422
    except GeofenceViolationError as exc:
        return jsonify({"erro": str(exc)}), 422

    # 4. Resposta com dados para exibição/impressão no totem
    # Converte UTC para timezone local (Brasília)
    from datetime import datetime as dt
    import pytz

    tz = pytz.timezone(current_app.config.get("TIMEZONE", "America/Sao_Paulo"))
    hora_local_tz = entrada.punch_time.astimezone(tz)

    hora_local = hora_local_tz.strftime("%H:%M:%S")
    data_completa = hora_local_tz.strftime("%d/%m/%Y %H:%M:%S")

    # 5. Notifica totem local via WebSocket para saudação de voz
    primeiro_nome = funcionario.nome.strip().split()[0]
    punch_type_str = entrada.punch_type.value if entrada.punch_type else "extra"
    try:
        socketio.emit("employee_clocked_in", {
            "first_name": primeiro_nome,
            "punch_type": punch_type_str,
        })
        logger.info("SocketIO emitido: employee_clocked_in → %s (%s)", primeiro_nome, punch_type_str)
    except Exception as exc:
        logger.warning("Falha ao emitir evento SocketIO: %s", exc)

    return jsonify({
        "sucesso": True,
        "nome": funcionario.nome,
        "hora": hora_local,
        "tipo_batida": entrada.punch_type.value if entrada.punch_type else "extra",
        "impressao": {
            "nome": funcionario.nome,
            "hora": hora_local,
            "tipo": entrada.punch_type.value if entrada.punch_type else "extra",
            "data_completa": data_completa,
        },
    }), 201


@bp.route("/ponto/importar-lote", methods=["POST"])
@require_internal_token
def importar_lote_ponto():
    """Importa em lote as batidas do relógio de ponto externo.

    Consumido por `externo/importador_ponto.py`, que lê o relatório .xls do
    aparelho e resolve o vínculo com o funcionário antes de enviar.

    JSON body:
        batidas (list):        Obrigatório. Itens {funcionario_id (int),
                               punch_time (str ISO 8601)}. `punch_time` é a hora
                               local do relógio, sem offset.
        timezone (str):        Fuso do relógio. Default "America/Sao_Paulo".
        dispositivo (str):     Identificador do relógio. Default "REP-01".
        periodo_inicio (str):  Início do período do relatório (AAAA-MM-DD), opcional.
        periodo_fim (str):     Fim do período do relatório (AAAA-MM-DD), opcional.

    Returns:
        200: Lote processado. Corpo com importadas, duplicadas, dias_ignorados,
             batidas_ignoradas, dias_em_revisao e o detalhe das inconsistências.
        400: Payload inválido, ou funcionário inexistente/inativo — nada é gravado.
        401: Token de serviço inválido.
    """
    dados = request.get_json(silent=True) or {}
    batidas = dados.get("batidas")

    if not isinstance(batidas, list) or not batidas:
        return jsonify({"erro": "Campo 'batidas' é obrigatório e deve ser uma lista não vazia."}), 400

    def _data(chave: str):
        bruto = dados.get(chave)
        if not bruto:
            return None
        try:
            return date.fromisoformat(str(bruto))
        except ValueError:
            return None

    from app.services.ponto_import_service import PontoImportService

    try:
        resultado = PontoImportService.importar_lote(
            batidas=batidas,
            dispositivo=str(dados.get("dispositivo") or "REP-01"),
            timezone_relogio=str(dados.get("timezone") or "America/Sao_Paulo"),
            periodo_inicio=_data("periodo_inicio"),
            periodo_fim=_data("periodo_fim"),
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"erro": str(exc)}), 400
    except Exception:
        db.session.rollback()
        logger.exception("Falha ao importar lote de ponto")
        return jsonify({"erro": "Erro interno ao importar o lote."}), 500

    logger.info(
        "Importacao de ponto: %s importadas, %s duplicadas, %s dias ignorados.",
        resultado["importadas"],
        resultado["duplicadas"],
        resultado["dias_ignorados"],
    )
    return jsonify(resultado), 200


@bp.route("/reservas/recursos/disponibilidade", methods=["GET"])
@require_internal_token
def recursos_disponibilidade():
    """Retorna disponibilidade de ativos físicos para um período.

    Usado pelo formulário de reserva externo (WordPress) para exibir
    o accordion de seleção de churrasqueiras e outros recursos físicos.

    Query params (todos obrigatórios):
        source_id (int): ID da fonte (venue).
        check_in (str): Data de início (YYYY-MM-DD).
        check_out (str): Data de término (YYYY-MM-DD).

    Returns:
        200 JSON: { source_id, check_in, check_out, recursos: [...] }
        400 JSON: { erro: "..." } se parâmetros ausentes ou datas inválidas.
    """
    from datetime import date
    from app.services.reserva_service import ReservaService

    source_id = request.args.get("source_id", type=int)
    check_in_str = request.args.get("check_in")
    check_out_str = request.args.get("check_out")

    if not source_id:
        return jsonify({"erro": "Parâmetro 'source_id' é obrigatório."}), 400
    if not check_in_str:
        return jsonify({"erro": "Parâmetro 'check_in' é obrigatório."}), 400
    if not check_out_str:
        return jsonify({"erro": "Parâmetro 'check_out' é obrigatório."}), 400

    try:
        check_in = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str)
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    try:
        recursos = ReservaService.verificar_disponibilidade_recursos(
            source_id=source_id,
            check_in=check_in,
            check_out=check_out,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    return jsonify({
        "source_id": source_id,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "recursos": recursos,
    }), 200


# ---------------------------------------------------------------------------
# Logging de falhas de criação de reserva (visível em /admin/logs)
# ---------------------------------------------------------------------------

# Campos do payload de ingest seguros para gravar no log (sem dados pessoais além
# do necessário para localizar a reserva).
_CAMPOS_LOG_INGEST = (
    "source_name", "category_name", "customer_name",
    "check_in_date", "check_out_date", "items", "item_id", "quantity",
)


def _payload_resumido(data: dict) -> dict:
    """Extrai do payload apenas os campos úteis para diagnóstico do erro."""
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _CAMPOS_LOG_INGEST if k in data}


def _log_ingest_erro(
    customer_name: str | None,
    error_code: str,
    error_message: str,
    raw_payload: dict | None = None,
) -> None:
    """Registra uma falha de criação de reserva como IntegrationLog(SISTUR_SYNC).

    Args:
        customer_name: Nome do cliente informado no payload, quando disponível.
        error_code:    Código curto do tipo de falha (INGEST_*).
        error_message: Descrição legível do problema.
        raw_payload:   Dados brutos para diagnóstico (opcional).
    """
    from app.services.integration_log_service import IntegrationLogService

    IntegrationLogService.registrar(
        error_source="SISTUR_SYNC",
        error_code=error_code,
        error_message=error_message,
        customer_name=customer_name,
        raw_payload=raw_payload,
        user_agent=request.headers.get("User-Agent"),
    )


@bp.route("/reservas/ingest", methods=["POST"])
@require_internal_token
def ingest_reserva():
    """Recebe carga genérica de nova reserva e mapeia para a API interna.

    Suporta dois formatos de itens:
    - Item único: item_id + quantity + price_override no topo do payload.
    - Múltiplos itens: campo items=[{item_id, quantity, price_override}, ...].

    JSON body:
        source_name (str): Obrigatório. Nome da fonte.
        category_name (str): Obrigatório. Nome da categoria.
        customer_name (str): Obrigatório.
        check_in_date (str): YYYY-MM-DD. Obrigatório.

        # Formato item único:
        item_id (int): Obrigatório se items ausente.
        price_override (str/float): Obrigatório se items ausente.
        quantity (int): Opcional. Padrão 1.

        # Formato múltiplos itens:
        items (list): Lista de {item_id, quantity, price_override}.

        # Opcionais:
        check_out_date (str): YYYY-MM-DD.
        customer_document (str): CPF/CNPJ.
        origin (str): WEB|BALCAO. Padrão WEB.
        metadata_info (dict): Metadados adicionais explícitos.
        ...outros campos: incorporados ao metadata_info.

    Toda falha aqui impede o cliente de chegar à tela de pagamento. Por isso cada
    caminho de erro registra um IntegrationLog (source=SISTUR_SYNC) — sem ele a
    reclamação "não consegui reservar" não deixa nenhum rastro em /admin/logs.

    Returns:
        JSON da reserva criada com soft lock 15min.
        400: Payload inválido ou recusado pelas regras de negócio.
        403: Cliente bloqueado (blacklist).
        500: Erro inesperado ao criar a reserva.
    """
    from datetime import date, datetime
    from app.services.reserva_service import ReservaService

    def _parse_dt(s: str) -> datetime:
        """Converte string YYYY-MM-DD ou YYYY-MM-DDTHH:MM em datetime."""
        if 'T' in s:
            return datetime.fromisoformat(s)
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day)

    data = request.get_json(silent=True)
    if not data:
        _log_ingest_erro(None, "INGEST_EMPTY_BODY", "Requisição de criação de reserva sem body JSON.")
        return jsonify({"erro": "Body JSON ausente."}), 400

    _cliente = str(data.get("customer_name") or "") or None

    # Campos base obrigatórios
    required = ["source_name", "category_name", "customer_name", "check_in_date"]
    for field in required:
        if field not in data:
            _log_ingest_erro(
                _cliente,
                "INGEST_VALIDATION_ERROR",
                f"Criação de reserva recusada: campo obrigatório ausente '{field}'.",
                raw_payload={"campo_ausente": field, "payload": _payload_resumido(data)},
            )
            return jsonify({"erro": f"Campo obrigatório ausente: '{field}'."}), 400

    # Deve ter items array OU item_id + price_override
    has_items_array = bool(data.get("items"))
    has_single_item = "item_id" in data and "price_override" in data
    if not has_items_array and not has_single_item:
        _log_ingest_erro(
            _cliente,
            "INGEST_EMPTY_ITEMS",
            "Criação de reserva recusada: payload sem itens. Normalmente indica slug "
            "de catálogo não encontrado no WordPress (item renomeado/removido no Sistur).",
            raw_payload={"payload": _payload_resumido(data)},
        )
        return jsonify({"erro": "Payload deve conter 'items' (array) ou 'item_id' + 'price_override'."}), 400

    try:
        check_in = _parse_dt(data["check_in_date"])
        check_out = _parse_dt(data["check_out_date"]) if data.get("check_out_date") else None
    except ValueError:
        _log_ingest_erro(
            _cliente,
            "INGEST_VALIDATION_ERROR",
            "Criação de reserva recusada: formato de data inválido "
            f"(check_in={data.get('check_in_date')!r} check_out={data.get('check_out_date')!r}).",
        )
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD ou YYYY-MM-DDTHH:MM."}), 400

    source_name = data.pop("source_name")
    category_name = data.pop("category_name")
    customer_name = data.pop("customer_name")
    data.pop("check_in_date")
    data.pop("check_out_date", None)
    customer_document = data.pop("customer_document", None)
    customer_phone    = (data.pop("customer_phone", None) or "").strip() or None
    origin_str = data.pop("origin", "WEB")

    if has_items_array:
        items_payload = data.pop("items")
    else:
        item_id = data.pop("item_id")
        quantity = data.pop("quantity", 1)
        price_override = data.pop("price_override")
        items_payload = [{"item_id": item_id, "quantity": quantity, "price_override": price_override}]

    # metadata_info explícito se presente; campos restantes são incorporados
    explicit_meta = data.pop("metadata_info", {}) or {}
    metadata_info = {**data, **explicit_meta}
    # Injeta telefone do cliente (chave padrão usada pelo admin e bot_api)
    if customer_phone and "telefone" not in metadata_info:
        metadata_info["telefone"] = customer_phone

    try:
        from app.services.blacklist_service import ClientBlacklistedError
        reserva = ReservaService.criar(
            source_name=source_name,
            category_name=category_name,
            customer_name=customer_name,
            customer_document=customer_document,
            origin=origin_str,
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=items_payload,
            metadata_info=metadata_info,
            ator_id=None, # Machine/System action
        )
    except ClientBlacklistedError as exc:
        public_message = "Sem resposta do servidor. Tente novamente em instantes."
        _log_ingest_erro(
            customer_name,
            "INGEST_CLIENT_BANNED",
            f"Criação de reserva bloqueada pela blacklist para '{customer_name}' "
            f"(documento={customer_document or 'não informado'}). O cliente vê uma "
            f"mensagem genérica e reclama que o site não deixa reservar.",
            raw_payload={"code": exc.code, "customer_document": customer_document},
        )
        return jsonify({
            "sucesso": False,
            "status": "error",
            "code": exc.code,
            "erro": public_message,
            "message": public_message,
        }), 403
    except ValueError as exc:
        # Inclui a divergência de preço da checagem anti-fraude, item inexistente
        # e estoque esgotado — todas impedem o cliente de pagar.
        _log_ingest_erro(
            customer_name,
            "INGEST_REJECTED",
            f"Criação de reserva recusada: {exc}",
            raw_payload={
                "source_name": source_name,
                "category_name": category_name,
                "items": items_payload,
                "check_in_date": check_in.isoformat(),
                "check_out_date": check_out.isoformat() if check_out else None,
            },
        )
        return jsonify({"erro": str(exc)}), 400
    except Exception as exc:
        # Sem este handler o Flask devolveria uma página de erro 500 e o WordPress
        # exibiria "erro desconhecido" — sem nenhum registro em /admin/logs.
        logger.exception("Erro inesperado ao criar reserva via ingest.")
        _log_ingest_erro(
            customer_name,
            "INGEST_UNEXPECTED_ERROR",
            f"Erro inesperado ao criar reserva: {exc}",
            raw_payload={
                "source_name": source_name,
                "category_name": category_name,
                "items": items_payload,
                "exception": str(exc),
            },
        )
        return jsonify({"erro": "Erro interno ao criar a reserva."}), 500

    return jsonify({
        "sucesso":      True,
        "reserva_id":  reserva.id,
        "group_id":    reserva.group_id,
        "expires_at":  reserva.expires_at.isoformat() if reserva.expires_at else None,
        "total_amount": float(reserva.total_amount) if reserva.total_amount is not None else None,
    }), 201
