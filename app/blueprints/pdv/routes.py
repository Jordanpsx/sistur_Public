# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Rotas do PDV (Ponto de Venda) — API JSON consumida pela SPA de balcão.

Prefixo: /api/v1/pdv (registrado em app/__init__.py).

| Rota                              | Método | Permissão          |
|-----------------------------------|--------|--------------------|
| /shifts/open                      | POST   | pdv.manage_shift   |
| /shifts/<id>/close                | POST   | pdv.manage_shift   |
| /shifts/<id>/approve-close        | POST   | pdv.manage_shift   |
| /shifts/pending-approval          | GET    | pdv.view_reports   |
| /shifts/<id>/report               | GET    | pdv.view_reports   |
| /sync-logs                        | GET    | pdv.view_reports   |
| /catalog                          | GET    | pdv.operate        |
| /quote                            | POST   | pdv.operate        |
| /manager-override/authorize       | POST   | pdv.operate        |
| /sale                             | POST   | pdv.operate        |
| /sale/comanda-append              | POST   | pdv.operate        |
| /customer-lookup                  | GET    | pdv.operate        |
| /held-carts                       | GET    | pdv.operate        |
| /held-carts                       | POST   | pdv.operate        |
| /held-carts/<id>/resolve          | POST   | pdv.operate        |
| /movimentacao-caixa/sangria       | POST   | pdv.manage_shift   |
| /movimentacao-caixa/suprimento    | POST   | pdv.manage_shift   |
| /movimentacao-caixa               | GET    | pdv.manage_shift   |
| /areas                            | GET    | pdv.manage_shift   |

Contratos completos: docs/pdv/README.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from flask import jsonify, request, session

from app.blueprints.pdv import bp
from app.blueprints.portal.routes import login_required
from app.core.models import Area
from app.core.permissions import require_permission
from app.extensions import db
from app.models.pdv import PdvMovimentacaoCaixa
from app.models.reservas import ReservaPreferencia, ReservaStatusConfig
from app.services.movimentacao_caixa_service import MovimentacaoCaixaService
from app.services.pdv_service import PdvService, QuebraDeCaixaError
from app.services.reserva_pricing_service import ReservaPricingService
from app.services.reserva_service import OverbookingError

logger = logging.getLogger(__name__)


def _ator_id() -> int | None:
    """ID do funcionário em sessão (para auditoria nos services)."""
    return session.get("funcionario_id")


def _erro(mensagem: str, status: int = 400, **extra):
    """Resposta JSON de erro padronizada."""
    payload = {"erro": mensagem}
    payload.update(extra)
    return jsonify(payload), status


def _preferencia_operador() -> ReservaPreferencia | None:
    ator_id = _ator_id()
    if not ator_id:
        return None
    return db.session.query(ReservaPreferencia).filter_by(
        funcionario_id=ator_id,
    ).first()


def _pdv_default_status_from_preference(preferencia: ReservaPreferencia | None) -> str | None:
    status = preferencia.pdv_default_status if preferencia else None
    if not status:
        return None
    config = db.session.query(ReservaStatusConfig).filter_by(
        internal_name=status,
    ).first()
    if not config or config.internal_name == "ARCHIVED_VERSION":
        return None
    return config.internal_name


# ---------------------------------------------------------------------------
# Turnos
# ---------------------------------------------------------------------------


@bp.route("/shifts/open", methods=["POST"])
@login_required
@require_permission("pdv", "manage_shift")
def abrir_turno():
    """Abre um turno de caixa.

    Body JSON: source_id (int), opening_float_amount (float, opcional),
    opening_float_pix (float, opcional).
    O operador é o funcionário em sessão.
    """
    data = request.get_json(silent=True) or {}
    try:
        shift = PdvService.abrir_turno(
            source_id=data.get("source_id"),
            operador_id=_ator_id(),
            opening_float_amount=float(data.get("opening_float_amount", 0) or 0),
            opening_float_pix=float(data.get("opening_float_pix", 0) or 0),
            ator_id=_ator_id(),
        )
    except (ValueError, TypeError) as exc:
        return _erro(str(exc))
    return jsonify({
        "shift_id": shift.id,
        "status": shift.status.value,
        "opening_float_amount": float(shift.opening_float_amount or 0),
        "opening_float_pix": float(shift.opening_float_pix or 0),
        "opened_at": shift.opened_at.isoformat(),
    }), 201


@bp.route("/shifts/<int:shift_id>/close", methods=["POST"])
@login_required
@require_permission("pdv", "manage_shift")
def fechar_turno(shift_id: int):
    """Fecha um turno de caixa e retorna a conferência físico/PIX.

    Body JSON: closing_counted_amount (float, obrigatório),
    closing_counted_pix (float, opcional), discrepancy_reason (opcional).
    """
    data = request.get_json(silent=True) or {}
    if data.get("closing_counted_amount") is None:
        return _erro("Campo 'closing_counted_amount' é obrigatório.")
    try:
        counted_cash = float(data["closing_counted_amount"])
        counted_pix = float(data.get("closing_counted_pix", 0) or 0)
        shift = PdvService.fechar_turno(
            shift_id,
            closing_counted_amount=counted_cash,
            closing_counted_pix=counted_pix,
            discrepancy_reason=data.get("discrepancy_reason"),
            ator_id=_ator_id(),
        )
    except QuebraDeCaixaError:
        return _erro(
            "Valor incoerente com o esperado. Informe o motivo para enviar à aprovação.",
            409,
            approval_required=True,
        )
    except (ValueError, TypeError) as exc:
        return _erro(str(exc))
    return jsonify({
        "shift_id": shift.id,
        "status": shift.status.value,
        "approval_required": shift.status.value == "PENDING_APPROVAL",
        "message": (
            "Fechamento enviado para aprovação de supervisor."
            if shift.status.value == "PENDING_APPROVAL"
            else "Turno fechado com sucesso."
        ),
    })


@bp.route("/shifts/<int:shift_id>/approve-close", methods=["POST"])
@login_required
@require_permission("pdv", "manage_shift")
def aprovar_fechamento(shift_id: int):
    """Aprova um fechamento de turno com quebra de caixa."""
    try:
        shift = PdvService.aprovar_fechamento(
            shift_id,
            aprovador_id=_ator_id(),
            ator_id=_ator_id(),
        )
    except ValueError as exc:
        return _erro(str(exc))
    return jsonify({
        "shift_id": shift.id,
        "status": shift.status.value,
        "approved_by_id": shift.approved_by_id,
        "approved_at": shift.approved_at.isoformat() if shift.approved_at else None,
        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
    })


@bp.route("/shifts/pending-approval", methods=["GET"])
@login_required
@require_permission("pdv", "view_reports")
def listar_turnos_pendentes():
    """Lista turnos com quebra de caixa aguardando aprovação."""
    return jsonify({"shifts": PdvService.listar_turnos_pendentes()})


@bp.route("/shifts/current", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def turno_atual():
    """Turno OPEN do operador em sessão, para restauração da SPA pós-F5.

    Response: {"shift": {...}} ou {"shift": null}.
    """
    return jsonify({"shift": PdvService.turno_atual(_ator_id())})


@bp.route("/sources", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def listar_venues():
    """Venues ativos para o seletor de abertura de turno."""
    return jsonify({"sources": PdvService.listar_venues()})


@bp.route("/preferences", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def preferencias_pdv():
    """Preferências do operador logado para acelerar o PDV."""
    pref = _preferencia_operador()
    return jsonify({
        "default_source_id": pref.default_source_id if pref else None,
        "default_category_id": pref.default_category_id if pref else None,
        "pdv_default_status": _pdv_default_status_from_preference(pref),
        "pdv_favorite_item_ids": pref.pdv_favorite_item_ids if pref and pref.pdv_favorite_item_ids else [],
    })


@bp.route("/shifts/<int:shift_id>/report", methods=["GET"])
@login_required
@require_permission("pdv", "view_reports")
def relatorio_turno(shift_id: int):
    """Relatório de vendas do turno agrupado por método de pagamento."""
    try:
        return jsonify(PdvService.relatorio_turno(shift_id))
    except ValueError as exc:
        return _erro(str(exc), 404)


@bp.route("/sync-logs", methods=["GET"])
@login_required
@require_permission("pdv", "view_reports")
def listar_sync_logs():
    """Lista tentativas de sync offline para reconciliação gerencial.

    Query params: outcome (opcional), limit (opcional, máximo 200).
    """
    try:
        logs = PdvService.listar_sync_logs(
            outcome=request.args.get("outcome") or None,
            limit=request.args.get("limit", default=100, type=int) or 100,
        )
    except ValueError as exc:
        return _erro(str(exc))
    return jsonify({"sync_logs": logs})


# ---------------------------------------------------------------------------
# Catálogo & cotação
# ---------------------------------------------------------------------------


@bp.route("/catalog", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def catalogo():
    """Catálogo do venue com flags offline por item, cacheável via ETag.

    Query params: source_id (int, obrigatório).
    Envie If-None-Match com o ETag anterior para receber 304 sem payload.
    """
    source_id = request.args.get("source_id", type=int)
    if not source_id:
        return _erro("Parâmetro 'source_id' é obrigatório.")
    try:
        pref = _preferencia_operador()
        payload = PdvService.catalogo_pdv(
            source_id,
            favorite_item_ids=pref.pdv_favorite_item_ids if pref and pref.pdv_favorite_item_ids else [],
        )
    except ValueError as exc:
        return _erro(str(exc), 404)

    corpo = json.dumps(payload["source"], sort_keys=True, ensure_ascii=False)
    etag = hashlib.sha256(corpo.encode("utf-8")).hexdigest()[:32]
    if request.headers.get("If-None-Match") == etag:
        return "", 304

    resposta = jsonify(payload)
    resposta.set_etag(etag)
    resposta.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return resposta


@bp.route("/quote", methods=["POST"])
@login_required
@require_permission("pdv", "operate")
def cotacao():
    """Cotação autoritativa server-side (preview de confiança da SPA).

    Body JSON: source_id, items [{item_id, quantity}],
    manual_discount_percent (opcional), check_in/check_out (opcional,
    ISO — default: agora, venda de balcão imediata).
    """
    data = request.get_json(silent=True) or {}
    if not data.get("source_id") or not data.get("items"):
        return _erro("Campos 'source_id' e 'items' são obrigatórios.")

    agora = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        check_in = (
            datetime.fromisoformat(data["check_in"]) if data.get("check_in") else agora
        )
        check_out = (
            datetime.fromisoformat(data["check_out"]) if data.get("check_out") else check_in
        )
        pricing = ReservaPricingService.calculate(
            source_id=int(data["source_id"]),
            check_in_date=check_in,
            check_out_date=check_out,
            items=data["items"],
            category_id=data.get("category_id"),
            manual_discount_percent=float(data.get("manual_discount_percent", 0) or 0),
        )
    except (ValueError, TypeError) as exc:
        return _erro(str(exc))
    return jsonify(pricing)


# ---------------------------------------------------------------------------
# Vendas
# ---------------------------------------------------------------------------


@bp.route("/manager-override/authorize", methods=["POST"])
@login_required
@require_permission("pdv", "operate")
def autorizar_manager_override():
    """Valida credenciais de gerente para override pontual do PDV.

    Body JSON: cpf, senha, reason.
    """
    data = request.get_json(silent=True) or {}
    try:
        override = PdvService.autorizar_manager_override(
            cpf=data.get("cpf") or "",
            senha=data.get("senha") or "",
            reason=data.get("reason") or "",
        )
    except ValueError as exc:
        return _erro(str(exc), 403)
    return jsonify({"manager_override": override})


@bp.route("/sale", methods=["POST"])
@login_required
@require_permission("pdv", "operate")
def registrar_venda():
    """Registra uma venda avulsa de balcão (idempotente por client_sale_uuid).

    Body JSON: client_sale_uuid, shift_id, source_name, category_name,
    items [{item_id, quantity, resource_id?}], payment_method
    (CASH | EXTERNAL_CARD_TERMINAL | MP_PIX | MP_CARD),
    customer_name?, customer_document?, amount_tendered?,
    manual_discount_percent?, force_overbooking?, manager_override?,
    offline_override_note? ({cpf, reason} — venda 🔴 autorizada offline,
    sem force_overbooking), synced_offline?, device_id?.

    Respostas: 201 criada · 200 duplicata idempotente · 400 validação ·
    409 conflito de estoque (OverbookingError).
    """
    data = request.get_json(silent=True) or {}
    try:
        resultado = PdvService.registrar_venda(
            client_sale_uuid=data.get("client_sale_uuid"),
            shift_id=data.get("shift_id"),
            source_name=data.get("source_name"),
            category_name=data.get("category_name"),
            items=data.get("items") or [],
            payment_method=data.get("payment_method"),
            customer_name=data.get("customer_name"),
            customer_document=data.get("customer_document"),
            amount_tendered=data.get("amount_tendered"),
            change_source=data.get("change_source") or "CASH",
            manual_discount_percent=float(data.get("manual_discount_percent", 0) or 0),
            force_overbooking=bool(data.get("force_overbooking", False)),
            manager_override=data.get("manager_override"),
            offline_override_note=data.get("offline_override_note"),
            synced_offline=bool(data.get("synced_offline", False)),
            device_id=data.get("device_id"),
            pdv_default_status=(
                data.get("pdv_default_status")
                or _pdv_default_status_from_preference(_preferencia_operador())
            ),
            ator_id=_ator_id(),
        )
    except OverbookingError as exc:
        return _erro(
            str(exc), 409,
            conflito={"resource_name": exc.resource_name, "conflitos": exc.conflitos},
        )
    except (ValueError, TypeError) as exc:
        return _erro(str(exc))

    status = 200 if resultado["duplicate"] else 201
    return jsonify(resultado), status


@bp.route("/sale/comanda-append", methods=["POST"])
@login_required
@require_permission("pdv", "operate")
def anexar_comanda():
    """Anexa itens à comanda de uma reserva ativa (pagamento no check-out).

    Body JSON: client_sale_uuid, shift_id, reserva_id,
    items [{item_id, quantity}].

    Respostas: 201 anexado (retorna reserva_id da NOVA versão) ·
    200 duplicata idempotente · 400 validação.
    """
    data = request.get_json(silent=True) or {}
    try:
        resultado = PdvService.anexar_comanda(
            client_sale_uuid=data.get("client_sale_uuid"),
            shift_id=data.get("shift_id"),
            reserva_id=data.get("reserva_id"),
            items=data.get("items") or [],
            ator_id=_ator_id(),
        )
    except OverbookingError as exc:
        return _erro(
            str(exc), 409,
            conflito={"resource_name": exc.resource_name, "conflitos": exc.conflitos},
        )
    except (ValueError, TypeError) as exc:
        return _erro(str(exc))

    status = 200 if resultado["duplicate"] else 201
    return jsonify(resultado), status


# ---------------------------------------------------------------------------
# Lookup de cliente / comanda
# ---------------------------------------------------------------------------


@bp.route("/customer-lookup", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def buscar_cliente():
    """Busca reservas ativas por CPF/CNPJ ou nome para vincular comanda.

    Query params: q (string, ≥ 3 chars), source_id (int, opcional).
    """
    try:
        resultados = PdvService.buscar_cliente(
            request.args.get("q", ""),
            source_id=request.args.get("source_id", type=int),
        )
    except ValueError as exc:
        return _erro(str(exc))
    return jsonify({"results": resultados})


# ---------------------------------------------------------------------------
# Carrinhos suspensos (espelho servidor)
# ---------------------------------------------------------------------------


@bp.route("/held-carts", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def listar_carrinhos():
    """Lista carrinhos suspensos pendentes do turno (query param: shift_id)."""
    shift_id = request.args.get("shift_id", type=int)
    if not shift_id:
        return _erro("Parâmetro 'shift_id' é obrigatório.")
    carts = PdvService.listar_carrinhos_suspensos(shift_id)
    return jsonify({
        "held_carts": [
            {
                "id": c.id,
                "label": c.label,
                "cart_json": c.cart_json,
                "held_at": c.held_at.isoformat(),
            }
            for c in carts
        ]
    })


@bp.route("/held-carts", methods=["POST"])
@login_required
@require_permission("pdv", "operate")
def suspender_carrinho():
    """Salva espelho servidor de um carrinho suspenso.

    Body JSON: shift_id, cart_json (dict), label (opcional).
    """
    data = request.get_json(silent=True) or {}
    try:
        held = PdvService.suspender_carrinho(
            shift_id=data.get("shift_id"),
            cart_json=data.get("cart_json"),
            label=data.get("label"),
            ator_id=_ator_id(),
        )
    except (ValueError, TypeError) as exc:
        return _erro(str(exc))
    return jsonify({"held_cart_id": held.id}), 201


@bp.route("/held-carts/<int:held_cart_id>/resolve", methods=["POST"])
@login_required
@require_permission("pdv", "operate")
def resolver_carrinho(held_cart_id: int):
    """Marca um carrinho suspenso como resolvido (retomado ou descartado)."""
    try:
        held = PdvService.resolver_carrinho_suspenso(held_cart_id, ator_id=_ator_id())
    except ValueError as exc:
        return _erro(str(exc), 404)
    return jsonify({"held_cart_id": held.id, "resolved_at": held.resolved_at.isoformat()})


# ---------------------------------------------------------------------------
# Movimentações de caixa (sangria/suprimento)
# ---------------------------------------------------------------------------


def _movimentacao_json(mov: PdvMovimentacaoCaixa) -> dict:
    """Serializa uma PdvMovimentacaoCaixa para a SPA, com nomes resolvidos."""
    return {
        "id": mov.id,
        "caixa_turno_id": mov.caixa_turno_id,
        "tipo_movimentacao": mov.tipo_movimentacao.value,
        "valor": float(mov.valor),
        "conta_destino_id": mov.conta_destino_id,
        "centro_de_custo_id": mov.centro_de_custo_id,
        "centro_de_custo_nome": mov.centro_de_custo.name if mov.centro_de_custo else None,
        "funcionario_id": mov.funcionario_id,
        "funcionario_nome": mov.funcionario.nome if mov.funcionario else None,
        "autorizado_por_id": mov.autorizado_por_id,
        "autorizado_por_nome": mov.autorizado_por.nome if mov.autorizado_por else None,
        "descricao": mov.descricao,
        "data_hora": mov.data_hora.isoformat() if mov.data_hora else None,
    }


@bp.route("/movimentacao-caixa/sangria", methods=["POST"])
@login_required
@require_permission("pdv", "manage_shift")
def registrar_sangria():
    """Registra retirada de dinheiro físico da gaveta (sangria).

    Body JSON: caixa_turno_id, tipo_movimentacao (TRANSFERENCIA_INTERNA |
    RECOLHIMENTO_COFRE | PAGAMENTO_DESPESA), valor, descricao,
    conta_destino_id? (obrigatório p/ TRANSFERENCIA_INTERNA),
    centro_de_custo_id? (obrigatório p/ PAGAMENTO_DESPESA),
    manager_override? ({manager_id, reason} de /manager-override/authorize —
    obrigatório quando valor >= R$ 500,00).
    """
    data = request.get_json(silent=True) or {}
    try:
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=data.get("caixa_turno_id"),
            tipo_movimentacao=data.get("tipo_movimentacao"),
            valor=data.get("valor"),
            funcionario_id=_ator_id(),
            descricao=data.get("descricao") or "",
            conta_destino_id=data.get("conta_destino_id"),
            centro_de_custo_id=data.get("centro_de_custo_id"),
            manager_override=data.get("manager_override"),
            ator_id=_ator_id(),
        )
    except ValueError as exc:
        return _erro(str(exc))
    return jsonify({"movimentacao": _movimentacao_json(mov)}), 201


@bp.route("/movimentacao-caixa/suprimento", methods=["POST"])
@login_required
@require_permission("pdv", "manage_shift")
def registrar_suprimento():
    """Registra reforço de dinheiro físico na gaveta (suprimento).

    Body JSON: caixa_turno_id, valor, descricao, manager_override?
    (obrigatório quando valor >= R$ 500,00).
    """
    data = request.get_json(silent=True) or {}
    try:
        mov = MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=data.get("caixa_turno_id"),
            valor=data.get("valor"),
            funcionario_id=_ator_id(),
            descricao=data.get("descricao") or "",
            manager_override=data.get("manager_override"),
            ator_id=_ator_id(),
        )
    except ValueError as exc:
        return _erro(str(exc))
    return jsonify({"movimentacao": _movimentacao_json(mov)}), 201


@bp.route("/movimentacao-caixa", methods=["GET"])
@login_required
@require_permission("pdv", "manage_shift")
def listar_movimentacao_caixa():
    """Lista movimentações de caixa de um turno (query param: shift_id)."""
    shift_id = request.args.get("shift_id", type=int)
    if not shift_id:
        return _erro("Parâmetro 'shift_id' é obrigatório.")
    movs = MovimentacaoCaixaService.listar_por_turno(shift_id)
    return jsonify({"movimentacoes": [_movimentacao_json(m) for m in movs]})


@bp.route("/areas", methods=["GET"])
@login_required
@require_permission("pdv", "manage_shift")
def listar_areas():
    """Lista áreas ativas (centros de custo) para o formulário de sangria."""
    areas = db.session.query(Area).filter(Area.is_active.is_(True)).order_by(Area.name).all()
    return jsonify({"areas": [{"id": a.id, "name": a.name} for a in areas]})
