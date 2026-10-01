# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Reservas Blueprint — Sistema de reservas genérico (bookings).

Rotas HTML (Dashboard)
---------------------
GET  /reservas/                      → index()          # dashboard do módulo (suporta ?view=lista)
GET  /reservas/sources               → listar_fontes()  # listagem de venues
GET  /reservas/<int:reserva_id>      → detalhe()        # detalhe de uma reserva
GET  /reservas/nova                             → admin_nova_reserva_form()  # formulário nova reserva (balcão)
POST /reservas/nova                             → admin_nova_reserva_submit() # processa nova reserva
GET|POST /reservas/predefinicoes               → admin_predefinicoes()      # predefinições do operador (PDV)
GET  /reservas/admin/reservas/<int:id>/historico → admin_historico_reserva() # histórico de versões
GET  /reservas/admin/config/status               → admin_status_config()      # CRUD de ReservaStatusConfig
POST /reservas/admin/config/status/<id>/editar   → admin_status_config_editar() # salva edição inline
POST /reservas/admin/config/status/<id>/deletar  → admin_status_config_deletar() # remove entry não-reservada
POST /reservas/admin/config/status/novo          → admin_status_config_novo()  # cria nova entry
GET  /reservas/admin/config/ajustes-preco                      → admin_ajustes_preco()         # Outros ajustes de preços (Early Bird e futuros)
POST /reservas/admin/config/ajustes-preco/criar                → admin_ajustes_preco_criar()   # cria AdvanceDiscountRule
POST /reservas/admin/config/ajustes-preco/<id>/editar          → admin_ajustes_preco_editar()  # edita AdvanceDiscountRule
POST /reservas/admin/config/ajustes-preco/<id>/deletar         → admin_ajustes_preco_deletar() # remove AdvanceDiscountRule
GET  /reservas/api/public/desconto-antecipado                  → api_public_desconto_antecipado() # desconto early bird para uma data (público)
GET  /reservas/analytics/              → analytics_dashboard()     # dashboard de análise
GET  /reservas/analytics/api/dados     → analytics_api_dados()     # API JSON dos dados
GET  /reservas/consumo-itens           → consumo_itens()           # relatório de consumo de itens
GET  /reservas/consumo-itens/<id>/reservas-detalhe → consumo_itens_detalhe(id)  # drill-down JSON

Rotas JSON API (Headless)
-------------------------
GET   /reservas/api/                        → api_listar()          # lista com filtros
GET   /reservas/api/sources                 → api_listar_fontes()   # lista de venues
GET   /reservas/api/<int:id>                → api_detalhe(id)       # detalhe de reserva
GET   /reservas/api/disponibilidade         → api_disponibilidade() # disponibilidade de itens
GET   /reservas/api/recursos/disponibilidade → api_disponibilidade_recursos() # disponibilidade de ativos físicos (accordion)
POST  /reservas/api/                        → api_criar()           # cria nova reserva
PATCH /reservas/api/<int:id>                → api_editar(id)        # atualiza (nova versão)
POST  /reservas/api/<int:id>/cancelar       → api_cancelar(id)      # cancela reserva
POST  /reservas/api/<int:id>/deletar        → api_deletar(id)       # soft delete

Todas as rotas exigem sessão ativa (@login_required) e a permissão
correspondente (@require_permission).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy.orm import selectinload

from app.blueprints.portal.routes import login_required
from app.core.access_policy import ActorContext, ReservaAccessPolicy
from app.core.permissions import require_permission
from app.extensions import db
from app.models.calendario import TipoEvento
from app.models.funcionario import Funcionario
from app.models.reservas import (
    AdvanceDiscountRule,
    AdvanceDiscountStayTier,
    CalendarDiscount,
    CalendarDiscountAdvanceTier,
    BillingType,
    PhysicalAsset,
    Reserva,
    ReservaCategory,
    ReservaGroup,
    ReservaItem,
    ReservaItemLink,
    ReservaPreferencia,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
    ReservaStatusConfig,
    ResourceImage,
    SecurityDeposit,
)
from app.services.resource_notice_service import ResourceNoticeService
from app.services.analytics_service import AnalyticsService
from app.services.blacklist_service import BlacklistService, ClientBlacklistedError
from app.services.calendar_service import CalendarService
from app.services.reserva_config_service import ReservaConfigService
from app.services.configuracao_service import ConfiguracaoService
from app.services.pdv_service import PdvService
from app.services.reserva_pricing_service import ReservaPricingService
from app.services.reserva_service import (
    OverbookingError,
    ReservaService,
    ResourceNoticeBlockedError,
    SecurityDepositService,
)

bp = Blueprint("reservas", __name__)


def _is_ajax_request() -> bool:
    """Indica se a requisição espera resposta JSON do servidor."""
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
        or request.path.startswith("/reservas/api/")
    )


@bp.errorhandler(401)
def _reservas_unauthorized(error):
    """Retorna JSON para chamadas AJAX sem sessão ativa."""
    if _is_ajax_request():
        return jsonify({"ok": False, "error": "Sessão expirada. Faça login novamente."}), 401
    return error


@bp.errorhandler(403)
def _reservas_forbidden(error):
    """Retorna JSON para chamadas AJAX sem permissão."""
    if _is_ajax_request():
        return jsonify({"ok": False, "error": "Sem permissão para executar esta ação."}), 403
    return error


# ---------------------------------------------------------------------------
# Master switch — module enabled check
# ---------------------------------------------------------------------------


@bp.before_request
def verificar_modulo_ativo():
    """Retorna 403 se o módulo Reservas estiver desabilitado nas configurações globais.

    Super admins sempre têm acesso para evitar lockout.
    """
    if not ConfiguracaoService.is_module_enabled("reservas"):
        fid = session.get("funcionario_id")
        if fid:
            f = db.session.get(Funcionario, fid)
            if f and f.role and f.role.is_super_admin:
                return
        abort(403)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_ator_id() -> int:
    """Retorna o ID do funcionário logado a partir da sessão Flask."""
    return session["funcionario_id"]


def _actor_context() -> ActorContext:
    """Monta o contexto de autorização do operador autenticado."""
    return ActorContext.from_funcionario_id(_get_ator_id())


def _permissoes_reservas(
    actor: ActorContext,
    source_id: int | None = None,
) -> dict[str, bool | list[int] | None]:
    """Flags granulares de Reservas usadas por rotas e templates."""
    contact_source_ids = ReservaAccessPolicy.visible_source_ids_for_action(actor, "view_contact")
    financial_source_ids = ReservaAccessPolicy.visible_source_ids_for_action(actor, "view_financial")
    payments_source_ids = ReservaAccessPolicy.visible_source_ids_for_action(actor, "view_payments")
    return {
        "can_view_contact": actor.has("reservas", "view_contact", source_id),
        "can_view_financial": actor.has("reservas", "view_financial", source_id),
        "can_view_payments": actor.has("reservas", "view_payments", source_id),
        "can_export": actor.has("reservas", "export", source_id),
        "view_contact_source_ids": contact_source_ids,
        "view_financial_source_ids": financial_source_ids,
        "view_payments_source_ids": payments_source_ids,
    }


def _janela_de_estadia(form) -> dict:
    """Lê a janela de horário da estadia vinda do formulário de categoria.

    Campo vazio vira ``None``: em branco significa "esta categoria não tem
    dimensão de horário", que é o Day Use. Só o Camping preenche, porque lá a
    diária é pró-rata por hora e o site precisa pedir a hora ao cliente.

    Args:
        form: ``request.form`` da tela de categoria.

    Returns:
        Dict pronto para atribuir ao modelo.

    Raises:
        ValueError: Se um horário não for HH:MM ou as horas não forem inteiro > 0.
    """
    def _hora(campo: str):
        bruto = form.get(campo, "").strip()
        return time.fromisoformat(bruto) if bruto else None

    horas_bruto = form.get("min_stay_hours", "").strip()
    horas = int(horas_bruto) if horas_bruto else None
    if horas is not None and horas < 1:
        raise ValueError("Permanência mínima deve ser ao menos 1 hora.")

    return {
        "check_in_from": _hora("check_in_from"),
        "check_in_until": _hora("check_in_until"),
        "check_out_until": _hora("check_out_until"),
        "min_stay_hours": horas,
    }


def _parse_dt(s: str) -> datetime:
    """Converte string YYYY-MM-DD ou YYYY-MM-DDTHH:MM em datetime.

    Args:
        s: String de data/datetime no formato ISO.

    Returns:
        Objeto datetime correspondente (meia-noite se apenas data).

    Raises:
        ValueError: Se o formato for inválido.
    """
    if 'T' in s:
        return datetime.fromisoformat(s)
    d = date.fromisoformat(s)
    return datetime(d.year, d.month, d.day)


def _serializar_reserva(
    r: Reserva,
    *,
    include_items: bool = False,
    actor: ActorContext | None = None,
) -> dict:
    """Converte uma Reserva em dict JSON-serializável."""
    d = {
        "id": r.id,
        "group_id": r.group_id,
        "version": r.version,
        "source_id": r.source_id,
        "category_id": r.category_id,
        "customer_name": r.customer_name,
        "customer_document": r.customer_document,
        "metadata_info": r.metadata_info,
        "origin": r.origin.value if r.origin else None,
        "status": r.status.value if r.status else None,
        "check_in_date": r.check_in_date.isoformat() if r.check_in_date else None,
        "check_out_date": r.check_out_date.isoformat() if r.check_out_date else None,
        "subtotal_amount": str(r.subtotal_amount) if r.subtotal_amount is not None else None,
        "discount_amount": str(r.discount_amount) if r.discount_amount is not None else None,
        "service_fee_amount": str(r.service_fee_amount) if r.service_fee_amount is not None else None,
        "total_amount": str(r.total_amount) if r.total_amount is not None else None,
        "payment_data": r.payment_data,
        "pix_data": r.pix_data,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "criado_em": r.criado_em.isoformat() if r.criado_em else None,
    }
    if include_items:
        d["items"] = [
            {
                "id": link.id,
                "item_id": link.item_id,
                "item_name": link.item.name if link.item else None,
                "quantity": link.quantity,
                "locked_price": str(link.locked_price),
            }
            for link in r.items
            if link.deleted_at is None
        ]
    if actor is not None:
        d = ReservaAccessPolicy.redact_dict(actor, d)
    return d


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.route("/", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def index():
    """Exibe o dashboard principal do módulo Reservas.

    Suporta dois modos de visualização via parâmetro `view`:
    - 'semanal' (padrão): grade 7 dias com navegação por semanas
    - 'lista': tabela paginada com busca universal e filtros por status/categoria

    Query params:
        week_offset (int): Deslocamento em semanas (somente view=semanal, padrão 0).
        view (str): Modo de visualização — 'semanal' (padrão) ou 'lista'.
        q (str): Busca livre por nome, CPF ou telefone (somente view=lista).
        status (str): Filtro por status — PENDING|PAID|CANCELED|COMPLETED (lista).
        categoria_id (int): Filtro por categoria (somente view=lista).

    Returns:
        Renderização de reservas/dashboard.html.
    """
    from sqlalchemy import func

    actor = _actor_context()
    perms_reservas = _permissoes_reservas(actor)
    view = request.args.get("view", "semanal")

    # ── Métricas globais ──────────────────────────────────────────────────────
    visible_source_ids = ReservaAccessPolicy.visible_source_ids(actor)
    total_sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    )
    if visible_source_ids is not None:
        total_sources = total_sources.filter(ReservaSource.id.in_(visible_source_ids))
    total_sources = total_sources.count()

    status_query = db.session.query(
        Reserva.status,
        func.count(Reserva.id).label("count")
    ).filter(
        Reserva.deleted_at.is_(None),
        Reserva.status != ReservaStatus.ARCHIVED_VERSION
    )
    status_counts = (
        ReservaAccessPolicy.filter_query(status_query, actor)
        .group_by(Reserva.status)
        .all()
    )

    status_dict = {status.value: count for status, count in status_counts}
    total_reservas = sum(status_dict.values())

    total_items = db.session.query(ReservaItem).filter(
        ReservaItem.deleted_at.is_(None)
    ).count()

    # ── Visão de Lista ────────────────────────────────────────────────────────
    reservas_lista = []
    lista_total = 0
    categorias = []
    q_lista = ""
    status_lista = ""
    categoria_id_lista = None
    sort_by_lista = "criado_em"
    sort_dir_lista = "desc"

    if view == "lista":
        q_lista = request.args.get("q", "").strip()
        status_lista = request.args.get("status", "").strip()
        categoria_id_lista = request.args.get("categoria_id", None, type=int)
        sort_by_lista = request.args.get("sort", "criado_em").strip()
        sort_dir_lista = request.args.get("dir", "desc").strip()
        if sort_dir_lista not in ("asc", "desc"):
            sort_dir_lista = "desc"

        try:
            reservas_lista, lista_total = ReservaService.listar(
                q=q_lista or None,
                status=status_lista or None,
                categoria_id=categoria_id_lista,
                per_page=50,
                sort_by=sort_by_lista,
                sort_dir=sort_dir_lista,
                actor=actor,
            )
        except ValueError:
            reservas_lista, lista_total = [], 0

        categorias = db.session.query(ReservaCategory).filter(
            ReservaCategory.deleted_at.is_(None)
        ).order_by(ReservaCategory.name).all()

    # ── Visão Semanal ─────────────────────────────────────────────────────────
    week_offset = request.args.get("week_offset", 0, type=int)
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)
    # Limites datetime para comparação correta com colunas check_in/check_out (DateTime).
    # date comparado contra DateTime no MySQL é tratado como 00:00:00, o que exclui
    # reservas com check-in após meia-noite no domingo (ex: 07:02 > 00:00 → fora do filtro).
    monday_dt = datetime(monday.year, monday.month, monday.day, 0, 0, 0)
    sunday_dt = datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59)

    weekly_reservas = []
    days_data = []
    week_total_count = 0
    week_total_revenue = Decimal("0")
    week_total_tickets_novos = 0

    # Carrega configuracao dinamica de status para labels e cores
    status_configs = db.session.query(ReservaStatusConfig).all()
    status_config_map = {s.internal_name: s for s in status_configs}

    # Statuses que devem aparecer na visao semanal (conforme show_on_dashboard do admin).
    # Usa strings diretamente para incluir status customizados criados via ReservaStatusConfig.
    semanal_statuses = [
        cfg.internal_name
        for cfg in status_configs
        if cfg.show_on_dashboard
    ] or ["PAID", "COMPLETED"]  # fallback de seguranca

    if view == "semanal":
        # Busca reservas que se sobreponham à semana.
        # Reservas sem check_out (day use, etc.) existem apenas no dia do check_in —
        # portanto só entram se check_in estiver dentro da semana.
        # Reservas com check_out entram se cobrirem qualquer dia da semana.
        weekly_query = db.session.query(Reserva).options(
            selectinload(Reserva.items).options(
                selectinload(ReservaItemLink.item),
                selectinload(ReservaItemLink.resource),
            )
        ).filter(
            Reserva.deleted_at.is_(None),
            Reserva.status.in_(semanal_statuses),
            Reserva.check_in_date <= sunday_dt,
            db.or_(
                db.and_(
                    Reserva.check_out_date.is_(None),
                    Reserva.check_in_date >= monday_dt,
                ),
                Reserva.check_out_date >= monday_dt,
            ),
        )
        weekly_reservas = (
            ReservaAccessPolicy.filter_query(weekly_query, actor)
            .order_by(Reserva.check_in_date, Reserva.criado_em)
            .all()
        )

        # Pré-carrega category name para evitar N+1 nos mini-cards
        _cat_cache: dict[int, str] = {}
        for r in weekly_reservas:
            if r.category_id and r.category_id not in _cat_cache:
                cat = db.session.get(ReservaCategory, r.category_id)
                _cat_cache[r.category_id] = cat.name if cat else "—"

        # Set de reserva_ids com caução ativa (qualquer estado, exceto devolvida/deletada)
        _weekly_ids = [r.id for r in weekly_reservas]
        _deposit_reserva_ids: set[int] = set()
        if _weekly_ids:
            _dep_rows = (
                db.session.query(SecurityDeposit.reserva_id)
                .filter(
                    SecurityDeposit.reserva_id.in_(_weekly_ids),
                    SecurityDeposit.is_refunded.is_(False),
                    SecurityDeposit.deleted_at.is_(None),
                )
                .distinct()
                .all()
            )
            _deposit_reserva_ids = {row[0] for row in _dep_rows}

        # Pré-calcula totais por reserva para evitar recálculo por dia.
        # Usa total_amount (snapshot server-side com descontos e taxa de serviço).
        # Fallback para soma de locked_price (sem multiplicar por quantity, pois
        # locked_price já é o total proporcional do item, não o preço unitário).
        _reserva_totals: dict[int, Decimal] = {}
        for r in weekly_reservas:
            if r.total_amount is not None:
                _reserva_totals[r.id] = Decimal(str(r.total_amount))
            else:
                _reserva_totals[r.id] = sum(
                    Decimal(str(link.locked_price or 0))
                    for link in r.items
                    if link.deleted_at is None
                )

        # Agrega por dia da semana (0=Seg … 6=Dom)
        # Reservas multi-dia aparecem em cada dia que cobrem, com indicador visual
        for i in range(7):
            day_date = monday + timedelta(days=i)
            day_revenue = Decimal("0")
            enriched = []
            for r in weekly_reservas:
                checkin = r.check_in_date.date() if r.check_in_date else None
                checkout = r.check_out_date.date() if r.check_out_date else None
                if checkin is None:
                    continue
                # Determina se esta reserva cobre o dia atual.
                # Sem check_out (day use, etc.) = apenas o dia do check_in.
                spans_day = (
                    checkin == day_date
                    or (checkout is not None and checkin < day_date and checkout >= day_date)
                )
                if not spans_day:
                    continue
                # Classifica o tipo do dia para indicador visual
                if checkin == day_date and (checkout is None or checkout == day_date):
                    day_type = "unico"      # entrada e saída no mesmo dia
                elif checkin == day_date:
                    day_type = "checkin"
                elif checkout is not None and checkout == day_date:
                    day_type = "checkout"
                else:
                    day_type = "estadia"    # dia intermediário
                total = _reserva_totals[r.id]
                # Soma receita apenas no dia de check-in para evitar dupla contagem
                if day_type in ("checkin", "unico") and actor.has(
                    "reservas",
                    "view_financial",
                    r.source_id,
                ):
                    day_revenue += total
                enriched.append({
                    "reserva": r,
                    "total": total,
                    "category_name": _cat_cache.get(r.category_id, "—") if r.category_id else "—",
                    "has_active_deposit": r.id in _deposit_reserva_ids,
                    "is_pdv": bool((r.metadata_info or {}).get("origem_pdv")),
                    "day_type": day_type,
                    "day_group": "checkins" if day_type in ("checkin", "unico") else day_type,
                })
            # Conta apenas check-ins do dia (não repete estadias no contador)
            day_checkins = sum(
                1 for e in enriched if e["day_type"] in ("checkin", "unico")
            )
            # Contadores de tickets de entrada:
            # tickets_novos — apenas check-ins do dia (novas entradas)
            # tickets_total — todas as reservas presentes no dia (pessoas no local)
            tickets_novos = sum(
                link.quantity
                for e in enriched
                if e["day_type"] in ("checkin", "unico")
                for link in e["reserva"].items
                if link.deleted_at is None
                and link.item is not None
                and link.item.is_entry_ticket
            )
            tickets_total = sum(
                link.quantity
                for e in enriched
                for link in e["reserva"].items
                if link.deleted_at is None
                and link.item is not None
                and link.item.is_entry_ticket
            )
            week_total_count += day_checkins
            week_total_revenue += day_revenue
            week_total_tickets_novos += tickets_novos
            days_data.append({
                "date": day_date,
                "is_today": day_date == today,
                "reservas": enriched,
                "count": day_checkins,
                "revenue": day_revenue,
                "tickets_novos": tickets_novos,
                "tickets_total": tickets_total,
            })
    return render_template(
        "reservas/dashboard.html",
        total_sources=total_sources,
        total_reservas=total_reservas,
        total_items=total_items,
        status_dict=status_dict,
        status_configs=status_configs,
        status_config_map=status_config_map,
        # modo de visão
        view=view,
        # visão semanal
        days_data=days_data,
        monday=monday,
        sunday=sunday,
        week_offset=week_offset,
        week_total_count=week_total_count,
        week_total_revenue=week_total_revenue,
        week_total_tickets_novos=week_total_tickets_novos,
        today=today,
        # visão de lista
        reservas_lista=reservas_lista,
        lista_total=lista_total,
        categorias=categorias,
        q_lista=q_lista,
        status_lista=status_lista,
        categoria_id_lista=categoria_id_lista,
        sort_by_lista=sort_by_lista,
        sort_dir_lista=sort_dir_lista,
        **perms_reservas,
    )


@bp.route("/sources", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def listar_fontes():
    """Lista todas as fontes de reserva (venues/locais).

    Mostra nome, número de categorias e data de criação de cada fonte ativa.

    Returns:
        Renderização de reservas/sources.html.
    """
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    return render_template(
        "reservas/sources.html",
        sources=sources,
    )


@bp.route("/<int:reserva_id>", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def detalhe(reserva_id: int):
    """Retorna detalhes de uma reserva específica.

    Args:
        reserva_id: ID da reserva a consultar.

    Returns:
        Renderização de reservas/detalhe.html ou erro 404.
    """
    reserva = db.session.get(Reserva, reserva_id)

    if not reserva or reserva.deleted_at is not None:
        return render_template(
            "reservas/erro.html",
            mensagem="Reserva não encontrada.",
            titulo="Erro 404"
        ), 404

    actor = _actor_context()
    if not ReservaAccessPolicy.can_view(actor, reserva):
        abort(403)
    perms_reservas = _permissoes_reservas(actor, reserva.source_id)

    # Registra primeira visualização (idempotente — só muda se ainda NULL)
    ReservaService.marcar_visualizada(reserva_id)

    # Sumário financeiro multi-pagamento
    from app.models.payment import PaymentTransaction, TransactionStatus

    total_amount = float(reserva.total_amount or 0)

    # Busca todas as transações do grupo (todas as versões da reserva)
    transactions = (
        db.session.query(PaymentTransaction)
        .filter(PaymentTransaction.reserva_group_id == reserva.group_id)
        .order_by(PaymentTransaction.created_at.asc())
        .all()
    )

    # total_paid = aprovados − extornados (ledger líquido)
    total_paid = float(sum(
        t.amount for t in transactions if t.status == TransactionStatus.APPROVED
    )) - float(sum(
        t.amount for t in transactions if t.status == TransactionStatus.REFUNDED
    ))
    total_paid = max(0.0, total_paid)

    # total_refunded — para exibição no sumário financeiro
    total_refunded = float(sum(
        t.amount for t in transactions if t.status == TransactionStatus.REFUNDED
    ))

    # Fallback legado se não há transações no novo modelo
    if not transactions:
        total_refunded = 0.0
        payment_data = reserva.payment_data or {}
        if reserva.status.value == "PAID" and total_amount > 0:
            total_paid = total_amount
        else:
            total_paid = float(payment_data.get("amount_paid", 0) or 0)

    # Cauções não-pagas incrementam o saldo devedor separadamente do total_amount
    pending_deposits = sum(
        float(d.amount)
        for d in reserva.deposits
        if not d.is_paid and not d.is_refunded
    )
    pending_balance = round(max(0.0, total_amount - total_paid) + pending_deposits, 2)

    # Ativos físicos do source para o select de "Adicionar Caução".
    # Garante que todo ReservaResource dos itens desta reserva tenha um
    # PhysicalAsset correspondente (criação automática idempotente).
    ativos_disponiveis = []
    if reserva.source_id:
        from app.models.reservas import AssetStatus

        # Coleta recursos físicos escolhidos nos itens desta reserva
        recursos_da_reserva = {
            link.resource_id: link.resource
            for link in reserva.items
            if link.resource_id and not link.deleted_at and link.resource
        }

        # Cria PhysicalAsset para cada recurso que ainda não tiver um
        for resource_id, resource in recursos_da_reserva.items():
            existe = (
                db.session.query(PhysicalAsset)
                .filter(
                    PhysicalAsset.resource_id == resource_id,
                    PhysicalAsset.deleted_at.is_(None),
                )
                .first()
            )
            if not existe:
                novo_ativo = PhysicalAsset(
                    source_id=reserva.source_id,
                    resource_id=resource_id,
                    name=resource.name,
                    asset_type=resource.group.name if resource.group and not resource.group.deleted_at else None,
                    current_status=AssetStatus.AVAILABLE,
                )
                db.session.add(novo_ativo)

        db.session.commit()

        ativos_disponiveis = (
            db.session.query(PhysicalAsset)
            .filter(
                PhysicalAsset.source_id == reserva.source_id,
                PhysicalAsset.deleted_at.is_(None),
            )
            .order_by(PhysicalAsset.name)
            .all()
        )

    return render_template(
        "reservas/detalhe.html",
        reserva=reserva,
        total_amount=total_amount,
        total_paid=total_paid,
        total_refunded=total_refunded,
        pending_balance=pending_balance,
        pending_deposits=pending_deposits,
        transactions=transactions,
        ativos_disponiveis=ativos_disponiveis,
        **perms_reservas,
    )


@bp.route("/<int:reserva_id>/nota-relatorio", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def salvar_nota_relatorio(reserva_id: int):
    """Salva a nota de relatório de uma reserva (AJAX, sem criar nova versão).

    Form fields:
        nota: str — texto da nota (vazio para apagar)

    Returns:
        200 JSON {"ok": true} em caso de sucesso.
        400 JSON {"erro": "..."} em caso de falha.
    """
    nota = request.form.get("nota", "").strip()
    try:
        ReservaService.salvar_nota_relatorio(reserva_id, nota, ator_id=_get_ator_id())
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/<int:reserva_id>/notas", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def salvar_notas(reserva_id: int):
    """Salva anotações do operador em uma reserva (AJAX, sem criar nova versão).

    Form fields:
        notas: str — texto das anotações (vazio para apagar)

    Returns:
        200 JSON {"ok": true} em caso de sucesso.
        400 JSON {"erro": "..."} em caso de falha.
    """
    notas = request.form.get("notas", "").strip()
    try:
        ReservaService.salvar_notas(reserva_id, notas, ator_id=_get_ator_id())
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Cauções (Security Deposits)
# ---------------------------------------------------------------------------


@bp.route("/<int:reserva_id>/caucoes/adicionar", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def caucao_adicionar(reserva_id: int):
    """Adiciona uma nova caução a uma reserva.

    Form fields:
        amount (str): Valor da caução em Reais (obrigatório, positivo).
        description (str): Descrição livre (opcional).
        linked_asset_id (int): ID do PhysicalAsset vinculado (opcional).

    Returns:
        Redirect para detalhe da reserva em caso de sucesso.
        Redirect com flash de erro em caso de falha.
    """
    try:
        amount = float(request.form.get("amount", 0) or 0)
        description = request.form.get("description", "").strip() or None
        linked_asset_id = request.form.get("linked_asset_id") or None
        if linked_asset_id:
            linked_asset_id = int(linked_asset_id)

        SecurityDepositService.criar(
            reserva_id=reserva_id,
            amount=amount,
            description=description,
            linked_asset_id=linked_asset_id,
            ator_id=_get_ator_id(),
        )
        flash("Caução adicionada com sucesso.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.detalhe", reserva_id=reserva_id))


@bp.route("/caucoes/<int:deposit_id>/pagar", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def caucao_pagar(deposit_id: int):
    """Registra o recebimento do valor de caução do cliente.

    Path args:
        deposit_id: ID da SecurityDeposit a marcar como paga.

    Returns:
        Redirect para detalhe da reserva em caso de sucesso.
        Redirect com flash de erro em caso de falha.
    """
    reserva_id = request.form.get("reserva_id", type=int)
    try:
        SecurityDepositService.pagar(deposit_id=deposit_id, ator_id=_get_ator_id())
        flash("Caução registrada como paga.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.detalhe", reserva_id=reserva_id))


@bp.route("/caucoes/<int:deposit_id>/devolver", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def caucao_devolver(deposit_id: int):
    """Registra a devolução da caução ao cliente.

    Path args:
        deposit_id: ID da SecurityDeposit a marcar como devolvida.

    Returns:
        Redirect para detalhe da reserva em caso de sucesso.
        Redirect com flash de erro em caso de falha.
    """
    reserva_id = request.form.get("reserva_id", type=int)
    try:
        SecurityDepositService.devolver(deposit_id=deposit_id, ator_id=_get_ator_id())
        flash("Caução devolvida ao cliente.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.detalhe", reserva_id=reserva_id))


@bp.route("/caucoes/<int:deposit_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def caucao_deletar(deposit_id: int):
    """Remove logicamente uma caução não-paga.

    Path args:
        deposit_id: ID da SecurityDeposit a deletar.

    Returns:
        Redirect para detalhe da reserva em caso de sucesso.
        Redirect com flash de erro em caso de falha.
    """
    reserva_id = request.form.get("reserva_id", type=int)
    try:
        SecurityDepositService.deletar(deposit_id=deposit_id, ator_id=_get_ator_id())
        flash("Caução removida.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.detalhe", reserva_id=reserva_id))


# ---------------------------------------------------------------------------
# JSON API Routes (Headless)
# ---------------------------------------------------------------------------


@bp.route("/api/", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def api_listar():
    """Lista reservas ativas com filtros e paginação (JSON API).

    Query params:
        source_id: int — filtra por ID da fonte
        categoria_id: int — filtra por ID de categoria
        status: str — filtra por status (PENDING, PAID, CANCELED, COMPLETED)
        q: str — busca universal (nome, CPF ou telefone do cliente)
        page: int — número da página (padrão 1)
        per_page: int — itens por página (padrão 20)

    Returns:
        200 JSON com lista de reservas, total e info de paginação
        400 Se status inválido
    """
    source_id = request.args.get("source_id", type=int)
    categoria_id = request.args.get("categoria_id", type=int)
    status = request.args.get("status") or None
    q = (request.args.get("q") or "").strip() or None
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    actor = _actor_context()

    try:
        reservas, total = ReservaService.listar(
            source_id=source_id,
            categoria_id=categoria_id,
            status=status,
            q=q,
            page=page,
            per_page=per_page,
            actor=actor,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "reservas": [_serializar_reserva(r, actor=actor) for r in reservas],
    }), 200


@bp.route("/api/sources", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def api_listar_fontes():
    """Lista fontes (venues) ativas com categorias aninhadas (JSON API).

    Returns:
        200 JSON com lista de fontes
    """
    sources = ReservaService.listar_fontes()

    return jsonify({
        "sources": [
            {
                "id": s.id,
                "name": s.name,
                "is_active": s.is_active,
                "criado_em": s.criado_em.isoformat() if s.criado_em else None,
                "categories": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "criado_em": c.criado_em.isoformat() if c.criado_em else None,
                    }
                    for c in s.categories
                    if c.deleted_at is None
                ],
            }
            for s in sources
        ]
    }), 200


@bp.route("/api/disponibilidade", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def api_disponibilidade():
    """Calcula disponibilidade de itens em uma categoria para um período (JSON API).

    Query params (todos obrigatórios):
        category_id: int — ID da categoria
        check_in: str — data de início (YYYY-MM-DD)
        check_out: str — data de término (YYYY-MM-DD)

    Returns:
        200 JSON com lista de itens e disponibilidade
        400 Se parâmetros obrigatórios ausentes ou datas inválidas
    """
    category_id = request.args.get("category_id", type=int)
    check_in_str = request.args.get("check_in")
    check_out_str = request.args.get("check_out")

    # Validate required params
    if not category_id:
        return jsonify({"erro": "Parâmetro 'category_id' é obrigatório."}), 400
    if not check_in_str:
        return jsonify({"erro": "Parâmetro 'check_in' é obrigatório."}), 400
    if not check_out_str:
        return jsonify({"erro": "Parâmetro 'check_out' é obrigatório."}), 400

    # Parse dates
    try:
        check_in = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str)
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    try:
        items = ReservaService.verificar_disponibilidade(
            category_id=category_id,
            check_in=check_in,
            check_out=check_out,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    return jsonify({
        "category_id": category_id,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "items": items,
    }), 200


@bp.route("/api/recursos/disponibilidade", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def api_disponibilidade_recursos():
    """Retorna disponibilidade de ativos físicos (ReservaResource) para um período (JSON API).

    Usado pelo accordion de seleção de recurso no formulário de reserva do atendente.
    Retorna cada ativo com status disponível/indisponível, agrupado por ReservaGroup.

    Query params (todos obrigatórios):
        source_id: int — ID da fonte (venue)
        check_in: str — data de início (YYYY-MM-DD)
        check_out: str — data de término (YYYY-MM-DD)

    Returns:
        200 JSON com lista de recursos e disponibilidade
        400 Se parâmetros ausentes ou datas inválidas
    """
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


@bp.route("/api/<int:reserva_id>", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def api_detalhe(reserva_id: int):
    """Retorna detalhes completos de uma reserva (JSON API).

    Args:
        reserva_id: ID da reserva a consultar

    Returns:
        200 JSON com detalhes da reserva e itens
        404 Se reserva não encontrada
    """
    try:
        reserva = ReservaService.detalhe(reserva_id)
    except ValueError:
        return jsonify({"erro": "Reserva não encontrada."}), 404
    actor = _actor_context()
    if not ReservaAccessPolicy.can_view(actor, reserva):
        abort(403)

    return jsonify({
        "reserva": _serializar_reserva(reserva, include_items=True, actor=actor),
    }), 200


@bp.route("/api/", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def api_criar():
    """Cria uma nova reserva (JSON API).

    Request body (JSON):
        source_name: str — nome da fonte (cria se não existir)
        category_name: str — nome da categoria
        customer_name: str — nome do cliente
        customer_document: str — CPF ou CNPJ (opcional)
        origin: str — "WEB", "BALCAO" ou "BOT"
        check_in_date: str — data de check-in (YYYY-MM-DD)
        check_out_date: str — data de check-out (YYYY-MM-DD, opcional)
        items: list — [{"item_id": int, "quantity": int, "price_override": str}]

    Returns:
        201 JSON com reserva criada e expires_at setado para 15min
        400 Se validação falhar
    """
    data = request.get_json(silent=True) or {}

    # Validate required fields
    required = ["source_name", "category_name", "customer_name", "origin",
                "check_in_date", "items"]
    for field in required:
        if not data.get(field):
            return jsonify({"erro": f"Campo obrigatório ausente: '{field}'."}), 400

    # Parse dates (aceita YYYY-MM-DD e YYYY-MM-DDTHH:MM)
    try:
        check_in = _parse_dt(data["check_in_date"])
        check_out = (
            _parse_dt(data["check_out_date"])
            if data.get("check_out_date")
            else None
        )
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD ou YYYY-MM-DDTHH:MM."}), 400

    # Create reservation
    try:
        reserva = ReservaService.criar(
            source_name=data["source_name"],
            category_name=data["category_name"],
            customer_name=data["customer_name"],
            customer_document=data.get("customer_document"),
            origin=data["origin"],
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=data["items"],
            ator_id=_get_ator_id(),
        )
    except ClientBlacklistedError as exc:
        public_message = "Sem resposta do servidor. Tente novamente em instantes."
        return jsonify({
            "status": "error",
            "code": exc.code,
            "erro": public_message,
            "message": public_message,
        }), 403
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    return jsonify({
        "reserva": _serializar_reserva(reserva, include_items=True),
    }), 201


@bp.route("/api/<int:reserva_id>", methods=["PATCH"])
@login_required
@require_permission("reservas", "edit")
def api_editar(reserva_id: int):
    """Atualiza uma reserva criando nova versão imutável (JSON API).

    Implementa o padrão de versionamento: a versão anterior é arquivada com
    status=ARCHIVED_VERSION e uma nova linha é inserida com version+1.

    Request body (JSON):
        items: list — [{"item_id": int, "quantity": int, "price_override": str}]
        metadata_info: dict — metadados adicionais (opcional)

    Returns:
        200 JSON com a nova versão da reserva
        400 Se validação ou divergência de preço
        404 Se reserva não encontrada
    """
    data = request.get_json(silent=True) or {}

    try:
        nova = ReservaService.atualizar(
            reserva_id,
            items_payload=data.get("items", []),
            metadata_info=data.get("metadata_info"),
            ator_id=_get_ator_id(),
        )
    except ValueError as exc:
        msg = str(exc)
        status_code = 404 if "não encontrada" in msg else 400
        return jsonify({"erro": msg}), status_code

    return jsonify({
        "reserva": _serializar_reserva(nova, include_items=True),
    }), 200


@bp.route("/api/<int:reserva_id>/status", methods=["PATCH"])
@login_required
@require_permission("reservas", "edit")
def api_atualizar_status(reserva_id: int):
    """Atualiza apenas o status de uma reserva criando nova versão (JSON API).

    Request body (JSON):
        status: str — novo status (PENDING, PAID, COMPLETED, CANCELED)

    Returns:
        200 JSON com a nova versão da reserva
        400 Se status inválido
        404 Se reserva não encontrada
    """
    data = request.get_json(silent=True) or {}
    novo_status = data.get("status", "").strip()

    if not novo_status:
        return jsonify({"erro": "Status é obrigatório"}), 400

    try:
        nova = ReservaService.atualizar(
            reserva_id,
            items_payload=[],
            status=novo_status,
            ator_id=_get_ator_id(),
        )
    except OverbookingError as exc:
        return jsonify({
            "erro": str(exc),
            "overbooking": True,
            "resource_name": exc.resource_name,
            "conflitos": exc.conflitos,
        }), 409
    except ValueError as exc:
        msg = str(exc)
        status_code = 404 if "não encontrada" in msg else 400
        return jsonify({"erro": msg}), status_code

    return jsonify({
        "reserva": _serializar_reserva(nova, include_items=False),
        "mensagem": f"Status atualizado para {novo_status}",
    }), 200


@bp.route("/api/<int:reserva_id>/cancelar", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def api_cancelar(reserva_id: int):
    """Cancela uma reserva criando nova versão com status=CANCELED (JSON API).

    Request body (JSON, opcional):
        motivo: str — motivo do cancelamento (armazenado em metadata_info)

    Returns:
        200 JSON com a versão cancelada
        400 Se reserva já cancelada ou inválida
        404 Se reserva não encontrada
    """
    data = request.get_json(silent=True) or {}

    try:
        cancelada = ReservaService.cancelar(
            reserva_id,
            motivo=data.get("motivo"),
            ator_id=_get_ator_id(),
        )
    except ValueError as exc:
        msg = str(exc)
        status_code = 404 if "não encontrada" in msg else 400
        return jsonify({"erro": msg}), status_code

    return jsonify({
        "reserva": _serializar_reserva(cancelada, include_items=False),
    }), 200


@bp.route("/api/<int:reserva_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "delete")
def api_deletar(reserva_id: int):
    """Soft-deleta uma reserva (JSON API). Nunca remove a linha do banco.

    Define deleted_at em todas as versões do mesmo group_id e registra
    AuditLog com snapshot da versão ativa.

    Returns:
        200 JSON com mensagem de confirmação
        404 Se reserva não encontrada ou já deletada
    """
    try:
        ReservaService.deletar(reserva_id, ator_id=_get_ator_id())
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 404

    return jsonify({"mensagem": "Reserva removida com sucesso."}), 200


# ---------------------------------------------------------------------------
# Public API Routes (No Auth Required)
# ---------------------------------------------------------------------------


@bp.route("/api/public/simular", methods=["POST"])
def simular_preco():
    """Simula cálculo de preço de reserva (public, sem autenticação).

    Endpoint público para frontends calcularem totais em tempo real durante
    construção do carrinho. Não salva nada no banco — apenas calcula.

    Request body (JSON):
        source_id: int — ID da fonte (venue)
        check_in_date: str — data de check-in (YYYY-MM-DD)
        check_out_date: str — data de check-out (YYYY-MM-DD)
        items: list — [{"item_id": int, "quantity": int}]

    Returns:
        200 JSON com breakdown detalhado de preços:
            {
                "items_breakdown": [
                    {"item_id": 1, "item_name": "Inteira", "quantity": 2,
                     "unit_price": 100.0, "billing_type": "FIXED", "item_total": 200.0},
                    ...
                ],
                "subtotal": 500.0,
                "discount_amount": 50.0,
                "discount_details": {
                    "early_bird": {"name": "Early Bird", "percent": 10, ...}
                },
                "service_fee": 40.5,
                "service_fee_details": {"percent": 8.1, "amount": 40.5},
                "total": 490.5
            }
        400 Se validação falhar (missing fields, invalid dates, etc.)
    """
    data = request.get_json(silent=True) or {}

    # Validar campos obrigatórios
    required_fields = ["source_id", "check_in_date", "check_out_date", "items"]
    for field in required_fields:
        if field not in data:
            return jsonify({
                "erro": f"Campo obrigatório ausente: '{field}'."
            }), 400

    # Parse source_id
    try:
        source_id = int(data["source_id"])
    except (ValueError, TypeError):
        return jsonify({
            "erro": "source_id deve ser um número inteiro."
        }), 400

    # Parse dates (aceita YYYY-MM-DD e YYYY-MM-DDTHH:MM)
    try:
        check_in_date = _parse_dt(data["check_in_date"])
        check_out_date = _parse_dt(data["check_out_date"])
    except ValueError:
        return jsonify({
            "erro": "Formato de data inválido. Use YYYY-MM-DD ou YYYY-MM-DDTHH:MM."
        }), 400

    # Validar items
    if not isinstance(data["items"], list):
        return jsonify({
            "erro": "items deve ser uma lista."
        }), 400

    if not data["items"]:
        return jsonify({
            "erro": "items não pode estar vazio."
        }), 400

    # Validar cada item
    for item in data["items"]:
        if not isinstance(item, dict):
            return jsonify({
                "erro": "Cada item deve ser um objeto JSON."
            }), 400

        if "item_id" not in item:
            return jsonify({
                "erro": "Cada item deve conter 'item_id'."
            }), 400

        try:
            int(item["item_id"])
        except (ValueError, TypeError):
            return jsonify({
                "erro": f"item_id deve ser um número inteiro."
            }), 400

        # quantity é opcional, padrão 1
        quantity = item.get("quantity", 1)
        try:
            int(quantity)
        except (ValueError, TypeError):
            return jsonify({
                "erro": f"quantity deve ser um número inteiro."
            }), 400

    # category_id opcional — quando fornecido, regras de desconto específicas
    # da categoria têm prioridade sobre regras da fonte.
    category_id = None
    if "category_id" in data:
        try:
            category_id = int(data["category_id"])
        except (ValueError, TypeError):
            return jsonify({"erro": "category_id deve ser um número inteiro."}), 400

    # manual_discount_percent opcional — desconto manual em % (operador de balcão)
    # Aceita também o campo legado manual_discount_amount por compatibilidade retroativa.
    manual_discount_percent = 0.0
    if "manual_discount_percent" in data:
        try:
            manual_discount_percent = float(data["manual_discount_percent"])
            if manual_discount_percent < 0:
                return jsonify({"erro": "manual_discount_percent não pode ser negativo."}), 400
            if manual_discount_percent > 100:
                return jsonify({"erro": "manual_discount_percent não pode ser maior que 100."}), 400
        except (ValueError, TypeError):
            return jsonify({"erro": "manual_discount_percent deve ser um número."}), 400

    # Calcular preço
    try:
        result = ReservaPricingService.calculate(
            source_id=source_id,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            items=data["items"],
            category_id=category_id,
            manual_discount_percent=manual_discount_percent,
        )
    except ValueError as exc:
        return jsonify({
            "erro": str(exc)
        }), 400
    except Exception as exc:
        # Log unexpected errors
        import sys
        print(f"Error in simular_preco: {exc}", file=sys.stderr)
        return jsonify({
            "erro": "Erro ao calcular preço. Tente novamente mais tarde."
        }), 500

    return jsonify(result), 200


@bp.route("/api/public/desconto-antecipado", methods=["GET"])
def api_public_desconto_antecipado():
    """Consulta o desconto por reserva antecipada aplicável a uma data e fonte (público, sem autenticação).

    Permite que frontends exibam imediatamente o desconto disponível quando o
    cliente seleciona a data de check-in, antes de montar o carrinho completo.

    A prioridade de desconto é: CalendarDiscount (desconto de data específica) >
    AdvanceDiscountRule (early bird global). Quando check_out_date é fornecido,
    stay_days é calculado e usado para qualificação dos tiers de estadia em ambos
    os tipos de desconto.

    Query parameters:
        source_id (int):       ID da ReservaSource. Obrigatório.
        check_in_date (str):   Data de check-in no formato YYYY-MM-DD. Obrigatório.
        check_out_date (str):  Data de check-out no formato YYYY-MM-DD. Opcional — quando
                               fornecido, ativa qualificação por número de noites nos tiers.
        category_id (int):     ID da ReservaCategory. Opcional — refina a busca para
                               regras específicas da categoria.

    Returns:
        200 JSON:
            {
                "aplicado": true,
                "percentual": 13.6,
                "dias_antecedencia": 14,
                "dias_minimos": 2,
                "regra_id": null,
                "origem": "calendario"
            }
            ou, se nenhum desconto aplicável:
            {
                "aplicado": false,
                "percentual": 0,
                "dias_antecedencia": 12,
                "dias_minimos": null,
                "regra_id": null,
                "origem": null
            }
        400 Se source_id ou check_in_date estiverem ausentes ou inválidos.
    """
    from datetime import date as _date

    source_id_raw   = request.args.get("source_id")
    check_in_raw    = request.args.get("check_in_date")
    check_out_raw   = request.args.get("check_out_date")
    category_raw    = request.args.get("category_id")

    if not source_id_raw or not check_in_raw:
        return jsonify({"erro": "source_id e check_in_date são obrigatórios."}), 400

    try:
        source_id = int(source_id_raw)
    except ValueError:
        return jsonify({"erro": "source_id inválido."}), 400

    try:
        check_in_date = _date.fromisoformat(check_in_raw)
    except ValueError:
        return jsonify({"erro": "check_in_date inválido. Use o formato YYYY-MM-DD."}), 400

    check_out_date: _date | None = None
    if check_out_raw:
        try:
            check_out_date = _date.fromisoformat(check_out_raw)
        except ValueError:
            return jsonify({"erro": "check_out_date inválido. Use o formato YYYY-MM-DD."}), 400

    category_id: int | None = None
    if category_raw:
        try:
            category_id = int(category_raw)
        except ValueError:
            return jsonify({"erro": "category_id inválido."}), 400

    days_in_advance = (check_in_date - _date.today()).days
    stay_days = (check_out_date - check_in_date).days if check_out_date else 0

    # CalendarDiscount tem prioridade sobre AdvanceDiscountRule.
    cal = ReservaPricingService._find_calendar_discount(
        source_id=source_id,
        category_id=category_id,
        check_in_date=check_in_date,
        stay_days=stay_days,
    )
    if cal:
        return jsonify({
            "aplicado": True,
            "percentual": cal["percent"],
            "dias_antecedencia": days_in_advance,
            "dias_minimos": cal["advance_tier_min_days"],
            "regra_id": None,
            "origem": "calendario",
        }), 200

    discount = ReservaPricingService._find_best_advance_discount(
        source_id=source_id,
        category_id=category_id,
        check_in_date=check_in_date,
        stay_days=stay_days,
    )

    if discount:
        return jsonify({
            "aplicado": True,
            "percentual": discount["percent"],
            "dias_antecedencia": discount["days_in_advance"],
            "dias_minimos": discount["required_days"],
            "regra_id": discount["rule_id"],
            "origem": "antecedencia",
        }), 200

    return jsonify({
        "aplicado": False,
        "percentual": 0,
        "dias_antecedencia": max(days_in_advance, 0),
        "dias_minimos": None,
        "regra_id": None,
        "origem": None,
    }), 200


@bp.route("/api/public/catalogo", methods=["GET"])
def api_catalogo_publico():
    """Retorna catálogo público de fontes, categorias e itens disponíveis (sem autenticação).

    Endpoint público para exibição a clientes sem necessidade de login.
    Itens com estoque esgotado (resource.stock_quantity = 0) são excluídos
    automaticamente. Quantidades de estoque nunca são expostas na resposta.

    Query params:
        source_id (int, opcional): Filtra por fonte específica pelo ID.
                                   Se omitido, retorna todas as fontes ativas.
                                   Se não-inteiro, retorna 400.
                                   Se não encontrado ou inativo, retorna sources: [].

    Returns:
        200 JSON:
            {
                "sources": [
                    {
                        "id": 1,
                        "name": "Cachoeira",
                        "categories": [
                            {
                                "id": 1,
                                "name": "Day Use",
                                "items": [
                                    {
                                        "id": 1,
                                        "name": "Inteira",
                                        "price": 100.0,
                                        "billing_type": "FIXED",
                                        "requires_deposit": false
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        400 Se source_id não for um inteiro válido.
        500 Em caso de erro inesperado.
    """
    source_id_raw = request.args.get("source_id")
    source_id: int | None = None

    if source_id_raw is not None:
        try:
            source_id = int(source_id_raw)
        except (ValueError, TypeError):
            return jsonify({"erro": "source_id deve ser um número inteiro."}), 400

    try:
        sources = ReservaService.catalogo(source_id=source_id)
    except Exception as exc:
        import sys
        print(f"Error in api_catalogo_publico: {exc}", file=sys.stderr)
        return jsonify({"erro": "Erro ao carregar catálogo. Tente novamente mais tarde."}), 500

    return jsonify({"sources": sources}), 200


# ---------------------------------------------------------------------------
# Admin HTML Routes — Reservation Edit
# ---------------------------------------------------------------------------


@bp.route("/admin/reservas/<int:reserva_id>/editar", methods=["GET"])
@login_required
@require_permission("reservas", "edit")
def admin_editar_reserva_form(reserva_id: int):
    """Exibe o formulário de edição de uma reserva existente.

    Carrega a reserva ativa, todos os itens disponíveis na categoria e
    os itens atualmente vinculados para pré-preencher o formulário.

    Args:
        reserva_id: ID da reserva a editar.

    Returns:
        Renderização de reservas/admin/reserva_edit.html ou abort 404.
    """
    reserva = db.session.get(Reserva, reserva_id)

    if not reserva or reserva.deleted_at is not None:
        abort(404)

    if reserva.status == ReservaStatus.ARCHIVED_VERSION:
        abort(404)

    # Itens disponíveis para a categoria desta reserva (lista plana, usada no JS)
    itens_disponiveis = []
    if reserva.category_id:
        itens_disponiveis = db.session.query(ReservaItem).filter(
            ReservaItem.category_id == reserva.category_id,
            ReservaItem.deleted_at.is_(None),
        ).order_by(ReservaItem.name).all()

    # Mapa item_id → (quantidade, resource_id) atuais (apenas links ativos)
    qtd_atual: dict[int, int] = {}
    resource_atual: dict[int, int] = {}
    resource_atual_nome: dict[int, str] = {}
    for link in reserva.items:
        if link.deleted_at is None and link.item_id is not None:
            qtd_atual[link.item_id] = link.quantity
            if link.resource_id is not None:
                resource_atual[link.item_id] = link.resource_id
                if link.resource and not link.resource.deleted_at:
                    resource_atual_nome[link.item_id] = link.resource.name

    # ── Árvore unificada: grupos → itens + recursos (filtrado por grupo) ──
    # Cada nó: {group, items, resources_list, children}
    # Itens e recursos partilham group_id — cada item vê apenas recursos
    # do seu próprio grupo, eliminando o ruído de ativo de outras categorias.
    grupos_com_itens: list = []
    itens_sem_grupo: list = []
    recursos_sem_grupo: list = []
    open_group_ids: set = set()

    if reserva.source_id:
        items_by_group_id: dict = {}
        for item in itens_disponiveis:
            items_by_group_id.setdefault(item.group_id, []).append(item)
        itens_sem_grupo = items_by_group_id.pop(None, [])

        recursos = db.session.query(ReservaResource).filter(
            ReservaResource.source_id == reserva.source_id,
            ReservaResource.deleted_at.is_(None),
        ).order_by(ReservaResource.name).all()

        recursos_by_group_id: dict = {}
        for r in recursos:
            if r.group_id:
                recursos_by_group_id.setdefault(r.group_id, []).append(r)
            else:
                recursos_sem_grupo.append(r)

        all_groups = db.session.query(ReservaGroup).filter(
            ReservaGroup.deleted_at.is_(None)
        ).order_by(ReservaGroup.sort_order, ReservaGroup.name).all()

        group_nodes = {
            g.id: {
                "group": g,
                "itens": items_by_group_id.get(g.id, []),
                "resources_list": recursos_by_group_id.get(g.id, []),
                "children": [],
            }
            for g in all_groups
        }
        root_nodes: list = []
        for g in all_groups:
            if g.parent_id and g.parent_id in group_nodes:
                group_nodes[g.parent_id]["children"].append(group_nodes[g.id])
            else:
                root_nodes.append(group_nodes[g.id])

        def _has_items(node: dict) -> bool:
            return bool(node["itens"]) or any(_has_items(c) for c in node["children"])

        grupos_com_itens = [n for n in root_nodes if _has_items(n)]

        # Grupos que devem estar expandidos: aqueles que contêm itens com qty > 0 (recursivo)
        def _collect_open_ids(node: dict) -> set:
            ids: set = set()
            if any(qtd_atual.get(item.id, 0) > 0 for item in node["itens"]):
                ids.add(node["group"].id)
            for child in node["children"]:
                child_ids = _collect_open_ids(child)
                if child_ids:
                    ids.add(node["group"].id)  # pai abre se filho tem itens ativos
                    ids.update(child_ids)
            return ids

        open_group_ids: set = set()
        for n in grupos_com_itens:
            open_group_ids.update(_collect_open_ids(n))

    # Telefone do metadata_info
    telefone_atual = ""
    if reserva.metadata_info:
        telefone_atual = reserva.metadata_info.get("telefone", "")

    status_configs = (
        db.session.query(ReservaStatusConfig)
        .filter(ReservaStatusConfig.internal_name != "ARCHIVED_VERSION")
        .order_by(ReservaStatusConfig.id)
        .all()
    )

    return render_template(
        "reservas/admin/reserva_edit.html",
        reserva=reserva,
        itens_disponiveis=itens_disponiveis,
        qtd_atual=qtd_atual,
        resource_atual=resource_atual,
        resource_atual_nome=resource_atual_nome,
        grupos_com_itens=grupos_com_itens,
        itens_sem_grupo=itens_sem_grupo,
        recursos_sem_grupo=recursos_sem_grupo,
        open_group_ids=open_group_ids,
        telefone_atual=telefone_atual,
        status_configs=status_configs,
        desconto_efetivo_pct=ReservaService.desconto_efetivo_pct(reserva),
    )


@bp.route("/admin/reservas/<int:reserva_id>/editar", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def admin_editar_reserva_submit(reserva_id: int):
    """Processa o formulário de edição de reserva e cria nova versão imutável.

    Fluxo:
    1. Valida dados do formulário (cliente, datas, itens)
    2. Resolve o desconto manual efetivo (submetido ou herdado via
       ReservaService.desconto_efetivo_pct — nunca deixado para o atualizar()
       herdar sozinho, ver comentário no bloco 3b)
    3. Calcula preços server-side via ReservaPricingService
    4. Passa preços calculados como price_override para ReservaService.atualizar()
       (garante que a validação anti-fraude passe com divergência zero)
    5. Redireciona para detalhe da nova versão em caso de sucesso

    Form fields:
        customer_name: str — nome do cliente
        customer_document: str — CPF/CNPJ (opcional)
        telefone: str — telefone do cliente (salvo em metadata_info)
        check_in_date: str — data ISO YYYY-MM-DD
        check_out_date: str — data ISO YYYY-MM-DD (opcional)
        items_<item_id>: int — quantidade para cada item (0 = remover)
        manual_discount_percent: str — desconto manual em % (opcional; vazio =
            mantém o desconto efetivo atual da reserva, ver desconto_efetivo_pct)

    Returns:
        Redirect para detalhe da reserva atualizada em caso de sucesso.
        Renderiza o formulário com erro (HTTP 400) em caso de falha.
    """


    reserva = db.session.get(Reserva, reserva_id)

    if not reserva or reserva.deleted_at is not None:
        abort(404)

    if reserva.status == ReservaStatus.ARCHIVED_VERSION:
        abort(404)

    # 1. Coletar dados do formulário
    customer_name = request.form.get("customer_name", "").strip()
    customer_document = request.form.get("customer_document", "").strip() or None
    telefone = request.form.get("telefone", "").strip()
    check_in_str = request.form.get("check_in_date", "").strip()
    check_out_str = request.form.get("check_out_date", "").strip()
    novo_status = request.form.get("status", "").strip() or None

    if not customer_name:
        return _render_edit_form_error(reserva, "Nome do cliente é obrigatório.")

    # 2. Parsear datas
    try:
        check_in = _parse_dt(check_in_str) if check_in_str else reserva.check_in_date
        check_out = _parse_dt(check_out_str) if check_out_str else reserva.check_out_date
    except ValueError:
        return _render_edit_form_error(reserva, "Formato de data inválido. Use YYYY-MM-DD ou YYYY-MM-DDTHH:MM.")

    if check_out and check_out < check_in:
        return _render_edit_form_error(reserva, "Data de check-out não pode ser anterior ao check-in.")

    # 3. Coletar itens com quantidade > 0 do formulário (campo: items_<item_id>)
    #    e recurso físico por item (campo: resource_<item_id>, opcional)
    itens_form: list[dict] = []
    for key, value in request.form.items():
        if key.startswith("items_"):
            try:
                item_id = int(key[len("items_"):])
                quantity = int(value)
            except (ValueError, TypeError):
                continue
            if quantity > 0:
                # Verificar se foi selecionado um recurso físico para este item
                resource_id_raw = request.form.get(f"resource_{item_id}", "").strip()
                resource_id = None
                if resource_id_raw:
                    try:
                        resource_id = int(resource_id_raw)
                    except (ValueError, TypeError):
                        resource_id = None
                itens_form.append({
                    "item_id": item_id,
                    "quantity": quantity,
                    "resource_id": resource_id,
                })

    if not itens_form:
        return _render_edit_form_error(reserva, "A reserva deve conter ao menos um item com quantidade > 0.")

    # 3b. Desconto manual — percentual informado pelo operador (ex: "10" = 10%).
    # Sempre resolvido explicitamente aqui (nunca deixado para o atualizar() herdar
    # sozinho): reservas cujo desconto original veio embutido no price_override da
    # criação (ex.: cupom do plugin WordPress) nunca tiveram desconto_percentual_manual
    # gravado, então a herança automática do service não tem o que herdar e o desconto
    # desaparecia silenciosamente a cada edição — mesmo uma que só trocasse o status.
    discount_raw = request.form.get("manual_discount_percent")
    if discount_raw is None or not discount_raw.strip():
        manual_discount_percent = ReservaService.desconto_efetivo_pct(reserva)
    else:
        try:
            manual_discount_percent = float(discount_raw.strip().replace(",", "."))
            if manual_discount_percent < 0:
                return _render_edit_form_error(reserva, "Desconto manual não pode ser negativo.")
            if manual_discount_percent > 100:
                return _render_edit_form_error(reserva, "Desconto manual não pode ser maior que 100%.")
        except (ValueError, TypeError):
            return _render_edit_form_error(reserva, "Desconto manual deve ser um número.")

    # 4. Calcular preços server-side (nunca confiar no cliente)
    source_id = reserva.source_id
    if not source_id:
        first_item = db.session.get(ReservaItem, itens_form[0]["item_id"])
        source_id = first_item.category.source_id if first_item else None

    if not source_id:
        return _render_edit_form_error(reserva, "Não foi possível determinar a fonte (venue) desta reserva.")

    try:
        pricing = ReservaPricingService.calculate(
            source_id=source_id,
            check_in_date=check_in,
            check_out_date=check_out or check_in,
            items=itens_form,
            category_id=reserva.category_id,
        )
    except ValueError as exc:
        return _render_edit_form_error(reserva, f"Erro no cálculo de preço: {exc}")

    # 5. Injetar price_override = item_total calculado (satisfaz validação anti-fraude)
    breakdown_map = {entry["item_id"]: entry["item_total"] for entry in pricing["items_breakdown"]}
    items_payload = [
        {
            "item_id": it["item_id"],
            "quantity": it["quantity"],
            "resource_id": it.get("resource_id"),
            "price_override": breakdown_map.get(it["item_id"], 0),
        }
        for it in itens_form
    ]

    # 6. Construir novo metadata_info preservando campos existentes
    meta_anterior = dict(reserva.metadata_info or {})
    if telefone:
        meta_anterior["telefone"] = telefone
    meta_anterior["editado_por_admin"] = True

    # 7. Delegar atualização ao service
    try:
        nova = ReservaService.atualizar(
            reserva_id,
            items_payload=items_payload,
            metadata_info=meta_anterior,
            customer_name=customer_name,
            customer_document=customer_document,
            check_in_date=check_in,
            check_out_date=check_out,
            status=novo_status,
            manual_discount_percent=manual_discount_percent,
            ator_id=_get_ator_id(),
        )
    except ValueError as exc:
        return _render_edit_form_error(reserva, str(exc))

    flash(f"Reserva #{reserva_id} atualizada. Nova versão: #{nova.id} (v{nova.version}).", "success")
    return redirect(url_for("reservas.detalhe", reserva_id=nova.id))


def _load_recursos_json() -> tuple[dict, list]:
    """Carrega todos os recursos físicos e grupos para embutir no formulário de nova reserva.

    Returns:
        Tupla (recursos_por_source, grupos_json) onde:
        - recursos_por_source: dict[str(source_id), list[dict]] com id, name, group_id
        - grupos_json: list[dict] com id, name, parent_id para todos os grupos ativos
    """
    all_recursos = db.session.query(ReservaResource).filter(
        ReservaResource.deleted_at.is_(None)
    ).order_by(ReservaResource.name).all()

    recursos_por_source: dict[str, list] = {}
    for r in all_recursos:
        sid = str(r.source_id)
        recursos_por_source.setdefault(sid, []).append({
            "id": r.id,
            "name": r.name,
            "group_id": r.group_id,
        })

    all_grupos = db.session.query(ReservaGroup).filter(
        ReservaGroup.deleted_at.is_(None)
    ).order_by(ReservaGroup.sort_order, ReservaGroup.name).all()

    grupos_json = [{"id": g.id, "name": g.name, "parent_id": g.parent_id} for g in all_grupos]

    return recursos_por_source, grupos_json


# ---------------------------------------------------------------------------
# Admin HTML Routes — PDV (Caixa)
# ---------------------------------------------------------------------------


@bp.route("/pdv", methods=["GET"])
@login_required
@require_permission("pdv", "operate")
def pdv_caixa():
    """Renderiza a SPA do PDV (caixa de balcão).

    Shell único sem recargas de página: turno, busca de itens, carrinho,
    pagamento com troco e comanda vivem em client-side state. Toda a API
    consumida está em /api/v1/pdv (ver docs/pdv/README.md).
    """
    return render_template("reservas/pdv.html")


@bp.route("/pdv/reconciliacao", methods=["GET"])
@login_required
@require_permission("pdv", "view_reports")
def pdv_reconciliacao():
    """Tela gerencial para revisar tentativas de sync offline do PDV."""
    outcome = request.args.get("outcome") or None
    try:
        logs = PdvService.listar_sync_logs(outcome=outcome, limit=100)
    except ValueError as exc:
        flash(str(exc), "error")
        outcome = None
        logs = PdvService.listar_sync_logs(limit=100)
    return render_template(
        "reservas/pdv_sync_logs.html",
        logs=logs,
        outcome=outcome,
        outcomes=[
            ("", "Todos"),
            ("ACCEPTED", "Aceitas"),
            ("REJECTED_STOCK", "Estoque"),
            ("REJECTED_DUPLICATE", "Duplicadas"),
            ("REJECTED_VALIDATION", "Validação"),
        ],
    )


@bp.route("/pdv-sw.js", methods=["GET"])
def pdv_service_worker():
    """Serve o service worker do PDV com escopo em /reservas/.

    O arquivo precisa sair de uma URL abaixo de /reservas/ para controlar a SPA
    em /reservas/pdv. A lógica de autenticação continua no backend: o service
    worker só reenvia chamadas para /api/v1/pdv com os cookies da sessão.
    """
    response = send_from_directory(current_app.static_folder, "pdv-sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Admin HTML Routes — Nova Reserva (Balcão)
# ---------------------------------------------------------------------------


@bp.route("/nova", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_nova_reserva_form():
    """Exibe o formulário de criação de nova reserva para atendimento no balcão.

    Carrega o catálogo completo (fontes → categorias → itens) e o embute como
    JSON no template para que o JavaScript preencha dinamicamente os selects
    de categoria e a lista de itens conforme a seleção do operador.

    Returns:
        Renderização de reservas/admin/reserva_nova.html.
    """
    catalogo = ReservaService.catalogo()
    recursos_por_source, grupos_json = _load_recursos_json()
    preferencia = db.session.query(ReservaPreferencia).filter_by(
        funcionario_id=_get_ator_id()
    ).first()
    status_configs = db.session.query(ReservaStatusConfig).order_by(ReservaStatusConfig.id).all()
    # Pré-preencher form_values com preferências do operador (somente em carga inicial)
    pref_values: dict = {}
    if preferencia:
        if preferencia.default_source_id:
            pref_values["source_id"] = str(preferencia.default_source_id)
        if preferencia.default_category_id:
            pref_values["category_id"] = str(preferencia.default_category_id)
        if preferencia.default_balcao_status:
            pref_values["balcao_status"] = preferencia.default_balcao_status
    return render_template(
        "reservas/admin/reserva_nova.html",
        catalogo=catalogo,
        catalogo_json=json.dumps(catalogo, ensure_ascii=False),
        recursos_json=json.dumps(recursos_por_source, ensure_ascii=False),
        grupos_json=json.dumps(grupos_json, ensure_ascii=False),
        status_configs=status_configs,
        error=None,
        form_values=pref_values,
    )


@bp.route("/nova", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_nova_reserva_submit():
    """Processa o formulário de criação de nova reserva no balcão.

    Fluxo:
    1. Valida campos obrigatórios (source, category, cliente, datas, itens)
    2. Calcula preços server-side via ReservaPricingService
    3. Injeta price_override calculado para satisfazer validação anti-fraude
    4. Chama ReservaService.criar() com origin=BALCAO
    5. Redireciona para detalhe da nova reserva em caso de sucesso

    Form fields:
        source_id: int — ID da ReservaSource selecionada
        category_id: int — ID da ReservaCategory selecionada
        customer_name: str — nome do cliente
        customer_document: str — CPF/CNPJ (opcional)
        telefone: str — telefone (salvo em metadata_info)
        check_in_date: str — data ISO YYYY-MM-DD
        check_out_date: str — data ISO YYYY-MM-DD (opcional)
        items_<item_id>: int — quantidade para cada item (0 = ignorar)
        force_overbooking: "1" quando o operador confirma criar mesmo com
            conflito de disponibilidade (campo oculto, setado via JS após a
            primeira tentativa retornar OverbookingError).
        force_notice_override: "1" quando o operador confirma criar mesmo com
            aviso de bloqueio ativo (campo oculto, setado via JS após a
            primeira tentativa retornar ResourceNoticeBlockedError).

    Returns:
        Redirect para detalhe da reserva criada em caso de sucesso.
        Renderiza o formulário com erro (HTTP 400) em caso de falha.
    """
    # Coletar campos básicos
    source_id_raw = request.form.get("source_id", "").strip()
    category_id_raw = request.form.get("category_id", "").strip()
    customer_name = request.form.get("customer_name", "").strip()
    customer_document = request.form.get("customer_document", "").strip() or None
    telefone = request.form.get("telefone", "").strip()
    email = request.form.get("email", "").strip()
    check_in_str = request.form.get("check_in_date", "").strip()
    check_out_str = request.form.get("check_out_date", "").strip()
    is_balcao_mode = request.form.get("is_balcao_mode", "0").strip() == "1"
    force_overbooking = request.form.get("force_overbooking", "0").strip() == "1"
    force_notice_override = request.form.get("force_notice_override", "0").strip() == "1"

    # Preservar valores do formulário para re-exibição em caso de erro
    form_values = {
        "source_id": source_id_raw,
        "category_id": category_id_raw,
        "customer_name": customer_name,
        "customer_document": customer_document or "",
        "telefone": telefone,
        "email": email,
        "check_in_date": check_in_str,
        "check_out_date": check_out_str,
        "manual_discount_percent": request.form.get("manual_discount_percent", "0"),
        "balcao_status": request.form.get("balcao_status", ""),
    }

    _status_configs_submit = db.session.query(ReservaStatusConfig).order_by(ReservaStatusConfig.id).all()

    def _erro(msg: str, conflito: dict | None = None, aviso_bloqueio: dict | None = None):
        cat = ReservaService.catalogo()
        recursos_por_source, grupos_json = _load_recursos_json()
        return render_template(
            "reservas/admin/reserva_nova.html",
            catalogo=cat,
            catalogo_json=json.dumps(cat, ensure_ascii=False),
            recursos_json=json.dumps(recursos_por_source, ensure_ascii=False),
            grupos_json=json.dumps(grupos_json, ensure_ascii=False),
            status_configs=_status_configs_submit,
            error=msg,
            form_values=form_values,
            conflito=conflito,
            aviso_bloqueio=aviso_bloqueio,
        ), 400

    # Validar source e category
    if not source_id_raw or not category_id_raw:
        return _erro("Selecione um local e uma categoria.")

    try:
        source_id = int(source_id_raw)
        category_id = int(category_id_raw)
    except (ValueError, TypeError):
        return _erro("Local ou categoria inválidos.")

    source = db.session.get(ReservaSource, source_id)
    category = db.session.get(ReservaCategory, category_id)

    if not source or source.deleted_at is not None:
        return _erro("Local não encontrado.")
    if not category or category.deleted_at is not None or category.source_id != source_id:
        return _erro("Categoria não encontrada.")

    # Validar cliente
    # Em modo balcão, nome/CPF/email/telefone são opcionais — gerado nome sequencial se ausente.
    if not customer_name:
        if is_balcao_mode:
            customer_name = ReservaService._proximo_nome_balcao()
        else:
            return _erro("Nome do cliente é obrigatório.")

    # Parsear datas
    try:
        check_in = _parse_dt(check_in_str) if check_in_str else None
    except ValueError:
        return _erro("Formato de data de check-in inválido. Use YYYY-MM-DDTHH:MM.")

    if not check_in:
        return _erro("Data de check-in é obrigatória.")

    try:
        check_out = _parse_dt(check_out_str) if check_out_str else None
    except ValueError:
        return _erro("Formato de data de check-out inválido. Use YYYY-MM-DDTHH:MM.")

    if check_out and check_out < check_in:
        return _erro("Data de check-out não pode ser anterior ao check-in.")

    # Coletar itens com quantidade > 0 (+ recurso físico opcional)
    itens_form: list[dict] = []
    for key, value in request.form.items():
        if key.startswith("items_"):
            try:
                item_id = int(key[len("items_"):])
                quantity = int(value)
            except (ValueError, TypeError):
                continue
            if quantity > 0:
                resource_id_raw = request.form.get(f"resource_{item_id}", "").strip()
                resource_id = None
                if resource_id_raw:
                    try:
                        resource_id = int(resource_id_raw)
                    except (ValueError, TypeError):
                        resource_id = None
                itens_form.append({"item_id": item_id, "quantity": quantity, "resource_id": resource_id})

    if not itens_form:
        return _erro("Selecione ao menos um item com quantidade maior que zero.")

    # Desconto manual — percentual informado pelo operador (ex: "10" = 10%)
    manual_discount_raw = request.form.get("manual_discount_percent", "0").strip().replace(",", ".")
    try:
        manual_discount_percent = float(manual_discount_raw) if manual_discount_raw else 0.0
        if manual_discount_percent < 0:
            return _erro("Desconto manual não pode ser negativo.")
        if manual_discount_percent > 100:
            return _erro("Desconto manual não pode ser maior que 100%.")
    except (ValueError, TypeError):
        manual_discount_percent = 0.0

    # Calcular preços server-side (com category_id e desconto manual em %)
    try:
        pricing = ReservaPricingService.calculate(
            source_id=source_id,
            check_in_date=check_in,
            check_out_date=check_out or check_in,
            items=itens_form,
            category_id=category_id,
            manual_discount_percent=manual_discount_percent,
        )
    except ValueError as exc:
        return _erro(f"Erro no cálculo de preço: {exc}")

    # Distribuir price_override proporcionalmente a partir do total final.
    # Garante sum(price_overrides) == server_total → validação anti-fraude passa com zero divergência.
    sim_subtotal = pricing["subtotal"]
    sim_total    = pricing["total"]
    breakdown_map = {entry["item_id"]: entry["item_total"] for entry in pricing["items_breakdown"]}

    items_payload = []
    for it in itens_form:
        item_total = breakdown_map.get(it["item_id"], 0)
        price_override = round(item_total / sim_subtotal * sim_total, 2) if sim_subtotal > 0 else 0
        items_payload.append({
            "item_id": it["item_id"],
            "quantity": it["quantity"],
            "resource_id": it.get("resource_id"),
            "price_override": price_override,
        })

    # Corrigir arredondamento: garantir que a soma seja exatamente sim_total
    sum_overrides = sum(it["price_override"] for it in items_payload)
    rounding_diff = round(sim_total - sum_overrides, 2)
    if abs(rounding_diff) >= 0.01 and items_payload:
        largest_idx = max(range(len(items_payload)), key=lambda i: items_payload[i]["price_override"])
        items_payload[largest_idx]["price_override"] = round(
            items_payload[largest_idx]["price_override"] + rounding_diff, 2
        )

    metadata: dict = {}
    if telefone:
        metadata["telefone"] = telefone
    if email:
        metadata["email"] = email
    metadata["origem_balcao"] = True
    # Armazena se foi uma reserva de balcão "walk-in" (toggle ativado).
    # Usado por analytics para excluir walk-ins do cálculo de antecedência média.
    metadata["is_balcao_mode"] = is_balcao_mode

    try:
        nova = ReservaService.criar(
            source_name=source.name,
            category_name=category.name,
            customer_name=customer_name,
            customer_document=customer_document,
            origin="BALCAO",
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=items_payload,
            metadata_info=metadata,
            manual_discount_percent=manual_discount_percent,
            force_overbooking=force_overbooking,
            force_notice_override=force_notice_override,
            ator_id=_get_ator_id(),
        )
    except ClientBlacklistedError:
        return _erro("Sem resposta do servidor. Tente novamente em instantes.")
    except OverbookingError as exc:
        return _erro(
            str(exc),
            conflito={
                "resource_name": exc.resource_name,
                "conflitos": exc.conflitos,
            },
        )
    except ResourceNoticeBlockedError as exc:
        return _erro(
            str(exc),
            aviso_bloqueio={"notice_text": exc.notice_text},
        )
    except ValueError as exc:
        return _erro(str(exc))

    # Reservas de balcão recebem status selecionado pelo operador (padrão: PAID)
    if is_balcao_mode:
        balcao_status_str = request.form.get("balcao_status", "").strip()
        # Validar se o status submetido é um ReservaStatusConfig ativo
        status_valido = None
        if balcao_status_str:
            status_valido = db.session.query(ReservaStatusConfig).filter_by(
                internal_name=balcao_status_str,
            ).first()
        nova.status = balcao_status_str if status_valido else ReservaStatus.PAID
        db.session.commit()

    flash(f"Reserva #{nova.id} criada com sucesso!", "sucesso")
    return redirect(url_for("reservas.detalhe", reserva_id=nova.id))


@bp.route("/predefinicoes", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "view")
def admin_predefinicoes():
    """Exibe e salva as predefinições do operador para o módulo de reservas.

    GET:  Carrega as preferências atuais do funcionário logado e renderiza o
          formulário de configuração.
    POST: Salva (upsert) as preferências — Local padrão, Categoria padrão e
          Status inicial para reservas de Balcão.

    Form fields:
        default_source_id (int, opcional): ID de ReservaSource preferido.
        default_category_id (int, opcional): ID de ReservaCategory preferido.
        default_balcao_status (str, opcional): internal_name de ReservaStatusConfig.

    Returns:
        GET: Renderização de reservas/admin/predefinicoes.html.
        POST sucesso: Redirect para a própria página com flash de confirmação.
        POST erro: Renderiza o formulário com mensagem de erro (HTTP 400).
    """
    from app.core.audit import AuditService

    ator_id = _get_ator_id()
    preferencia = db.session.query(ReservaPreferencia).filter_by(
        funcionario_id=ator_id
    ).first()

    catalogo = ReservaService.catalogo()
    status_configs = db.session.query(ReservaStatusConfig).order_by(ReservaStatusConfig.id).all()
    itens_pdv = (
        db.session.query(ReservaItem)
        .join(ReservaItem.category)
        .join(ReservaCategory.source)
        .filter(
            ReservaItem.deleted_at.is_(None),
            ReservaCategory.deleted_at.is_(None),
            ReservaSource.deleted_at.is_(None),
            ReservaSource.is_active.is_(True),
        )
        .order_by(ReservaSource.name, ReservaCategory.name, ReservaItem.name)
        .all()
    )

    def _favoritos_ordenados():
        """Resolve os IDs favoritos salvos em objetos ReservaItem, na ordem salva.

        A ordem de exibição no PDV é a própria ordem do array
        ``pdv_favorite_item_ids``; itens deletados/inativos são descartados.
        """
        itens_por_id = {item.id: item for item in itens_pdv}
        salvos = (preferencia.pdv_favorite_item_ids or []) if preferencia else []
        return [itens_por_id[i] for i in salvos if i in itens_por_id]

    if request.method == "POST":
        src_raw = request.form.get("default_source_id", "").strip()
        cat_raw = request.form.get("default_category_id", "").strip()
        status_raw = request.form.get("default_balcao_status", "").strip()
        pdv_status_raw = request.form.get("pdv_default_status", "").strip()
        favorite_raw = request.form.getlist("pdv_favorite_item_ids")

        new_source_id = int(src_raw) if src_raw.isdigit() else None
        new_category_id = int(cat_raw) if cat_raw.isdigit() else None
        new_status = status_raw or None
        new_pdv_status = pdv_status_raw or None

        # A ordem dos favoritos é a própria ordem de envio do formulário:
        # controles HTML são submetidos em ordem de árvore do DOM, e a tela
        # reordena as linhas (arrastar-e-soltar / setas) antes do submit.
        valid_item_ids = {item.id for item in itens_pdv}
        new_pdv_favorites: list[int] = []
        for item_raw in favorite_raw:
            if not str(item_raw).isdigit():
                continue
            item_id = int(item_raw)
            if item_id in valid_item_ids and item_id not in new_pdv_favorites:
                new_pdv_favorites.append(item_id)

        # Validar que category pertence ao source selecionado (se ambos fornecidos)
        if new_source_id and new_category_id:
            cat_obj = db.session.get(ReservaCategory, new_category_id)
            if not cat_obj or cat_obj.source_id != new_source_id or cat_obj.deleted_at is not None:
                flash("Categoria não pertence ao local selecionado.", "error")
                return render_template(
                    "reservas/admin/predefinicoes.html",
                    catalogo=catalogo,
                    itens_pdv=itens_pdv,
                    favoritos_ordenados=_favoritos_ordenados(),
                    status_configs=status_configs,
                    preferencia=preferencia,
                ), 400

        # Validar status
        if new_status:
            sc = db.session.query(ReservaStatusConfig).filter_by(
                internal_name=new_status,
            ).first()
            if not sc:
                new_status = None
        if new_pdv_status:
            sc = db.session.query(ReservaStatusConfig).filter_by(
                internal_name=new_pdv_status,
            ).first()
            if not sc or sc.internal_name == "ARCHIVED_VERSION":
                new_pdv_status = None

        snapshot_anterior = None
        if preferencia:
            snapshot_anterior = {
                "default_source_id": preferencia.default_source_id,
                "default_category_id": preferencia.default_category_id,
                "default_balcao_status": preferencia.default_balcao_status,
                "pdv_default_status": preferencia.pdv_default_status,
                "pdv_favorite_item_ids": preferencia.pdv_favorite_item_ids or [],
            }
            preferencia.default_source_id = new_source_id
            preferencia.default_category_id = new_category_id
            preferencia.default_balcao_status = new_status
            preferencia.pdv_default_status = new_pdv_status
            preferencia.pdv_favorite_item_ids = new_pdv_favorites
        else:
            preferencia = ReservaPreferencia(
                funcionario_id=ator_id,
                default_source_id=new_source_id,
                default_category_id=new_category_id,
                default_balcao_status=new_status,
                pdv_default_status=new_pdv_status,
                pdv_favorite_item_ids=new_pdv_favorites,
            )
            db.session.add(preferencia)

        db.session.flush()

        snapshot_novo = {
            "default_source_id": preferencia.default_source_id,
            "default_category_id": preferencia.default_category_id,
            "default_balcao_status": preferencia.default_balcao_status,
            "pdv_default_status": preferencia.pdv_default_status,
            "pdv_favorite_item_ids": preferencia.pdv_favorite_item_ids or [],
        }

        AuditService.log_update(
            "reservas",
            entity_id=preferencia.id,
            previous_state=snapshot_anterior or {},
            new_state=snapshot_novo,
            actor_id=ator_id,
        )

        db.session.commit()
        flash("Predefinições salvas com sucesso.", "success")
        return redirect(url_for("reservas.admin_predefinicoes"))

    return render_template(
        "reservas/admin/predefinicoes.html",
        catalogo=catalogo,
        itens_pdv=itens_pdv,
        favoritos_ordenados=_favoritos_ordenados(),
        status_configs=status_configs,
        preferencia=preferencia,
    )


@bp.route("/admin/reservas/<int:reserva_id>/historico", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def admin_historico_reserva(reserva_id: int):
    """Exibe o histórico completo de versões de uma reserva na interface admin.

    Recupera todas as versões (arquivadas, ativas, canceladas) que compartilham
    o mesmo group_id da reserva indicada. A visualização é somente-leitura —
    versões históricas não são editáveis.

    Args:
        reserva_id: ID de qualquer versão da reserva (ativa ou arquivada).

    Returns:
        Renderização de reservas/admin/reserva_historico.html ou abort 404.
    """
    try:
        versoes = ReservaService.historico_versoes(reserva_id)
    except ValueError:
        abort(404)

    # Versão de referência = a que foi solicitada na URL (qualquer versão)
    reserva_ref = db.session.get(Reserva, reserva_id)

    # Versão atual = a que não é ARCHIVED_VERSION e não está deletada
    versao_atual = next(
        (v for v in reversed(versoes) if v.status != ReservaStatus.ARCHIVED_VERSION and v.deleted_at is None),
        versoes[-1] if versoes else reserva_ref,
    )

    return render_template(
        "reservas/admin/reserva_historico.html",
        versoes=versoes,
        reserva_ref=reserva_ref,
        versao_atual=versao_atual,
    )


# ---------------------------------------------------------------------------
# Admin — Geração Manual de PIX
# ---------------------------------------------------------------------------


@bp.route("/admin/reservas/<int:reserva_id>/gerar-pix", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def admin_gerar_pix_manual(reserva_id: int):
    """Gera um PIX Mercado Pago manualmente para uma reserva via painel admin.

    Autenticado por sessão de operador. Chama MercadoPagoService diretamente,
    sem exigir o bearer token do WordPress. Retorna JSON para consumo via fetch().

    JSON body (opcional):
        payer_email (str): E-mail do pagador. Padrão: "<id>@sistur.local".

    Returns:
        200: { success, payment_id, qr_code, qr_code_base64, ticket_url }
        400: Reserva inválida ou sem total calculado.
        422: MP não retornou QR Code.
        500: Erro interno.
    """
    import logging as _logging
    from app.services.payment_service import MercadoPagoService

    _log = _logging.getLogger(__name__)

    reserva = db.session.get(Reserva, reserva_id)
    if not reserva or reserva.deleted_at is not None:
        return jsonify({"erro": f"Reserva {reserva_id} não encontrada."}), 404

    if not reserva.total_amount or float(reserva.total_amount) <= 0:
        return jsonify({"erro": "Reserva sem valor total calculado. Edite e salve antes de gerar PIX."}), 400

    body = request.get_json(silent=True) or {}
    payer_email = body.get("payer_email") or f"{reserva.id}@sistur.local"

    # Suporte a pagamento parcial: se partial_amount fornecido, usa esse valor.
    # Caso contrário, cobra somente o saldo devedor (total - já pago).
    raw_partial = body.get("partial_amount")
    if raw_partial is not None:
        try:
            transaction_amount = round(float(raw_partial), 2)
            if transaction_amount <= 0:
                return jsonify({"erro": "partial_amount deve ser maior que zero."}), 400
        except (TypeError, ValueError):
            return jsonify({"erro": "partial_amount inválido."}), 400
    else:
        # PIX Integral: cobra apenas o saldo devedor (evita cobrar o total quando já há parcial pago)
        _total_val = float(reserva.total_amount or 0)
        _payment_data_leg = reserva.payment_data or {}
        if reserva.status.value == "PAID" and _total_val > 0:
            _total_paid = _total_val
        else:
            _total_paid = float(_payment_data_leg.get("amount_paid", 0) or 0)
        transaction_amount = round(max(0.01, _total_val - _total_paid), 2)

    # validated_partial é sempre o valor efetivo a cobrar (passado explicitamente ao serviço)
    validated_partial = transaction_amount

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
        "transaction_amount": transaction_amount,
        "payer": payer,
    }

    try:
        mp_response = MercadoPagoService.processar_pagamento(
            reserva_id,
            payment_dados,
            partial_amount=validated_partial,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    except RuntimeError as exc:
        _log.error("Erro de configuração ao gerar PIX admin reserva=%s: %s", reserva_id, exc)
        return jsonify({"erro": str(exc)}), 500
    except Exception as exc:
        _log.exception("Erro inesperado ao gerar PIX admin reserva=%s", reserva_id)
        return jsonify({"erro": "Erro interno ao gerar PIX.", "detalhe": str(exc)}), 500

    point_of_interaction = mp_response.get("point_of_interaction", {})
    transaction_data = point_of_interaction.get("transaction_data", {})
    qr_code = transaction_data.get("qr_code", "")
    qr_code_base64 = transaction_data.get("qr_code_base64", "")

    if not qr_code:
        return jsonify({
            "erro": "Mercado Pago não retornou QR Code.",
            "mp_status": mp_response.get("status"),
            "mp_detail": mp_response.get("status_detail"),
        }), 422

    return jsonify({
        "success": True,
        "payment_id": str(mp_response.get("id", "")),
        "transaction_id": mp_response.get("_transaction_id", ""),
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
        "ticket_url": transaction_data.get("ticket_url", ""),
        "status": mp_response.get("status"),
    }), 200


# ---------------------------------------------------------------------------
# Admin — Geração de Link de Pagamento por Cartão
# ---------------------------------------------------------------------------


@bp.route("/admin/reservas/<int:reserva_id>/gerar-link-cartao", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def admin_gerar_link_cartao(reserva_id: int):
    """Gera um link de pagamento por cartão via Mercado Pago Preferences.

    Autenticado por sessão de operador. Cria uma Preference no MP com
    external_reference vinculado ao PaymentTransaction criado. Quando o cliente
    pagar, o webhook MP atualiza a reserva automaticamente (mesmo fluxo do PIX).

    JSON body (opcional):
        payer_email (str): E-mail do pagador para pré-preencher no checkout.

    Returns:
        200: { success, checkout_url, transaction_id, preference_id, expires_at, amount }
        400: Reserva inválida ou sem total calculado.
        500: Erro de configuração MP ou erro interno.
    """
    import logging as _logging

    from app.services.payment_service import MercadoPagoService

    _log = _logging.getLogger(__name__)

    reserva = db.session.get(Reserva, reserva_id)
    if not reserva or reserva.deleted_at is not None:
        return jsonify({"erro": f"Reserva {reserva_id} não encontrada."}), 404

    if not reserva.total_amount or float(reserva.total_amount) <= 0:
        return jsonify({
            "erro": "Reserva sem valor total calculado. Edite e salve antes de gerar o link."
        }), 400

    body = request.get_json(silent=True) or {}
    payer_email = body.get("payer_email") or None

    try:
        resultado = MercadoPagoService.gerar_link_pagamento_cartao(
            reserva_id,
            payer_email=payer_email,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    except RuntimeError as exc:
        _log.error("Erro de configuração ao gerar link cartão reserva=%s: %s", reserva_id, exc)
        return jsonify({"erro": str(exc)}), 500
    except Exception as exc:
        _log.exception("Erro inesperado ao gerar link cartão reserva=%s", reserva_id)
        return jsonify({"erro": "Erro interno ao gerar link de pagamento.", "detalhe": str(exc)}), 500

    return jsonify({
        "success": True,
        "checkout_url": resultado["checkout_url"],
        "transaction_id": resultado["transaction_id"],
        "preference_id": resultado["preference_id"],
        "expires_at": resultado["expires_at"],
        "amount": resultado["amount"],
    }), 200


# ---------------------------------------------------------------------------
# Admin — Verificação Manual de Pagamento
# ---------------------------------------------------------------------------


@bp.route("/admin/reservas/<int:reserva_id>/verificar-pagamento", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def admin_verificar_pagamento(reserva_id: int):
    """Sincroniza manualmente o status de pagamento de uma reserva com o Mercado Pago.

    Útil quando o webhook do MP não foi entregue (URL não configurada, timeout, etc.).
    Chama a API do MP com o payment_id da reserva e aplica a mesma lógica do webhook.

    Returns:
        200: { status, mp_status, msg } — status é 'approved', 'pending' ou 'rejected'
        400: Reserva sem payment_id (PIX não foi gerado).
        404: Reserva não encontrada.
        500: Erro ao chamar a API do MP.
    """
    import logging as _logging
    from app.services.payment_service import MercadoPagoService

    _log = _logging.getLogger(__name__)

    reserva = db.session.get(Reserva, reserva_id)
    if not reserva or reserva.deleted_at is not None:
        return jsonify({"erro": "Reserva não encontrada."}), 404

    if not reserva.payment_id:
        return jsonify({"erro": "Esta reserva não possui pagamento gerado."}), 400

    try:
        # Reutiliza a lógica do webhook passando o payment_id diretamente
        resultado = MercadoPagoService.processar_webhook({
            "type": "payment",
            "data": {"id": reserva.payment_id},
        })

        # Relê o status atualizado do banco
        db.session.refresh(reserva)
        from app.models.reservas import ReservaStatus
        mp_status_map = {
            ReservaStatus.PAID: "approved",
            ReservaStatus.PARTIALLY_PAID: "partially_paid",
            ReservaStatus.CANCELED: "rejected",
        }
        status = mp_status_map.get(reserva.status, "pending")

        return jsonify({
            "status": status,
            "mp_status": status,
            "msg": resultado.get("msg", ""),
        }), 200

    except RuntimeError as exc:
        _log.error("Erro de configuração ao verificar pagamento reserva=%s: %s", reserva_id, exc)
        return jsonify({"erro": str(exc)}), 500
    except Exception as exc:
        _log.exception("Erro inesperado ao verificar pagamento reserva=%s", reserva_id)
        return jsonify({"erro": "Erro interno.", "detalhe": str(exc)}), 500


# ---------------------------------------------------------------------------
# Admin — Verificação de Pagamento em Massa
# ---------------------------------------------------------------------------


@bp.route("/admin/verificar-pagamentos-em-massa", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def admin_verificar_pagamentos_em_massa():
    """Verifica o status de pagamento de todas as reservas com PIX pendente em um intervalo.

    Recebe um intervalo de datas via JSON e consulta o Mercado Pago para cada
    reserva que possua payment_id e status não finalizado (não PAID, não CANCELED).
    Limitado a no máximo 7 dias para evitar sobrecarga.

    Após a execução, a preferência de intervalo é resetada para 1 dia no frontend
    (comportamento imposto via campo reset_interval na resposta).

    JSON body:
        date_from (str): Data início no formato YYYY-MM-DD.
        date_to   (str): Data fim no formato YYYY-MM-DD (inclusive).

    Returns:
        200: {
            "verificadas": int,    — reservas consultadas no MP
            "atualizadas": int,    — reservas que mudaram de status
            "erros": int,          — reservas com falha na consulta
            "detalhes": list[dict] — [{reserva_id, status_anterior, status_novo, erro}]
            "reset_interval": 1    — sinaliza ao frontend para resetar o intervalo para 1 dia
        }
        400: Intervalo inválido ou maior que 7 dias.
        500: Erro interno.
    """
    import logging as _logging
    from app.services.payment_service import MercadoPagoService

    _log = _logging.getLogger(__name__)

    body = request.get_json(silent=True) or {}
    date_from_str = (body.get("date_from") or "").strip()
    date_to_str   = (body.get("date_to")   or "").strip()

    if not date_from_str or not date_to_str:
        return jsonify({"erro": "Parâmetros date_from e date_to são obrigatórios."}), 400

    try:
        date_from = datetime.strptime(date_from_str, "%Y-%m-%d").replace(hour=0,  minute=0,  second=0)
        date_to   = datetime.strptime(date_to_str,   "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    if date_to < date_from:
        return jsonify({"erro": "date_to não pode ser anterior a date_from."}), 400

    delta_days = (date_to.date() - date_from.date()).days + 1
    if delta_days > 7:
        return jsonify({"erro": f"Intervalo máximo é de 7 dias. Intervalo solicitado: {delta_days} dias."}), 400

    # Status que ainda podem ter pagamento pendente no MP
    _statuses_pendentes = [
        s.internal_name
        for s in db.session.query(ReservaStatusConfig).all()
        if s.internal_name not in ("PAID", "CANCELED", "ARCHIVED_VERSION", "COMPLETED")
    ] or ["PENDING", "PARTIALLY_PAID", "EM_USO", "DEPOSIT"]

    candidatas = db.session.query(Reserva).filter(
        Reserva.deleted_at.is_(None),
        Reserva.payment_id.isnot(None),
        Reserva.status.in_(_statuses_pendentes),
        Reserva.criado_em >= date_from,
        Reserva.criado_em <= date_to,
    ).order_by(Reserva.criado_em).all()

    verificadas = 0
    atualizadas = 0
    erros = 0
    detalhes: list[dict] = []

    for reserva in candidatas:
        status_anterior = str(reserva.status)
        try:
            MercadoPagoService.processar_webhook({
                "type": "payment",
                "data": {"id": reserva.payment_id},
            })
            db.session.refresh(reserva)
            status_novo = str(reserva.status)
            verificadas += 1
            mudou = status_novo != status_anterior
            if mudou:
                atualizadas += 1
            detalhes.append({
                "reserva_id": reserva.id,
                "customer_name": reserva.customer_name,
                "status_anterior": status_anterior,
                "status_novo": status_novo,
                "atualizada": mudou,
            })
        except Exception as exc:
            erros += 1
            _log.error("Erro ao verificar pagamento em massa reserva=%s: %s", reserva.id, exc)
            detalhes.append({
                "reserva_id": reserva.id,
                "customer_name": reserva.customer_name,
                "status_anterior": status_anterior,
                "status_novo": None,
                "erro": str(exc),
            })

    return jsonify({
        "verificadas": verificadas,
        "atualizadas": atualizadas,
        "erros": erros,
        "detalhes": detalhes,
        "reset_interval": 1,
    }), 200


# ---------------------------------------------------------------------------
# Admin — Status Config
# ---------------------------------------------------------------------------


@bp.route("/admin/config/status", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_status_config():
    """Exibe o painel de configuração dinâmica de status de reserva.

    Permite que operadores customizem rótulos, cores e regras de negócio
    (blocks_resource, show_on_dashboard) para cada status. Entradas marcadas
    como is_system_reserved não podem ser excluídas.

    Returns:
        Renderização de reservas/admin/status_config.html.
    """
    configs = (
        db.session.query(ReservaStatusConfig)
        .order_by(ReservaStatusConfig.id)
        .all()
    )
    return render_template("reservas/admin/status_config.html", configs=configs)


@bp.route("/admin/config/status/<int:config_id>/editar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_status_config_editar(config_id: int):
    """Salva alterações em uma entrada de configuração de status.

    Atualiza label, color_hex, blocks_resource, show_on_dashboard e auto_cancel_hours.
    O campo internal_name é protegido quando is_system_reserved=True.

    Args:
        config_id: ID da ReservaStatusConfig a atualizar.

    Returns:
        Redirect para admin_status_config com flash de sucesso ou erro.
    """


    cfg = db.session.get(ReservaStatusConfig, config_id)
    if cfg is None:
        abort(404)

    label = request.form.get("label", "").strip()
    color_hex = request.form.get("color_hex", "").strip()
    blocks_resource = request.form.get("blocks_resource") == "1"
    show_on_dashboard = request.form.get("show_on_dashboard") == "1"
    auto_cancel_hours_raw = request.form.get("auto_cancel_hours", "").strip()

    if not label:
        flash("O rótulo não pode estar vazio.", "erro")
        return redirect(url_for("reservas.admin_status_config"))

    if not color_hex or len(color_hex) != 7 or not color_hex.startswith("#"):
        flash("Cor inválida. Use o formato #RRGGBB.", "erro")
        return redirect(url_for("reservas.admin_status_config"))

    if auto_cancel_hours_raw:
        try:
            auto_cancel_hours = int(auto_cancel_hours_raw)
        except ValueError:
            flash("Prazo de cancelamento automático deve ser um número de horas.", "erro")
            return redirect(url_for("reservas.admin_status_config"))
        if auto_cancel_hours <= 0:
            flash("Prazo de cancelamento automático deve ser maior que zero.", "erro")
            return redirect(url_for("reservas.admin_status_config"))
    else:
        auto_cancel_hours = None

    cfg.label = label
    cfg.color_hex = color_hex
    cfg.blocks_resource = blocks_resource
    cfg.show_on_dashboard = show_on_dashboard
    cfg.auto_cancel_hours = auto_cancel_hours
    db.session.commit()

    flash(f"Configuração '{cfg.internal_name}' atualizada com sucesso.", "sucesso")
    return redirect(url_for("reservas.admin_status_config"))


@bp.route("/admin/config/status/<int:config_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_status_config_deletar(config_id: int):
    """Remove uma entrada de configuração de status não reservada pelo sistema.

    Entradas com is_system_reserved=True são protegidas e não podem ser removidas.

    Args:
        config_id: ID da ReservaStatusConfig a remover.

    Returns:
        Redirect para admin_status_config com flash de resultado.
    """


    cfg = db.session.get(ReservaStatusConfig, config_id)
    if cfg is None:
        abort(404)

    if cfg.is_system_reserved:
        flash(f"'{cfg.internal_name}' é reservado pelo sistema e não pode ser excluído.", "erro")
        return redirect(url_for("reservas.admin_status_config"))

    db.session.delete(cfg)
    db.session.commit()

    flash(f"Configuração '{cfg.internal_name}' removida.", "sucesso")
    return redirect(url_for("reservas.admin_status_config"))


@bp.route("/admin/config/status/novo", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_status_config_novo():
    """Cria uma nova entrada de configuração de status personalizada.

    Permite adicionar status visuais extras (ex: 'DEPOSIT') para uso em
    indicadores do Mapa de Ativos sem necessidade de alterar o código.

    Returns:
        Redirect para admin_status_config com flash de resultado.
    """


    internal_name = request.form.get("internal_name", "").strip().upper()
    label = request.form.get("label", "").strip()
    color_hex = request.form.get("color_hex", "#6B7280").strip()
    blocks_resource = request.form.get("blocks_resource") == "1"
    show_on_dashboard = request.form.get("show_on_dashboard") == "1"
    auto_cancel_hours_raw = request.form.get("auto_cancel_hours", "").strip()

    if not internal_name or not label:
        flash("Nome interno e rótulo são obrigatórios.", "erro")
        return redirect(url_for("reservas.admin_status_config"))

    if not color_hex or len(color_hex) != 7 or not color_hex.startswith("#"):
        flash("Cor inválida. Use o formato #RRGGBB.", "erro")
        return redirect(url_for("reservas.admin_status_config"))

    if auto_cancel_hours_raw:
        try:
            auto_cancel_hours = int(auto_cancel_hours_raw)
        except ValueError:
            flash("Prazo de cancelamento automático deve ser um número de horas.", "erro")
            return redirect(url_for("reservas.admin_status_config"))
        if auto_cancel_hours <= 0:
            flash("Prazo de cancelamento automático deve ser maior que zero.", "erro")
            return redirect(url_for("reservas.admin_status_config"))
    else:
        auto_cancel_hours = None

    existing = db.session.query(ReservaStatusConfig).filter_by(
        internal_name=internal_name
    ).first()
    if existing:
        flash(f"Já existe uma configuração com o nome '{internal_name}'.", "erro")
        return redirect(url_for("reservas.admin_status_config"))

    novo = ReservaStatusConfig(
        internal_name=internal_name,
        label=label,
        color_hex=color_hex,
        blocks_resource=blocks_resource,
        show_on_dashboard=show_on_dashboard,
        is_system_reserved=False,
        auto_cancel_hours=auto_cancel_hours,
    )
    db.session.add(novo)
    db.session.commit()

    flash(f"Configuração '{internal_name}' criada com sucesso.", "sucesso")
    return redirect(url_for("reservas.admin_status_config"))


# ---------------------------------------------------------------------------
# Admin — Banimentos
# ---------------------------------------------------------------------------


@bp.route("/admin/config/lista-negra", methods=["GET"], endpoint="admin_lista_negra_legacy")
@login_required
@require_permission("reservas", "configurar")
def admin_lista_negra_legacy():
    """Redireciona o endereço antigo de banimentos para a URL atual.

    Returns:
        Redirect permanente para o painel de banimentos.
    """
    return redirect(url_for("reservas.admin_lista_negra"), code=301)


@bp.route("/admin/config/banimentos", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_lista_negra():
    """Exibe o painel administrativo de banimentos.

    Returns:
        Renderização de reservas/admin/lista_negra.html com entradas e locais ativos.
    """
    entries = BlacklistService.listar()
    sources = BlacklistService.listar_sources_disponiveis()
    return render_template(
        "reservas/admin/lista_negra.html",
        entries=entries,
        sources=sources,
        editing=None,
    )


@bp.route("/admin/config/banimentos/<int:entry_id>/editar", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_lista_negra_editar(entry_id: int):
    """Exibe o formulário de edição de um banimento.

    Args:
        entry_id: ID da entrada a editar.

    Returns:
        Renderização de reservas/admin/lista_negra.html.
    """
    try:
        editing = BlacklistService.obter(entry_id)
    except ValueError:
        abort(404)
    entries = BlacklistService.listar()
    sources = BlacklistService.listar_sources_disponiveis()
    return render_template(
        "reservas/admin/lista_negra.html",
        entries=entries,
        sources=sources,
        editing=editing,
    )


@bp.route("/admin/config/banimentos/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_lista_negra_criar():
    """Cria um banimento a partir do formulário.

    Form fields:
        name: Nome da pessoa bloqueada.
        cpf: CPF da pessoa bloqueada.
        phone: Telefone opcional.
        email: E-mail opcional.
        block_by_*: Checkboxes dos critérios de bloqueio.
        source_ids: Lista de IDs de locais.

    Returns:
        Redirect para o painel de banimentos com flash de resultado.
    """
    try:
        BlacklistService.criar(
            name=request.form.get("name", ""),
            cpf=request.form.get("cpf", ""),
            phone=request.form.get("phone", ""),
            email=request.form.get("email", ""),
            block_by_name=request.form.get("block_by_name") == "1",
            block_by_cpf=request.form.get("block_by_cpf") == "1",
            block_by_phone=request.form.get("block_by_phone") == "1",
            block_by_email=request.form.get("block_by_email") == "1",
            source_ids=request.form.getlist("source_ids"),
            ator_id=_get_ator_id(),
        )
        flash("Banimento adicionado com sucesso.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.admin_lista_negra"))


@bp.route("/admin/config/banimentos/<int:entry_id>/editar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_lista_negra_atualizar(entry_id: int):
    """Atualiza um banimento a partir do formulário.

    Args:
        entry_id: ID da entrada a atualizar.

    Returns:
        Redirect para o painel de banimentos com flash de resultado.
    """
    try:
        BlacklistService.atualizar(
            entry_id,
            name=request.form.get("name", ""),
            cpf=request.form.get("cpf", ""),
            phone=request.form.get("phone", ""),
            email=request.form.get("email", ""),
            block_by_name=request.form.get("block_by_name") == "1",
            block_by_cpf=request.form.get("block_by_cpf") == "1",
            block_by_phone=request.form.get("block_by_phone") == "1",
            block_by_email=request.form.get("block_by_email") == "1",
            source_ids=request.form.getlist("source_ids"),
            ator_id=_get_ator_id(),
        )
        flash("Banimento atualizado com sucesso.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.admin_lista_negra"))


@bp.route("/admin/config/banimentos/<int:entry_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_lista_negra_deletar(entry_id: int):
    """Remove logicamente um banimento.

    Args:
        entry_id: ID da entrada a remover.

    Returns:
        Redirect para o painel de banimentos com flash de resultado.
    """
    try:
        BlacklistService.deletar(entry_id, ator_id=_get_ator_id())
        flash("Banimento removido com sucesso.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reservas.admin_lista_negra"))


# ---------------------------------------------------------------------------
# Outros ajustes de preços — CRUD de AdvanceDiscountRule (Early Bird)
# ---------------------------------------------------------------------------


@bp.route("/admin/config/", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_config_hub():
    """Índice das telas de configuração do módulo de Reservas.

    Reúne num só lugar o que antes eram cinco links soltos na sidebar. Os
    ícones são SVG inline (o projeto não carrega icon font) e o `| safe` do
    template é seguro porque a string vem daqui, não de dados do usuário.

    Returns:
        Renderização de reservas/admin/config_hub.html.
    """
    cards = [
        {
            "titulo": "Ajustes de Preços",
            "descricao": (
                "Taxa de serviço, Early Bird, descontos por data e o calendário "
                "de feriados e alta temporada."
            ),
            "url": url_for("reservas.admin_ajustes_preco"),
            "icone": (
                '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<line x1="12" y1="1" x2="12" y2="23"/>'
                '<path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>'
            ),
        },
        {
            "titulo": "Status de Reservas",
            "descricao": "Nomes, cores e comportamento de cada status do fluxo.",
            "url": url_for("reservas.admin_status_config"),
            "icone": (
                '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>'
            ),
        },
        {
            "titulo": "Banimentos",
            "descricao": "Clientes bloqueados de criar novas reservas.",
            "url": url_for("reservas.admin_lista_negra"),
            "icone": (
                '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<circle cx="12" cy="12" r="10"/>'
                '<line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>'
            ),
        },
        {
            "titulo": "Avisos de Recursos",
            "descricao": "Mensagens exibidas ao operador ao reservar um recurso.",
            "url": url_for("reservas.admin_avisos_lista"),
            "icone": (
                '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 '
                '1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>'
                '<line x1="12" y1="9" x2="12" y2="13"/>'
                '<line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
            ),
        },
    ]
    return render_template("reservas/admin/config_hub.html", cards=cards)


@bp.route("/admin/config/ajustes-preco", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco():
    """Exibe o painel de ajustes de preço (Early Bird e futuros tipos).

    Lista todas as AdvanceDiscountRule ordenadas por fonte, categoria e antecedência.
    Também carrega fontes e categorias ativas para o formulário de criação.

    Returns:
        Renderização de reservas/admin/ajustes_preco.html.
    """
    regras = (
        db.session.query(AdvanceDiscountRule)
        .order_by(
            AdvanceDiscountRule.source_id,
            # category_id NULL por último — portável (MySQL func.isnull não existe no Postgres)
            AdvanceDiscountRule.category_id.is_(None),
            AdvanceDiscountRule.category_id,
            AdvanceDiscountRule.min_days_in_advance,
        )
        .all()
    )
    sources = (
        db.session.query(ReservaSource)
        .filter(ReservaSource.deleted_at.is_(None), ReservaSource.is_active.is_(True))
        .order_by(ReservaSource.name)
        .all()
    )
    categories = (
        db.session.query(ReservaCategory)
        .filter(ReservaCategory.deleted_at.is_(None))
        .order_by(ReservaCategory.source_id, ReservaCategory.name)
        .all()
    )
    open_rule_id = request.args.get("open_rule_id", type=int)
    cal_source = request.args.get("cal_source", type=int)
    cal_year = request.args.get("cal_year", type=int)
    cal_month = request.args.get("cal_month", type=int)

    pricing_config = {
        s.id: ReservaConfigService.obter_pricing_config(s.id) for s in sources
    }

    return render_template(
        "reservas/admin/ajustes_preco.html",
        regras=regras,
        sources=sources,
        categories=categories,
        open_rule_id=open_rule_id,
        cal_source=cal_source,
        cal_year=cal_year,
        cal_month=cal_month,
        pricing_config=pricing_config,
    )


@bp.route("/admin/config/ajustes-preco/config/<int:source_id>", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco_config_salvar(source_id: int):
    """Salva taxa de serviço e desconto linear de uma fonte.

    Campo vazio remove o override da fonte, voltando a herdar o valor global.

    Args:
        source_id: PK da ReservaSource.

    Form fields:
        service_fee_percent (str): Percentual da taxa de serviço (0–100).
        flat_discount_percent (str): Percentual de desconto linear (0–100).
    """
    try:
        ReservaConfigService.salvar_pricing_config(
            source_id=source_id,
            service_fee_percent=request.form.get("service_fee_percent"),
            flat_discount_percent=request.form.get("flat_discount_percent"),
            ator_id=session.get("funcionario_id"),
        )
        flash("Configurações de preço da fonte atualizadas.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("reservas.admin_ajustes_preco"))


@bp.route("/admin/config/ajustes-preco/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco_criar():
    """Cria uma nova regra de desconto por antecedência (Early Bird).

    Form fields:
        source_id (int): ID da ReservaSource. Obrigatório.
        category_id (int|''): ID da ReservaCategory. Vazio = aplica a toda a fonte.
        min_days_in_advance (int): Mínimo de dias de antecedência. Deve ser > 0.
        discount_percentage (float): Percentual de desconto (0 < valor <= 100).
        is_active (str): '1' se ativa. Ausente = inativa.

    Returns:
        Redirect para admin_ajustes_preco.
    """
    from datetime import datetime, timezone

    from app.core.audit import AuditService
    from app.services.base import BaseService

    try:
        source_id = int(request.form["source_id"])
        category_id_raw = request.form.get("category_id", "").strip()
        category_id = int(category_id_raw) if category_id_raw else None
        min_days = int(request.form["min_days_in_advance"])
        pct = float(request.form["discount_percentage"])
        is_active = request.form.get("is_active") == "1"
    except (KeyError, ValueError):
        flash("Dados inválidos. Verifique todos os campos.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    if min_days <= 0:
        flash("Mínimo de dias deve ser maior que zero.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    if not (0 < pct <= 100):
        flash("Percentual de desconto deve estar entre 0 e 100.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    source = db.session.get(ReservaSource, source_id)
    if not source:
        flash("Fonte não encontrada.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    if category_id:
        cat = db.session.get(ReservaCategory, category_id)
        if not cat or cat.source_id != source_id:
            flash("Categoria inválida para a fonte selecionada.", "erro")
            return redirect(url_for("reservas.admin_ajustes_preco"))

    now = datetime.now(timezone.utc)
    regra = AdvanceDiscountRule(
        source_id=source_id,
        category_id=category_id,
        min_days_in_advance=min_days,
        discount_percentage=pct,
        is_active=is_active,
        criado_em=now,
        atualizado_em=now,
    )
    db.session.add(regra)
    db.session.flush()

    snapshot = BaseService._snapshot(
        regra,
        ["id", "source_id", "category_id", "min_days_in_advance", "discount_percentage", "is_active"],
    )
    db.session.commit()

    AuditService.log_create(
        module="desconto_antecipado",
        entity_id=regra.id,
        new_state=snapshot,
        actor_id=_get_ator_id(),
    )

    cat_label = f" (categoria {category_id})" if category_id else " (todas categorias)"
    flash(f"Regra criada: {min_days} dias → {pct}% em {source.name}{cat_label}.", "sucesso")
    return redirect(url_for("reservas.admin_ajustes_preco"))


@bp.route("/admin/config/ajustes-preco/<int:rule_id>/editar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco_editar(rule_id: int):
    """Atualiza uma regra de desconto por antecedência existente.

    Args:
        rule_id: PK da AdvanceDiscountRule a editar.

    Form fields:
        min_days_in_advance (int): Novo mínimo de dias. Deve ser > 0.
        discount_percentage (float): Novo percentual (0 < valor <= 100).
        is_active (str): '1' se ativa. Ausente = inativa.

    Returns:
        Redirect para admin_ajustes_preco.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    regra = db.session.get(AdvanceDiscountRule, rule_id)
    if not regra:
        flash("Regra não encontrada.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    try:
        min_days = int(request.form["min_days_in_advance"])
        pct = float(request.form["discount_percentage"])
        is_active = request.form.get("is_active") == "1"
    except (KeyError, ValueError):
        flash("Dados inválidos. Verifique todos os campos.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    if min_days <= 0:
        flash("Mínimo de dias deve ser maior que zero.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    if not (0 < pct <= 100):
        flash("Percentual de desconto deve estar entre 0 e 100.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    _FIELDS = ["id", "source_id", "category_id", "min_days_in_advance", "discount_percentage", "is_active"]
    antes = BaseService._snapshot(regra, _FIELDS)

    regra.min_days_in_advance = min_days
    regra.discount_percentage = pct
    regra.is_active = is_active
    db.session.commit()

    depois = BaseService._snapshot(regra, _FIELDS)
    AuditService.log_update(
        module="desconto_antecipado",
        entity_id=regra.id,
        previous_state=antes,
        new_state=depois,
        actor_id=_get_ator_id(),
    )

    flash(f"Regra #{rule_id} atualizada.", "sucesso")
    return redirect(url_for("reservas.admin_ajustes_preco"))


@bp.route("/admin/config/ajustes-preco/<int:rule_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco_deletar(rule_id: int):
    """Remove permanentemente uma regra de desconto por antecedência.

    Args:
        rule_id: PK da AdvanceDiscountRule a remover.

    Returns:
        Redirect para admin_ajustes_preco.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    regra = db.session.get(AdvanceDiscountRule, rule_id)
    if not regra:
        flash("Regra não encontrada.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    snapshot = BaseService._snapshot(
        regra,
        ["id", "source_id", "category_id", "min_days_in_advance", "discount_percentage", "is_active"],
    )
    db.session.delete(regra)
    db.session.commit()

    AuditService.log_delete(
        module="desconto_antecipado",
        entity_id=rule_id,
        previous_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Regra #{rule_id} removida.", "aviso")
    return redirect(url_for("reservas.admin_ajustes_preco"))


@bp.route("/admin/config/ajustes-preco/<int:rule_id>/tiers/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco_tier_criar(rule_id: int):
    """Cria um novo tier de desconto por dias de estadia para uma AdvanceDiscountRule.

    Args:
        rule_id: PK da AdvanceDiscountRule pai.

    Form fields:
        min_stay_days (int): Mínimo de dias de estadia para este tier. Deve ser >= 1.
        discount_percentage (float): Percentual de desconto (0 < valor <= 100).

    Returns:
        Redirect para admin_ajustes_preco com ?open_rule_id=<rule_id> para reabrir modal.
    """
    from datetime import datetime, timezone

    from app.core.audit import AuditService
    from app.services.base import BaseService

    regra = db.session.get(AdvanceDiscountRule, rule_id)
    if not regra:
        flash("Regra não encontrada.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    try:
        min_stay = int(request.form["min_stay_days"])
        pct = float(request.form["discount_percentage"])
    except (KeyError, ValueError):
        flash("Dados inválidos. Verifique os campos do tier.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))

    if min_stay < 1:
        flash("Mínimo de dias de estadia deve ser pelo menos 1.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))

    if not (0 < pct <= 100):
        flash("Percentual de desconto deve estar entre 0 e 100.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))

    existing = db.session.query(AdvanceDiscountStayTier).filter_by(
        rule_id=rule_id, min_stay_days=min_stay
    ).first()
    if existing:
        flash(f"Já existe um tier para {min_stay} dia(s) nesta regra.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))

    tier = AdvanceDiscountStayTier(
        rule_id=rule_id,
        min_stay_days=min_stay,
        discount_percentage=pct,
    )
    db.session.add(tier)
    db.session.flush()

    snapshot = BaseService._snapshot(tier, ["id", "rule_id", "min_stay_days", "discount_percentage"])
    db.session.commit()

    AuditService.log_create(
        module="desconto_antecipado_stay_tier",
        entity_id=tier.id,
        new_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Tier criado: {min_stay} dia(s) de estadia → {pct}%.", "sucesso")
    return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))


@bp.route(
    "/admin/config/ajustes-preco/<int:rule_id>/tiers/<int:tier_id>/deletar",
    methods=["POST"],
)
@login_required
@require_permission("reservas", "configurar")
def admin_ajustes_preco_tier_deletar(rule_id: int, tier_id: int):
    """Remove permanentemente um tier de desconto por estadia.

    Args:
        rule_id:  PK da AdvanceDiscountRule pai (usado para validação e redirect).
        tier_id:  PK do AdvanceDiscountStayTier a remover.

    Returns:
        Redirect para admin_ajustes_preco com ?open_rule_id=<rule_id> para reabrir modal.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    tier = db.session.get(AdvanceDiscountStayTier, tier_id)
    if not tier or tier.rule_id != rule_id:
        flash("Tier não encontrado.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))

    snapshot = BaseService._snapshot(tier, ["id", "rule_id", "min_stay_days", "discount_percentage"])
    db.session.delete(tier)
    db.session.commit()

    AuditService.log_delete(
        module="desconto_antecipado_stay_tier",
        entity_id=tier_id,
        previous_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Tier #{tier_id} removido.", "aviso")
    return redirect(url_for("reservas.admin_ajustes_preco", open_rule_id=rule_id))


# ---------------------------------------------------------------------------
# CalendarDiscount — descontos por data específica de check-in
# ---------------------------------------------------------------------------


@bp.route("/admin/config/ajustes-preco/calendar/api", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_api():
    """Retorna JSON com descontos de calendário de um source para um mês específico.

    Query params:
        source_id (int): ID da ReservaSource. Obrigatório.
        year (int): Ano a consultar. Padrão: ano corrente.
        month (int): Mês a consultar (1–12). Padrão: mês corrente.

    Returns:
        JSON: envelope com duas camadas do calendário —
              `discounts` (CalendarDiscount da fonte, com tiers) e
              `eventos` (datas especiais do Calendário Global que tocam o mês).
              Cada evento traz `editavel`, calculado pela política do
              CalendarService: o módulo só mantém o que ele mesmo criou e que
              ainda não virou evento de folha.
    """
    from datetime import date as date_cls
    import calendar as cal_mod

    source_id = request.args.get("source_id", type=int)
    if not source_id:
        return jsonify({"erro": "source_id obrigatório"}), 400

    today = date_cls.today()
    year = request.args.get("year", default=today.year, type=int)
    month = request.args.get("month", default=today.month, type=int)

    if not (1 <= month <= 12):
        return jsonify({"erro": "month deve estar entre 1 e 12"}), 400

    _, last_day = cal_mod.monthrange(year, month)
    start = date_cls(year, month, 1)
    end = date_cls(year, month, last_day)

    discounts = (
        db.session.query(CalendarDiscount)
        .filter(
            CalendarDiscount.source_id == source_id,
            CalendarDiscount.discount_date >= start,
            CalendarDiscount.discount_date <= end,
        )
        .order_by(CalendarDiscount.discount_date, CalendarDiscount.category_id)
        .all()
    )

    result = []
    for d in discounts:
        result.append({
            "id": d.id,
            "date": d.discount_date.isoformat(),
            "label": d.label,
            "is_active": d.is_active,
            "category_id": d.category_id,
            "category_name": d.category.name if d.category else None,
            "tiers": [
                {
                    "id": t.id,
                    "min_days_in_advance": t.min_days_in_advance,
                    "min_stay_days": t.min_stay_days,
                    "pct": float(t.discount_percentage),
                }
                for t in d.advance_tiers
            ],
        })

    eventos = [
        {
            "id": ev.id,
            "titulo": ev.titulo,
            "data_inicio": ev.data_evento.isoformat(),
            "data_fim": (ev.data_fim or ev.data_evento).isoformat(),
            "tipo": ev.tipo.value,
            "origem": ev.origem,
            "afeta_precificacao": ev.afeta_precificacao,
            "afeta_folha": ev.afeta_folha,
            "recorrente_anual": ev.recorrente_anual,
            "editavel": CalendarService.editavel_por_reservas(ev),
        }
        for ev in CalendarService.obter_eventos_mes(year, month)
    ]

    return jsonify({"discounts": result, "eventos": eventos})


@bp.route("/admin/config/ajustes-preco/calendar/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_criar():
    """Cria um novo CalendarDiscount para uma data específica de check-in.

    Form fields:
        source_id (int): ID da ReservaSource. Obrigatório.
        category_id (int|''): ID da ReservaCategory. Vazio = aplica a toda a fonte.
        discount_date (str): Data no formato YYYY-MM-DD. Obrigatório.
        label (str): Rótulo opcional (ex: "Black Friday").
        is_active (str): '1' se ativo. Ausente = inativo.
        open_year (int): Ano do calendário a reabrir no redirect.
        open_month (int): Mês do calendário a reabrir no redirect.

    Returns:
        Redirect para admin_ajustes_preco com parâmetros de reabertura de calendário.
    """
    from datetime import datetime, timezone, date as date_cls

    from app.core.audit import AuditService
    from app.services.base import BaseService

    try:
        source_id = int(request.form["source_id"])
        category_id_raw = request.form.get("category_id", "").strip()
        category_id = int(category_id_raw) if category_id_raw else None
        discount_date = date_cls.fromisoformat(request.form["discount_date"])
        label = request.form.get("label", "").strip() or None
        is_active = request.form.get("is_active") == "1"
        open_year = request.form.get("open_year", type=int, default=discount_date.year)
        open_month = request.form.get("open_month", type=int, default=discount_date.month)
    except (KeyError, ValueError):
        flash("Dados inválidos. Verifique todos os campos.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    source = db.session.get(ReservaSource, source_id)
    if not source:
        flash("Fonte não encontrada.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    if category_id:
        cat = db.session.get(ReservaCategory, category_id)
        if not cat or cat.source_id != source_id:
            flash("Categoria inválida para a fonte selecionada.", "erro")
            return redirect(url_for(
                "reservas.admin_ajustes_preco", cal_source=source_id,
                cal_year=open_year, cal_month=open_month,
            ))

    existing = db.session.query(CalendarDiscount).filter_by(
        source_id=source_id,
        category_id=category_id,
        discount_date=discount_date,
    ).first()
    if existing:
        flash(
            f"Já existe um desconto para {discount_date.strftime('%d/%m/%Y')} "
            f"nesta fonte/categoria. Edite o existente.",
            "erro",
        )
        return redirect(url_for(
            "reservas.admin_ajustes_preco", cal_source=source_id,
            cal_year=open_year, cal_month=open_month,
        ))

    now = datetime.now(timezone.utc)
    cd = CalendarDiscount(
        source_id=source_id,
        category_id=category_id,
        discount_date=discount_date,
        label=label,
        is_active=is_active,
        criado_em=now,
        atualizado_em=now,
    )
    db.session.add(cd)
    db.session.flush()

    snapshot = BaseService._snapshot(
        cd, ["id", "source_id", "category_id", "discount_date", "label", "is_active"]
    )
    db.session.commit()

    AuditService.log_create(
        module="calendar_discount",
        entity_id=cd.id,
        new_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Data especial criada: {discount_date.strftime('%d/%m/%Y')}. Adicione os tiers de antecedência.", "sucesso")
    return redirect(url_for(
        "reservas.admin_ajustes_preco", cal_source=source_id,
        cal_year=open_year, cal_month=open_month,
    ))


@bp.route("/admin/config/ajustes-preco/calendar/<int:cd_id>/editar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_editar(cd_id: int):
    """Atualiza label e status de um CalendarDiscount existente.

    Args:
        cd_id: PK do CalendarDiscount a editar.

    Form fields:
        label (str): Novo rótulo (pode ser vazio).
        is_active (str): '1' se ativo.
        open_year (int), open_month (int): Mês do calendário a reabrir no redirect.
        cal_source (int): source_id para redirect.

    Returns:
        Redirect para admin_ajustes_preco com parâmetros de reabertura.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    cd = db.session.get(CalendarDiscount, cd_id)
    if not cd:
        flash("Desconto de calendário não encontrado.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    try:
        label = request.form.get("label", "").strip() or None
        is_active = request.form.get("is_active") == "1"
        open_year = request.form.get("open_year", type=int, default=cd.discount_date.year)
        open_month = request.form.get("open_month", type=int, default=cd.discount_date.month)
    except (KeyError, ValueError):
        flash("Dados inválidos.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    _FIELDS = ["id", "source_id", "category_id", "discount_date", "label", "is_active"]
    antes = BaseService._snapshot(cd, _FIELDS)

    cd.label = label
    cd.is_active = is_active
    db.session.commit()

    depois = BaseService._snapshot(cd, _FIELDS)
    AuditService.log_update(
        module="calendar_discount",
        entity_id=cd.id,
        previous_state=antes,
        new_state=depois,
        actor_id=_get_ator_id(),
    )

    flash(f"Data especial #{cd_id} atualizada.", "sucesso")
    return redirect(url_for(
        "reservas.admin_ajustes_preco", cal_source=cd.source_id,
        cal_year=open_year, cal_month=open_month,
    ))


@bp.route("/admin/config/ajustes-preco/calendar/<int:cd_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_deletar(cd_id: int):
    """Remove permanentemente um CalendarDiscount e seus stay tiers (CASCADE).

    Args:
        cd_id: PK do CalendarDiscount a remover.

    Returns:
        Redirect para admin_ajustes_preco com parâmetros de reabertura.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    cd = db.session.get(CalendarDiscount, cd_id)
    if not cd:
        flash("Desconto de calendário não encontrado.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    open_year = cd.discount_date.year
    open_month = cd.discount_date.month
    source_id = cd.source_id

    snapshot = BaseService._snapshot(
        cd, ["id", "source_id", "category_id", "discount_date", "label", "is_active"]
    )
    db.session.delete(cd)
    db.session.commit()

    AuditService.log_delete(
        module="calendar_discount",
        entity_id=cd_id,
        previous_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Desconto #{cd_id} removido.", "aviso")
    return redirect(url_for(
        "reservas.admin_ajustes_preco", cal_source=source_id,
        cal_year=open_year, cal_month=open_month,
    ))


@bp.route("/admin/config/ajustes-preco/calendar/<int:cd_id>/tiers/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_tier_criar(cd_id: int):
    """Cria um tier 2D (antecedência + estadia opcional) para um CalendarDiscount.

    Args:
        cd_id: PK do CalendarDiscount pai.

    Form fields:
        min_days_in_advance (int):   Antecedência mínima em dias. >= 1.
        min_stay_days       (int):   Estadia mínima em noites (opcional). >= 1 se fornecido.
        discount_percentage (float): Percentual (0 < valor <= 100).

    Returns:
        Redirect para admin_ajustes_preco com parâmetros de reabertura.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def _err(msg, code=400):
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), code
        flash(msg, "erro")
        cd_ = db.session.get(CalendarDiscount, cd_id)
        if cd_:
            return redirect(url_for(
                "reservas.admin_ajustes_preco",
                cal_source=cd_.source_id,
                cal_year=cd_.discount_date.year,
                cal_month=cd_.discount_date.month,
            ))
        return redirect(url_for("reservas.admin_ajustes_preco"))

    cd = db.session.get(CalendarDiscount, cd_id)
    if not cd:
        return _err("Desconto de calendário não encontrado.", 404)

    try:
        min_adv = int(request.form["min_days_in_advance"])
        pct = float(request.form["discount_percentage"])
    except (KeyError, ValueError):
        return _err("Dados inválidos.")

    # Estadia mínima é opcional; campo vazio ou ausente = None
    min_stay_raw = request.form.get("min_stay_days", "").strip()
    min_stay: int | None = None
    if min_stay_raw:
        try:
            min_stay = int(min_stay_raw)
        except ValueError:
            return _err("Estadia mínima inválida.")
        if min_stay < 1:
            return _err("Estadia mínima deve ser pelo menos 1 noite.")

    if min_adv < 1:
        return _err("Antecedência mínima deve ser pelo menos 1 dia.")

    if not (0 < pct <= 100):
        return _err("Percentual de desconto deve estar entre 0 e 100.")

    # Unicidade: (calendar_discount_id, min_days_in_advance, min_stay_days).
    # MySQL trata NULLs como distintos em unique indexes, então NULL é verificado
    # explicitamente aqui para impedir duplicatas com min_stay_days=None.
    dup_q = db.session.query(CalendarDiscountAdvanceTier).filter_by(
        calendar_discount_id=cd_id, min_days_in_advance=min_adv
    )
    if min_stay is None:
        dup_q = dup_q.filter(CalendarDiscountAdvanceTier.min_stay_days.is_(None))
    else:
        dup_q = dup_q.filter(CalendarDiscountAdvanceTier.min_stay_days == min_stay)
    if dup_q.first():
        stay_label = f" + {min_stay} noite(s)" if min_stay else ""
        return _err(f"Já existe um tier para {min_adv} dia(s) de antecedência{stay_label} neste desconto.")

    tier = CalendarDiscountAdvanceTier(
        calendar_discount_id=cd_id,
        min_days_in_advance=min_adv,
        min_stay_days=min_stay,
        discount_percentage=pct,
    )
    db.session.add(tier)
    db.session.flush()

    snapshot = BaseService._snapshot(
        tier, ["id", "calendar_discount_id", "min_days_in_advance", "min_stay_days", "discount_percentage"]
    )
    db.session.commit()

    AuditService.log_create(
        module="calendar_discount_advance_tier",
        entity_id=tier.id,
        new_state=snapshot,
        actor_id=_get_ator_id(),
    )

    if is_ajax:
        return jsonify({
            "ok": True,
            "tier": {
                "id": tier.id,
                "min_days_in_advance": tier.min_days_in_advance,
                "min_stay_days": tier.min_stay_days,
                "pct": float(tier.discount_percentage),
            },
        }), 201

    stay_label = f" + {min_stay} noite(s)" if min_stay else ""
    flash(f"Tier criado: {min_adv} dia(s) de antecedência{stay_label} → {pct}%.", "sucesso")
    return redirect(url_for(
        "reservas.admin_ajustes_preco", cal_source=cd.source_id,
        cal_year=cd.discount_date.year, cal_month=cd.discount_date.month,
    ))


@bp.route(
    "/admin/config/ajustes-preco/calendar/<int:cd_id>/tiers/<int:tier_id>/deletar",
    methods=["POST"],
)
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_tier_deletar(cd_id: int, tier_id: int):
    """Remove um tier de antecedência de um CalendarDiscount.

    Args:
        cd_id:    PK do CalendarDiscount pai.
        tier_id:  PK do CalendarDiscountAdvanceTier a remover.

    Returns:
        Redirect para admin_ajustes_preco com parâmetros de reabertura.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    tier = db.session.get(CalendarDiscountAdvanceTier, tier_id)
    if not tier or tier.calendar_discount_id != cd_id:
        if is_ajax:
            return jsonify({"ok": False, "error": "Tier não encontrado."}), 404
        flash("Tier não encontrado.", "erro")
        return redirect(url_for("reservas.admin_ajustes_preco"))

    cd = db.session.get(CalendarDiscount, cd_id)
    open_year = cd.discount_date.year if cd else date.today().year
    open_month = cd.discount_date.month if cd else date.today().month
    source_id = cd.source_id if cd else None

    snapshot = BaseService._snapshot(
        tier, ["id", "calendar_discount_id", "min_days_in_advance", "min_stay_days", "discount_percentage"]
    )
    db.session.delete(tier)
    db.session.commit()

    AuditService.log_delete(
        module="calendar_discount_advance_tier",
        entity_id=tier_id,
        previous_state=snapshot,
        actor_id=_get_ator_id(),
    )

    if is_ajax:
        return jsonify({"ok": True}), 200

    flash(f"Tier #{tier_id} removido.", "aviso")
    return redirect(url_for(
        "reservas.admin_ajustes_preco", cal_source=source_id,
        cal_year=open_year, cal_month=open_month,
    ))


@bp.route(
    "/admin/config/ajustes-preco/calendar/<int:cd_id>/replicar",
    methods=["POST"],
)
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_discount_replicar(cd_id: int):
    """Replica os tiers de um CalendarDiscount para uma ou mais datas destino.

    Etapa 1 (sem on_conflict): detecta conflitos e retorna 409 com a lista
    de datas que já possuem um CalendarDiscount equivalente, para o frontend
    perguntar ao operador como proceder.

    Etapa 2 (com on_conflict): aplica a replicação com a estratégia escolhida.

    Request JSON:
        target_dates (list[str]):  Datas destino no formato YYYY-MM-DD.
        on_conflict  (str|null):   "overwrite" | "skip" | null (etapa 1).

    Returns:
        200 JSON: { "ok": true, "criados": N, "substituidos": N, "pulados": N }
        409 JSON: { "ok": false, "conflitos": [...], "sem_conflito": [...] }
        400/404 JSON: { "ok": false, "error": "..." }
    """
    from app.core.audit import AuditService

    source_cd = db.session.get(CalendarDiscount, cd_id)
    if not source_cd:
        return jsonify({"ok": False, "error": "Desconto de calendário não encontrado."}), 404

    payload = request.get_json(force=True, silent=True) or {}
    target_dates_raw: list = payload.get("target_dates", [])
    on_conflict: str | None = payload.get("on_conflict")

    if not target_dates_raw:
        return jsonify({"ok": False, "error": "Nenhuma data destino informada."}), 400

    target_dates: list[date] = []
    for d_str in target_dates_raw:
        try:
            target_dates.append(date.fromisoformat(d_str))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": f"Data inválida: {d_str}"}), 400

    # Detecta conflitos: CalendarDiscount existente com mesmo source/category na data destino.
    def _find_existing(d: date) -> CalendarDiscount | None:
        q = db.session.query(CalendarDiscount).filter(
            CalendarDiscount.source_id == source_cd.source_id,
            CalendarDiscount.discount_date == d,
        )
        if source_cd.category_id is None:
            q = q.filter(CalendarDiscount.category_id.is_(None))
        else:
            q = q.filter(CalendarDiscount.category_id == source_cd.category_id)
        return q.first()

    conflict_set: set[str] = set()
    existing_map: dict[date, CalendarDiscount] = {}
    for d in target_dates:
        existing = _find_existing(d)
        if existing:
            conflict_set.add(d.isoformat())
            existing_map[d] = existing

    # Etapa 1: apenas informa conflitos, aguarda decisão do operador.
    if conflict_set and on_conflict is None:
        return jsonify({
            "ok": False,
            "conflitos": sorted(conflict_set),
            "sem_conflito": sorted(
                d.isoformat() for d in target_dates if d.isoformat() not in conflict_set
            ),
        }), 409

    criados = substituidos = pulados = 0
    ator_id = _get_ator_id()

    for d in target_dates:
        d_str = d.isoformat()
        if d_str in conflict_set:
            if on_conflict == "skip":
                pulados += 1
                continue
            # overwrite: apaga tiers existentes e reutiliza o CalendarDiscount pai.
            dest_cd = existing_map[d]
            for t in list(dest_cd.advance_tiers):
                db.session.delete(t)
            db.session.flush()
            substituidos += 1
        else:
            dest_cd = CalendarDiscount(
                source_id=source_cd.source_id,
                category_id=source_cd.category_id,
                discount_date=d,
                label=source_cd.label,
                is_active=source_cd.is_active,
            )
            db.session.add(dest_cd)
            db.session.flush()
            criados += 1

        for t in source_cd.advance_tiers:
            db.session.add(CalendarDiscountAdvanceTier(
                calendar_discount_id=dest_cd.id,
                min_days_in_advance=t.min_days_in_advance,
                min_stay_days=t.min_stay_days,
                discount_percentage=t.discount_percentage,
            ))

    db.session.commit()

    AuditService.log_update(
        module="calendar_discount",
        entity_id=cd_id,
        previous_state={"action": "replicar"},
        new_state={
            "target_dates": [d.isoformat() for d in target_dates],
            "criados": criados,
            "substituidos": substituidos,
            "pulados": pulados,
        },
        actor_id=ator_id,
    )

    return jsonify({"ok": True, "criados": criados, "substituidos": substituidos, "pulados": pulados}), 200


# ══════════════════════════════════════════════════════════════════════════════
# Datas especiais (Calendário Global) geridas pelo módulo de Reservas
#
# A tabela é a mesma do Calendário Global (`sistur_global_events`) — o mandato do
# CLAUDE.md proíbe tabela de feriado por módulo. O que separa os dois lados é a
# POLÍTICA: aqui só se cria evento de preço (`afeta_precificacao=True`,
# `afeta_folha=False`, `origem='reservas'`), e só se altera o que continua sendo
# só de preço. Assim que o super admin promove o evento a evento de folha, ele
# passa a ser exclusivo das Configurações Globais.
# ══════════════════════════════════════════════════════════════════════════════

# Só converte string → enum. Quais tipos o módulo pode cadastrar é política, e
# mora no CalendarService — assim a mensagem de recusa é a dele, específica.
_TIPO_EVENTO_RESERVAS = {tipo.value: tipo for tipo in TipoEvento}


def _evento_redirect():
    """Monta o redirect de volta ao calendário no mês/fonte em que o usuário estava.

    Returns:
        Response de redirecionamento para `admin_ajustes_preco`.
    """
    return redirect(url_for(
        "reservas.admin_ajustes_preco",
        cal_source=request.form.get("cal_source") or None,
        cal_year=request.form.get("open_year") or None,
        cal_month=request.form.get("open_month") or None,
    ))


def _parse_evento_form():
    """Lê e valida os campos de data especial vindos do formulário.

    Form fields:
        titulo (str): Nome do evento. Obrigatório.
        data_evento (str): Primeiro dia, ISO (YYYY-MM-DD). Obrigatório.
        data_fim (str): Último dia, ISO. Opcional — vazio = evento de um dia.
        tipo (str): NATIONAL_HOLIDAY | LOCAL_HOLIDAY | HIGH_SEASON.
        recorrente_anual (str): "1" para repetir todo ano.

    Returns:
        Tupla (titulo, data_evento, data_fim, tipo, recorrente_anual).

    Raises:
        ValueError: Se algum campo obrigatório faltar ou a data for inválida.
    """
    titulo = (request.form.get("titulo") or "").strip()
    if not titulo:
        raise ValueError("Informe o título do evento.")

    try:
        data_evento = date.fromisoformat(request.form["data_evento"])
    except (KeyError, ValueError):
        raise ValueError("Data inicial inválida.")

    data_fim_raw = (request.form.get("data_fim") or "").strip()
    data_fim = None
    if data_fim_raw:
        try:
            data_fim = date.fromisoformat(data_fim_raw)
        except ValueError:
            raise ValueError("Data final inválida.")

    tipo = _TIPO_EVENTO_RESERVAS.get(request.form.get("tipo", ""))
    if tipo is None:
        raise ValueError("Tipo de evento inválido.")

    return (
        titulo,
        data_evento,
        data_fim,
        tipo,
        request.form.get("recorrente_anual") == "1",
    )


@bp.route("/admin/config/ajustes-preco/calendar/evento/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_evento_criar():
    """Cria uma data especial (feriado ou alta temporada) pelo módulo de Reservas.

    O evento nasce afetando apenas precificação — promovê-lo a evento de folha
    é prerrogativa do super admin, em `/admin/configuracoes/`.

    Form fields: ver `_parse_evento_form`, mais `cal_source`, `open_year` e
    `open_month` para reabrir o calendário no mesmo ponto.
    """
    ator_id = session.get("funcionario_id")
    try:
        titulo, data_evento, data_fim, tipo, recorrente = _parse_evento_form()
        CalendarService.criar_por_reservas(
            titulo=titulo,
            data_evento=data_evento,
            data_fim=data_fim,
            tipo=tipo,
            recorrente_anual=recorrente,
            ator_id=ator_id,
        )
        flash(f"Data especial '{titulo}' criada.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return _evento_redirect()


@bp.route(
    "/admin/config/ajustes-preco/calendar/evento/<int:event_id>/editar",
    methods=["POST"],
)
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_evento_editar(event_id: int):
    """Edita uma data especial criada pelo módulo de Reservas.

    Bloqueia eventos de origem global e eventos já promovidos a folha.

    Args:
        event_id: PK do GlobalEvent a editar.
    """
    ator_id = session.get("funcionario_id")
    try:
        titulo, data_evento, data_fim, tipo, recorrente = _parse_evento_form()
        CalendarService.atualizar_por_reservas(
            event_id=event_id,
            titulo=titulo,
            data_evento=data_evento,
            data_fim=data_fim,
            tipo=tipo,
            recorrente_anual=recorrente,
            ator_id=ator_id,
        )
        flash(f"Data especial '{titulo}' atualizada.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return _evento_redirect()


@bp.route(
    "/admin/config/ajustes-preco/calendar/evento/<int:event_id>/deletar",
    methods=["POST"],
)
@login_required
@require_permission("reservas", "configurar")
def admin_calendar_evento_deletar(event_id: int):
    """Remove uma data especial criada pelo módulo de Reservas.

    Bloqueia eventos de origem global e eventos já promovidos a folha.

    Args:
        event_id: PK do GlobalEvent a remover.
    """
    ator_id = session.get("funcionario_id")
    try:
        titulo = CalendarService.deletar_por_reservas(
            event_id=event_id, ator_id=ator_id)
        flash(f"Data especial '{titulo}' removida.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return _evento_redirect()


def _render_edit_form_error(reserva: "Reserva", error: str):
    """Recarrega o formulário de edição com mensagem de erro preservando contexto.

    Args:
        reserva: Instância da reserva em edição.
        error: Mensagem de erro a exibir no formulário.

    Returns:
        Renderização de reservas/admin/reserva_edit.html com status 400.
    """
    itens_disponiveis = []
    if reserva.category_id:
        itens_disponiveis = db.session.query(ReservaItem).filter(
            ReservaItem.category_id == reserva.category_id,
            ReservaItem.deleted_at.is_(None),
        ).order_by(ReservaItem.name).all()

    qtd_atual: dict[int, int] = {
        link.item_id: link.quantity
        for link in reserva.items
        if link.deleted_at is None and link.item_id is not None
    }

    telefone_atual = ""
    if reserva.metadata_info:
        telefone_atual = reserva.metadata_info.get("telefone", "")

    status_configs = (
        db.session.query(ReservaStatusConfig)
        .filter(ReservaStatusConfig.internal_name != "ARCHIVED_VERSION")
        .order_by(ReservaStatusConfig.id)
        .all()
    )

    return render_template(
        "reservas/admin/reserva_edit.html",
        reserva=reserva,
        itens_disponiveis=itens_disponiveis,
        qtd_atual=qtd_atual,
        telefone_atual=telefone_atual,
        status_configs=status_configs,
        desconto_efetivo_pct=ReservaService.desconto_efetivo_pct(reserva),
        error=error,
    ), 400


# ---------------------------------------------------------------------------
# Admin CRUD Routes (ReservaSource & ReservaCategory Management)
# ---------------------------------------------------------------------------


@bp.route("/admin/sources", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_listar_sources():
    """Lista todas as ReservaSource (venues) ativas para gerenciamento.

    Returns:
        Renderização de reservas/admin/sources_list.html.
    """
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    return render_template(
        "reservas/admin/sources_list.html",
        sources=sources,
    )


def _chatwoot_inboxes() -> list[dict]:
    """Busca a lista de inboxes disponíveis no Chatwoot via API interna.

    Usado para popular o seletor de inbox no formulário de sources.

    Returns:
        Lista de dicts com id e name de cada inbox, ou lista vazia em caso de erro.
    """
    import json as _json
    import urllib.request as _urllib
    from flask import current_app

    api_token = current_app.config.get("CHATWOOT_API_TOKEN", "")
    account_id = current_app.config.get("CHATWOOT_ACCOUNT_ID", "1")
    if not api_token:
        return []

    url = f"{_CHATWOOT_API_URL}/api/v1/accounts/{account_id}/inboxes"
    req = _urllib.Request(url, headers={
        "api_access_token": api_token,
        "Content-Type": "application/json",
    })
    try:
        with _urllib.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
            return [
                {"id": i["id"], "name": i["name"]}
                for i in data.get("payload", [])
            ]
    except Exception:
        return []


@bp.route("/admin/sources/novo", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_criar_source():
    """Cria uma nova ReservaSource (venue).

    GET: Renderiza formulário em branco com lista de inboxes do Chatwoot
    POST: Processa submissão do formulário

    Returns:
        GET: Renderização de reservas/admin/source_form.html
        POST: Redirect para admin_listar_sources() ou renderiza form com erros
    """
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()

        if not display_name:
            return render_template(
                "reservas/admin/source_form.html",
                source=None,
                chatwoot_inboxes=_chatwoot_inboxes(),
                error="Nome de exibição é obrigatório."
            ), 400

        # O identificador de sistema (name) é derivado do display_name no momento
        # da criação e nunca mais alterado — garante compatibilidade com sistemas externos.
        name = display_name

        # Check if name already exists (active records)
        existing = db.session.query(ReservaSource).filter(
            db.func.lower(ReservaSource.name) == name.lower(),
            ReservaSource.deleted_at.is_(None)
        ).first()

        if existing:
            return render_template(
                "reservas/admin/source_form.html",
                source=None,
                chatwoot_inboxes=_chatwoot_inboxes(),
                error=f"Já existe um local com o identificador '{name}'. Escolha um nome diferente."
            ), 400

        inbox_id_raw = request.form.get("inbox_id", "").strip()
        inbox_id = int(inbox_id_raw) if inbox_id_raw.isdigit() else None
        report_name = request.form.get("report_name", "").strip() or None
        report_logo = request.form.get("report_logo", "").strip() or None

        source = ReservaSource(
            name=name,
            display_name=display_name,
            inbox_id=inbox_id,
            report_name=report_name,
            report_logo=report_logo,
        )
        db.session.add(source)
        db.session.commit()

        flash(f"Local '{display_name}' criado com sucesso.", "success")
        return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_listar_sources"))

    return render_template(
        "reservas/admin/source_form.html",
        source=None,
        chatwoot_inboxes=_chatwoot_inboxes(),
    )


@bp.route("/admin/sources/<int:source_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_editar_source(source_id):
    """Edita uma ReservaSource existente.

    GET: Renderiza formulário preenchido
    POST: Processa atualização

    Returns:
        GET: Renderização de reservas/admin/source_form.html
        POST: Redirect ou formulário com erros
    """
    source = db.session.get(ReservaSource, source_id)

    if not source or source.deleted_at is not None:
        abort(404)

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()

        if not display_name:
            return render_template(
                "reservas/admin/source_form.html",
                source=source,
                chatwoot_inboxes=_chatwoot_inboxes(),
                error="Nome de exibição é obrigatório."
            ), 400

        # Apenas display_name, inbox e campos de relatório são editáveis —
        # name (identificador de sistema) permanece imutável.
        inbox_id_raw = request.form.get("inbox_id", "").strip()
        source.display_name = display_name
        source.inbox_id = int(inbox_id_raw) if inbox_id_raw.isdigit() else None
        source.report_name = request.form.get("report_name", "").strip() or None
        source.report_logo = request.form.get("report_logo", "").strip() or None
        db.session.commit()

        flash(f"Local '{display_name}' atualizado com sucesso.", "success")
        return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_listar_sources"))

    return render_template(
        "reservas/admin/source_form.html",
        source=source,
        chatwoot_inboxes=_chatwoot_inboxes(),
    )


@bp.route("/admin/sources/<int:source_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_deletar_source(source_id):
    """Soft-delete de uma ReservaSource (marca com deleted_at).

    Returns:
        Redirect para admin_listar_sources()
    """
    source = db.session.get(ReservaSource, source_id)

    if not source or source.deleted_at is not None:
        abort(404)

    from datetime import datetime, timezone
    source.deleted_at = datetime.now(timezone.utc)
    db.session.commit()


    flash(f"Fonte '{source.name}' deletada com sucesso.", "success")
    return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_listar_sources"))


@bp.route("/admin/sources/<int:source_id>/categorias", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_listar_categorias(source_id):
    """Lista todas as ReservaCategory para uma fonte específica.

    Returns:
        Renderização de reservas/admin/categories_list.html.
    """
    source = db.session.get(ReservaSource, source_id)

    if not source or source.deleted_at is not None:
        abort(404)

    categories = db.session.query(ReservaCategory).filter(
        ReservaCategory.source_id == source_id,
        ReservaCategory.deleted_at.is_(None)
    ).order_by(ReservaCategory.name).all()

    return render_template(
        "reservas/admin/categories_list.html",
        source=source,
        categories=categories,
    )


@bp.route("/admin/categorias/nova", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_criar_categoria():
    """Cria uma nova ReservaCategory.

    GET: Renderiza formulário com dropdown de fonte
    POST: Processa submissão

    Returns:
        GET: Renderização de reservas/admin/category_form.html
        POST: Redirect ou formulário com erros
    """
    # Fetch all active sources for dropdown
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    if request.method == "POST":
        source_id = request.form.get("source_id", "")
        name = request.form.get("name", "").strip()
        single_day_only = request.form.get("single_day_only") == "on"
        same_day_cutoff_raw = request.form.get("same_day_cutoff_time", "").strip()

        try:
            source_id = int(source_id)
        except (ValueError, TypeError):
            return render_template(
                "reservas/admin/category_form.html",
                category=None,
                sources=sources,
                error="Fonte é obrigatória."
            ), 400

        if not name:
            return render_template(
                "reservas/admin/category_form.html",
                category=None,
                sources=sources,
                error="Nome da categoria é obrigatório."
            ), 400

        same_day_cutoff_time = None
        if same_day_cutoff_raw:
            try:
                same_day_cutoff_time = time.fromisoformat(same_day_cutoff_raw)
            except ValueError:
                return render_template(
                    "reservas/admin/category_form.html",
                    category=None,
                    sources=sources,
                    error="Horário limite para reserva no mesmo dia inválido."
                ), 400

        try:
            janela = _janela_de_estadia(request.form)
        except ValueError as exc:
            return render_template(
                "reservas/admin/category_form.html",
                category=None,
                sources=sources,
                error=f"Janela de horário inválida. {exc}".strip()
            ), 400

        # Verify source exists
        source = db.session.get(ReservaSource, source_id)
        if not source or source.deleted_at is not None:
            return render_template(
                "reservas/admin/category_form.html",
                category=None,
                sources=sources,
                error="Fonte inválida."
            ), 400

        # Check if category name already exists in this source
        existing = db.session.query(ReservaCategory).filter(
            ReservaCategory.source_id == source_id,
            db.func.lower(ReservaCategory.name) == name.lower(),
            ReservaCategory.deleted_at.is_(None)
        ).first()

        if existing:
            return render_template(
                "reservas/admin/category_form.html",
                category=None,
                sources=sources,
                error=f"Categoria '{name}' já existe nesta fonte."
            ), 400

        # Create category
        category = ReservaCategory(
            source_id=source_id,
            name=name,
            single_day_only=single_day_only,
            same_day_cutoff_time=same_day_cutoff_time,
            imagem_url=request.form.get("imagem_url", "").strip() or None,
            tema=request.form.get("tema", "").strip() or None,
            **janela,
        )
        db.session.add(category)
        db.session.commit()

    
        flash(f"Categoria '{name}' criada com sucesso.", "success")
        return __import__('flask').redirect(
            __import__('flask').url_for("reservas.admin_listar_categorias", source_id=source_id)
        )

    return render_template(
        "reservas/admin/category_form.html",
        category=None,
        sources=sources,
    )


@bp.route("/admin/categorias/<int:category_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_editar_categoria(category_id):
    """Edita uma ReservaCategory existente.

    GET: Renderiza formulário preenchido
    POST: Processa atualização

    Returns:
        GET: Renderização de reservas/admin/category_form.html
        POST: Redirect ou formulário com erros
    """
    category = db.session.get(ReservaCategory, category_id)

    if not category or category.deleted_at is not None:
        abort(404)

    # Fetch all active sources for dropdown
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    if request.method == "POST":
        source_id = request.form.get("source_id", "")
        name = request.form.get("name", "").strip()
        single_day_only = request.form.get("single_day_only") == "on"
        same_day_cutoff_raw = request.form.get("same_day_cutoff_time", "").strip()

        try:
            source_id = int(source_id)
        except (ValueError, TypeError):
            return render_template(
                "reservas/admin/category_form.html",
                category=category,
                sources=sources,
                error="Fonte é obrigatória."
            ), 400

        if not name:
            return render_template(
                "reservas/admin/category_form.html",
                category=category,
                sources=sources,
                error="Nome da categoria é obrigatório."
            ), 400

        same_day_cutoff_time = None
        if same_day_cutoff_raw:
            try:
                same_day_cutoff_time = time.fromisoformat(same_day_cutoff_raw)
            except ValueError:
                return render_template(
                    "reservas/admin/category_form.html",
                    category=category,
                    sources=sources,
                    error="Horário limite para reserva no mesmo dia inválido."
                ), 400

        try:
            janela = _janela_de_estadia(request.form)
        except ValueError as exc:
            return render_template(
                "reservas/admin/category_form.html",
                category=category,
                sources=sources,
                error=f"Janela de horário inválida. {exc}".strip()
            ), 400

        # Verify source exists
        source = db.session.get(ReservaSource, source_id)
        if not source or source.deleted_at is not None:
            return render_template(
                "reservas/admin/category_form.html",
                category=category,
                sources=sources,
                error="Fonte inválida."
            ), 400

        # Check if category name already exists in this source (excluding current record)
        existing = db.session.query(ReservaCategory).filter(
            ReservaCategory.id != category_id,
            ReservaCategory.source_id == source_id,
            db.func.lower(ReservaCategory.name) == name.lower(),
            ReservaCategory.deleted_at.is_(None)
        ).first()

        if existing:
            return render_template(
                "reservas/admin/category_form.html",
                category=category,
                sources=sources,
                error=f"Categoria '{name}' já existe nesta fonte."
            ), 400

        category.source_id = source_id
        category.name = name
        category.single_day_only = single_day_only
        category.same_day_cutoff_time = same_day_cutoff_time
        category.imagem_url = request.form.get("imagem_url", "").strip() or None
        category.tema = request.form.get("tema", "").strip() or None
        for campo, valor in janela.items():
            setattr(category, campo, valor)
        db.session.commit()

    
        flash(f"Categoria '{name}' atualizada com sucesso.", "success")
        return __import__('flask').redirect(
            __import__('flask').url_for("reservas.admin_listar_categorias", source_id=source_id)
        )

    return render_template(
        "reservas/admin/category_form.html",
        category=category,
        sources=sources,
    )


@bp.route("/admin/categorias/<int:category_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_deletar_categoria(category_id):
    """Soft-delete de uma ReservaCategory (marca com deleted_at).

    Returns:
        Redirect para admin_listar_categorias() com source_id da categoria
    """
    category = db.session.get(ReservaCategory, category_id)

    if not category or category.deleted_at is not None:
        abort(404)

    source_id = category.source_id

    from datetime import datetime, timezone
    category.deleted_at = datetime.now(timezone.utc)
    db.session.commit()


    flash(f"Categoria '{category.name}' deletada com sucesso.", "success")
    return __import__('flask').redirect(
        __import__('flask').url_for("reservas.admin_listar_categorias", source_id=source_id)
    )


# ---------------------------------------------------------------------------
# Admin: ReservaResource Management (Physical Inventory)
# ---------------------------------------------------------------------------


@bp.route("/admin/recursos", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_listar_recursos():
    """Lista todos os ReservaResource (capacidades/estoque físico) ativos.

    Organiza os dados em uma estrutura de árvore baseada em ReservaGroup.

    Query params:
        source_id (int, opcional): filtra recursos e grupos exibidos por
            local/venue. Uma raiz de agrupamento aparece se seus `sources`
            incluírem aquele local — sub-grupos sempre herdam do ancestral
            raiz, então a árvore inteira daquela raiz acompanha. Quando
            ausente, mostra tudo de todos os locais (padrão atual).
    """
    selected_source_id = request.args.get("source_id", type=int)

    # 1. Fetch all active groups (ordered) — filtragem por venue acontece
    # depois de montar a árvore (ver root_groups abaixo), pois sub-grupos
    # não têm `sources` próprios.
    all_groups = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )

    # 2. Fetch all active resources (optionally filtered by venue)
    resources_query = db.session.query(ReservaResource).filter(
        ReservaResource.deleted_at.is_(None)
    )
    if selected_source_id:
        resources_query = resources_query.filter(ReservaResource.source_id == selected_source_id)
    all_resources = resources_query.order_by(ReservaResource.name).all()

    # 2b. Sources for the venue filter dropdown
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    # 3. Organize recursively
    resources_by_group = {}
    ungrouped_resources = []
    for r in all_resources:
        if r.group_id:
            if r.group_id not in resources_by_group:
                resources_by_group[r.group_id] = []
            resources_by_group[r.group_id].append(r)
        else:
            ungrouped_resources.append(r)

    # Build tree
    group_map = {g.id: {"group": g, "children": [], "resources_list": resources_by_group.get(g.id, [])} for g in all_groups}
    root_groups = []
    for g in all_groups:
        if g.parent_id and g.parent_id in group_map:
            group_map[g.parent_id]["children"].append(group_map[g.id])
        else:
            root_groups.append(group_map[g.id])

    if selected_source_id:
        root_groups = [
            rg for rg in root_groups
            if selected_source_id in {s.id for s in rg["group"].sources}
        ]

    return render_template(
        "reservas/admin/resources_list.html",
        root_groups=root_groups,
        ungrouped_resources=ungrouped_resources,
        sources=sources,
        selected_source_id=selected_source_id,
    )


@bp.route("/admin/recursos/novo", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_criar_recurso():
    """Cria um novo ReservaResource (capacity/inventory pool).

    GET: Renderiza formulário em branco, com suporte a pré-seleção via
        query string (`group_id`, `source_id`) — usado pelo botão
        "Criar recurso aqui" na árvore de agrupamentos.
    POST: Processa submissão do formulário

    Returns:
        GET: Renderização de reservas/admin/resource_form.html
        POST: Redirect para admin_listar_recursos() ou renderiza form com erros
    """
    selected_group_id = request.args.get("group_id", type=int)
    selected_source_id = request.args.get("source_id", type=int)

    # Fetch all active sources and groups for dropdowns
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    groups = db.session.query(ReservaGroup).filter(
        ReservaGroup.deleted_at.is_(None)
    ).order_by(ReservaGroup.name).all()

    if request.method == "POST":
        source_id = request.form.get("source_id", "")
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        image_url = request.form.get("image_url", "").strip() or None
        stock_input = request.form.get("stock_quantity", "").strip()
        is_unlimited = request.form.get("is_unlimited", "off") == "on"
        group_id_raw = request.form.get("group_id", "").strip()

        try:
            source_id = int(source_id)
        except (ValueError, TypeError):
            return render_template(
                "reservas/admin/resource_form.html",
                resource=None, sources=sources, groups=groups,
                error="Fonte é obrigatória."
            ), 400

        if not name:
            return render_template(
                "reservas/admin/resource_form.html",
                resource=None, sources=sources, groups=groups,
                error="Nome do recurso é obrigatório."
            ), 400

        # Verify source exists
        source = db.session.get(ReservaSource, source_id)
        if not source or source.deleted_at is not None:
            return render_template(
                "reservas/admin/resource_form.html",
                resource=None, sources=sources, groups=groups,
                error="Fonte inválida."
            ), 400

        # Check if resource name already exists in this source
        existing = db.session.query(ReservaResource).filter(
            ReservaResource.source_id == source_id,
            db.func.lower(ReservaResource.name) == name.lower(),
            ReservaResource.deleted_at.is_(None)
        ).first()

        if existing:
            return render_template(
                "reservas/admin/resource_form.html",
                resource=None, sources=sources, groups=groups,
                error=f"Recurso '{name}' já existe nesta fonte."
            ), 400

        # Resolver group_id opcional
        group_id = None
        if group_id_raw:
            try:
                group_id = int(group_id_raw)
                grp = db.session.get(ReservaGroup, group_id)
                if not grp or grp.deleted_at is not None:
                    group_id = None
            except (ValueError, TypeError):
                group_id = None

        # Handle stock quantity: unlimited (NULL) or integer
        stock_quantity = None
        if not is_unlimited:
            if not stock_input:
                return render_template(
                    "reservas/admin/resource_form.html",
                    resource=None, sources=sources, groups=groups,
                    error="Informe a capacidade ou marque 'Sem limite'."
                ), 400
            try:
                stock_quantity = int(stock_input)
                if stock_quantity < 0:
                    raise ValueError("Capacidade não pode ser negativa.")
            except ValueError as e:
                return render_template(
                    "reservas/admin/resource_form.html",
                    resource=None, sources=sources, groups=groups,
                    error=f"Capacidade inválida: {str(e)}"
                ), 400

        # Create resource
        resource = ReservaResource(
            source_id=source_id,
            group_id=group_id,
            name=name,
            description=description,
            image_url=image_url,
            stock_quantity=stock_quantity,
        )
        db.session.add(resource)
        db.session.flush()  # gera resource.id antes de criar imagens

        # Galeria: persistir lista de URLs enviadas pelo formulário
        image_urls_raw = request.form.getlist("image_urls[]")
        sort_orders_raw = request.form.getlist("sort_orders[]")
        for idx, url in enumerate(image_urls_raw):
            url = url.strip()
            if not url:
                continue
            try:
                order = int(sort_orders_raw[idx]) if idx < len(sort_orders_raw) else idx
            except (ValueError, TypeError):
                order = idx
            db.session.add(ResourceImage(
                resource_id=resource.id,
                image_url=url,
                sort_order=order,
            ))

        db.session.commit()

        flash(f"Recurso '{name}' criado com sucesso.", "success")
        return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_listar_recursos"))

    return render_template(
        "reservas/admin/resource_form.html",
        resource=None, sources=sources, groups=groups,
        selected_group_id=selected_group_id,
        selected_source_id=selected_source_id,
    )


@bp.route("/admin/recursos/<int:resource_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_editar_recurso(resource_id):
    """Edita um ReservaResource existente.

    GET: Renderiza formulário preenchido
    POST: Processa atualização

    Returns:
        GET: Renderização de reservas/admin/resource_form.html
        POST: Redirect ou formulário com erros
    """
    resource = db.session.get(ReservaResource, resource_id)

    if not resource or resource.deleted_at is not None:
        abort(404)

    # Fetch all active sources and groups for dropdowns
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    groups = db.session.query(ReservaGroup).filter(
        ReservaGroup.deleted_at.is_(None)
    ).order_by(ReservaGroup.name).all()

    if request.method == "POST":
        source_id = request.form.get("source_id", "")
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        image_url = request.form.get("image_url", "").strip() or None
        stock_input = request.form.get("stock_quantity", "").strip()
        is_unlimited = request.form.get("is_unlimited", "off") == "on"
        group_id_raw = request.form.get("group_id", "").strip()

        try:
            source_id = int(source_id)
        except (ValueError, TypeError):
            return render_template(
                "reservas/admin/resource_form.html",
                resource=resource, sources=sources, groups=groups,
                error="Fonte é obrigatória."
            ), 400

        if not name:
            return render_template(
                "reservas/admin/resource_form.html",
                resource=resource, sources=sources, groups=groups,
                error="Nome do recurso é obrigatório."
            ), 400

        # Verify source exists
        source = db.session.get(ReservaSource, source_id)
        if not source or source.deleted_at is not None:
            return render_template(
                "reservas/admin/resource_form.html",
                resource=resource, sources=sources, groups=groups,
                error="Fonte inválida."
            ), 400

        # Check if resource name already exists in this source (excluding current)
        existing = db.session.query(ReservaResource).filter(
            ReservaResource.id != resource_id,
            ReservaResource.source_id == source_id,
            db.func.lower(ReservaResource.name) == name.lower(),
            ReservaResource.deleted_at.is_(None)
        ).first()

        if existing:
            return render_template(
                "reservas/admin/resource_form.html",
                resource=resource, sources=sources, groups=groups,
                error=f"Recurso '{name}' já existe nesta fonte."
            ), 400

        # Resolver group_id opcional
        group_id = None
        if group_id_raw:
            try:
                group_id = int(group_id_raw)
                grp = db.session.get(ReservaGroup, group_id)
                if not grp or grp.deleted_at is not None:
                    group_id = None
            except (ValueError, TypeError):
                group_id = None

        # Handle stock quantity
        stock_quantity = None
        if not is_unlimited:
            if not stock_input:
                return render_template(
                    "reservas/admin/resource_form.html",
                    resource=resource, sources=sources, groups=groups,
                    error="Informe a capacidade ou marque 'Sem limite'."
                ), 400
            try:
                stock_quantity = int(stock_input)
                if stock_quantity < 0:
                    raise ValueError("Capacidade não pode ser negativa.")
            except ValueError as e:
                return render_template(
                    "reservas/admin/resource_form.html",
                    resource=resource, sources=sources, groups=groups,
                    error=f"Capacidade inválida: {str(e)}"
                ), 400

        resource.source_id = source_id
        resource.group_id = group_id
        resource.name = name
        resource.description = description
        resource.image_url = image_url
        resource.stock_quantity = stock_quantity

        # Galeria: soft-delete das imagens existentes e recriar com os dados do form.
        # Simples e sem ambiguidade de ordenação.
        from datetime import datetime, timezone as _tz
        now = datetime.now(_tz.utc)
        for img in db.session.query(ResourceImage).filter_by(
            resource_id=resource.id, deleted_at=None
        ).all():
            img.deleted_at = now

        image_urls_raw = request.form.getlist("image_urls[]")
        sort_orders_raw = request.form.getlist("sort_orders[]")
        for idx, url in enumerate(image_urls_raw):
            url = url.strip()
            if not url:
                continue
            try:
                order = int(sort_orders_raw[idx]) if idx < len(sort_orders_raw) else idx
            except (ValueError, TypeError):
                order = idx
            db.session.add(ResourceImage(
                resource_id=resource.id,
                image_url=url,
                sort_order=order,
            ))

        db.session.commit()

        flash(f"Recurso '{name}' atualizado com sucesso.", "success")
        return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_listar_recursos"))

    return render_template("reservas/admin/resource_form.html", resource=resource, sources=sources, groups=groups)


@bp.route("/admin/recursos/<int:resource_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_deletar_recurso(resource_id):
    """Soft-delete de um ReservaResource (marca com deleted_at).

    Returns:
        Redirect para admin_listar_recursos()
    """
    resource = db.session.get(ReservaResource, resource_id)

    if not resource or resource.deleted_at is not None:
        abort(404)

    from datetime import datetime, timezone
    resource.deleted_at = datetime.now(timezone.utc)
    db.session.commit()


    flash(f"Recurso '{resource.name}' deletado com sucesso.", "success")
    return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_listar_recursos"))



@bp.route("/admin/recursos/todos", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_todos_recursos():
    """Lista todos os ReservaResource, incluindo os soft-deletados.

    Permite visualizar, restaurar ou excluir definitivamente recursos que não
    aparecem na listagem principal por estarem marcados com deleted_at.

    Returns:
        Renderização do template resources_all.html com todos os recursos.
    """
    all_resources = (
        db.session.query(ReservaResource)
        .order_by(ReservaResource.deleted_at.is_(None).desc(), ReservaResource.name)
        .all()
    )
    return render_template(
        "reservas/admin/resources_all.html",
        all_resources=all_resources,
    )


@bp.route("/admin/recursos/<int:resource_id>/restaurar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_restaurar_recurso(resource_id):
    """Restaura um ReservaResource soft-deletado (limpa deleted_at).

    Args:
        resource_id: ID do recurso a ser restaurado.

    Returns:
        Redirect para admin_todos_recursos().
    """
    resource = db.session.get(ReservaResource, resource_id)

    if not resource or resource.deleted_at is None:
        abort(404)

    resource.deleted_at = None
    db.session.commit()

    flash(f"Recurso '{resource.name}' restaurado com sucesso.", "success")
    return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_todos_recursos"))


@bp.route("/admin/recursos/<int:resource_id>/excluir-definitivo", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_excluir_definitivo_recurso(resource_id):
    """Exclui permanentemente um ReservaResource já soft-deletado.

    Só é permitido para recursos já marcados com deleted_at. Libera o nome
    para reutilização (desfaz a constraint unique source_id + name).
    Links de reserva com este recurso são automaticamente nulificados (SET NULL).

    Args:
        resource_id: ID do recurso a ser excluído permanentemente.

    Returns:
        Redirect para admin_todos_recursos().
    """
    resource = db.session.get(ReservaResource, resource_id)

    if not resource or resource.deleted_at is None:
        abort(404)

    nome = resource.name
    db.session.delete(resource)
    db.session.commit()

    flash(f"Recurso '{nome}' excluído permanentemente.", "success")
    return __import__('flask').redirect(__import__('flask').url_for("reservas.admin_todos_recursos"))


# ---------------------------------------------------------------------------
# ReservaGroup Admin Routes (Hierarchical Grouping)
# ---------------------------------------------------------------------------


@bp.route("/admin/grupos", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_listar_grupos():
    """Lista todos os ReservaGroup ativos (deleted_at IS NULL) em árvore.

    Organiza os dados em uma estrutura de árvore (raiz → sub-grupos em
    profundidade arbitrária), mesmo padrão de admin_listar_recursos() e
    admin_listar_itens().

    Query params:
        source_id (int, opcional): filtra a árvore por local/venue — mostra
            apenas as raízes cujos `sources` incluem o local selecionado
            (e toda a sua descendência, já que sub-grupos sempre herdam o
            local do ancestral raiz). Quando ausente, mostra tudo.

    Returns:
        Renderização de reservas/admin/groups_list.html.
    """
    selected_source_id = request.args.get("source_id", type=int)

    all_groups = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )
    sources = (
        db.session.query(ReservaSource)
        .filter(ReservaSource.deleted_at.is_(None))
        .order_by(ReservaSource.name)
        .all()
    )

    group_map = {g.id: {"group": g, "children": []} for g in all_groups}
    root_groups = []
    for g in all_groups:
        if g.parent_id and g.parent_id in group_map:
            group_map[g.parent_id]["children"].append(group_map[g.id])
        else:
            root_groups.append(group_map[g.id])

    if selected_source_id:
        root_groups = [
            rg for rg in root_groups
            if selected_source_id in {s.id for s in rg["group"].sources}
        ]

    return render_template(
        "reservas/admin/groups_list.html",
        root_groups=root_groups,
        sources=sources,
        selected_source_id=selected_source_id,
    )


@bp.route("/admin/grupos/novo", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_criar_grupo():
    """GET: Exibe formulário vazio. POST: Cria novo ReservaGroup.

    Form fields:
        name (str): Nome do grupo (obrigatório).
        source_ids (list[int]): IDs dos locais aos quais o grupo pertence.
            Obrigatório (ao menos um) quando `parent_id` vazio (grupo raiz).
            Ignorado quando `parent_id` é informado — sub-grupos nunca têm
            locais próprios, sempre herdam do ancestral raiz dinamicamente
            via ReservaGroup.effective_sources.
        parent_id (int): ID do grupo pai para criar sub-grupo (opcional).
        description (str): Descrição explicativa (opcional).
        sort_order (int): Ordem de exibição (opcional, padrão 0).

    Query params (GET):
        parent_id (int, opcional): pré-seleciona o grupo pai — usado pelo
            botão "Criar grupo filho" na árvore de agrupamentos.
        next (str, opcional): "recursos" ou "itens" fazem o formulário
            retornar para admin_listar_recursos() ou admin_listar_itens()
            (respectivamente) em vez de admin_listar_grupos() após o POST —
            usado pelos botões "Criar grupo filho" e "Novo Grupo" nas telas
            de recursos físicos e itens/tarifas, para manter o usuário no
            contexto da árvore de agrupamentos.

    Returns:
        GET: Renderização de reservas/admin/group_form.html com formulário vazio.
        POST: Redirect para admin_listar_grupos() (ou admin_listar_recursos()
            se `next=recursos`) ou formulário com erro (400).
    """
    sources = (
        db.session.query(ReservaSource)
        .filter(ReservaSource.deleted_at.is_(None))
        .order_by(ReservaSource.name)
        .all()
    )
    parent_groups = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )

    if request.method == "GET":
        return render_template(
            "reservas/admin/group_form.html",
            group=None,
            sources=sources,
            parent_groups=parent_groups,
            selected_parent_id=request.args.get("parent_id", type=int),
            next=request.args.get("next", ""),
        )

    name = request.form.get("name", "").strip()
    source_ids_raw = request.form.getlist("source_ids")
    parent_id_raw = request.form.get("parent_id", "").strip()
    description = request.form.get("description", "").strip() or None
    sort_order_raw = request.form.get("sort_order", "0").strip()
    next_target = request.form.get("next", "")

    def _render_error(msg: str):
        return render_template(
            "reservas/admin/group_form.html",
            group=None,
            sources=sources,
            parent_groups=parent_groups,
            error=msg,
            next=next_target,
        ), 400

    if not name:
        return _render_error("Nome do grupo é obrigatório.")

    parent_id = None
    if parent_id_raw:
        try:
            parent_id = int(parent_id_raw)
        except (ValueError, TypeError):
            return _render_error("Grupo pai inválido.")
        parent = db.session.get(ReservaGroup, parent_id)
        if not parent or parent.deleted_at is not None:
            return _render_error("Grupo pai inválido.")

    source_objs = []
    if parent_id is None:
        try:
            source_ids = [int(x) for x in source_ids_raw]
        except (ValueError, TypeError):
            return _render_error("Local inválido.")
        if not source_ids:
            return _render_error("Selecione ao menos um local.")
        source_objs = (
            db.session.query(ReservaSource)
            .filter(ReservaSource.id.in_(source_ids), ReservaSource.deleted_at.is_(None))
            .all()
        )
        if len(source_objs) != len(set(source_ids)):
            return _render_error("Um ou mais locais selecionados são inválidos.")

    try:
        sort_order = int(sort_order_raw) if sort_order_raw else 0
    except (ValueError, TypeError):
        sort_order = 0

    # Verify uniqueness among siblings (same parent_id)
    existing_query = db.session.query(ReservaGroup).filter(
        db.func.lower(ReservaGroup.name) == name.lower(),
        ReservaGroup.deleted_at.is_(None),
    )
    existing_query = existing_query.filter(
        ReservaGroup.parent_id.is_(None) if parent_id is None else ReservaGroup.parent_id == parent_id
    )
    if existing_query.first():
        return _render_error(f"Grupo '{name}' já existe neste nível.")

    group = ReservaGroup(
        parent_id=parent_id,
        name=name,
        description=description,
        sort_order=sort_order,
    )
    if parent_id is None:
        group.sources = source_objs
    db.session.add(group)
    db.session.commit()


    flash(f"Grupo '{name}' criado com sucesso.", "success")
    next_redirects = {
        "recursos": "reservas.admin_listar_recursos",
        "itens": "reservas.admin_listar_itens",
    }
    return redirect(url_for(next_redirects.get(next_target, "reservas.admin_listar_grupos")))


@bp.route("/admin/grupos/<int:group_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_editar_grupo(group_id: int):
    """GET: Exibe formulário preenchido. POST: Atualiza ReservaGroup.

    Args:
        group_id: ID do ReservaGroup a editar.

    Returns:
        GET: Renderização de reservas/admin/group_form.html com dados preenchidos.
        POST: Redirect para admin_listar_grupos() ou formulário com erro (400).

    Raises:
        404: Se o grupo não for encontrado ou estiver deletado.
    """
    group = db.session.get(ReservaGroup, group_id)
    if not group or group.deleted_at is not None:
        abort(404)

    sources = (
        db.session.query(ReservaSource)
        .filter(ReservaSource.deleted_at.is_(None))
        .order_by(ReservaSource.name)
        .all()
    )
    parent_groups = (
        db.session.query(ReservaGroup)
        .filter(
            ReservaGroup.deleted_at.is_(None),
            ReservaGroup.id != group_id,
        )
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )

    if request.method == "GET":
        return render_template(
            "reservas/admin/group_form.html",
            group=group,
            sources=sources,
            parent_groups=parent_groups,
        )

    name = request.form.get("name", "").strip()
    source_ids_raw = request.form.getlist("source_ids")
    parent_id_raw = request.form.get("parent_id", "").strip()
    description = request.form.get("description", "").strip() or None
    sort_order_raw = request.form.get("sort_order", "0").strip()

    def _render_error(msg: str):
        return render_template(
            "reservas/admin/group_form.html",
            group=group,
            sources=sources,
            parent_groups=parent_groups,
            error=msg,
        ), 400

    if not name:
        return _render_error("Nome do grupo é obrigatório.")

    parent_id = None
    if parent_id_raw:
        try:
            parent_id = int(parent_id_raw)
        except (ValueError, TypeError):
            return _render_error("Grupo pai inválido.")
        parent = db.session.get(ReservaGroup, parent_id)
        if not parent or parent.deleted_at is not None:
            return _render_error("Grupo pai inválido.")

    # Prevent self-reference
    if parent_id == group_id:
        return _render_error("Um grupo não pode ser seu próprio pai.")

    source_objs = []
    if parent_id is None:
        try:
            source_ids = [int(x) for x in source_ids_raw]
        except (ValueError, TypeError):
            return _render_error("Local inválido.")
        if not source_ids:
            return _render_error("Selecione ao menos um local.")
        source_objs = (
            db.session.query(ReservaSource)
            .filter(ReservaSource.id.in_(source_ids), ReservaSource.deleted_at.is_(None))
            .all()
        )
        if len(source_objs) != len(set(source_ids)):
            return _render_error("Um ou mais locais selecionados são inválidos.")

    try:
        sort_order = int(sort_order_raw) if sort_order_raw else 0
    except (ValueError, TypeError):
        sort_order = 0

    # Verify uniqueness among siblings (same parent_id), excluding current
    existing_query = db.session.query(ReservaGroup).filter(
        ReservaGroup.id != group_id,
        db.func.lower(ReservaGroup.name) == name.lower(),
        ReservaGroup.deleted_at.is_(None),
    )
    existing_query = existing_query.filter(
        ReservaGroup.parent_id.is_(None) if parent_id is None else ReservaGroup.parent_id == parent_id
    )
    if existing_query.first():
        return _render_error(f"Grupo '{name}' já existe neste nível.")

    group.parent_id = parent_id
    group.name = name
    group.description = description
    group.sort_order = sort_order
    group.sources = source_objs if parent_id is None else []
    db.session.commit()


    flash(f"Grupo '{name}' atualizado com sucesso.", "success")
    return redirect(url_for("reservas.admin_listar_grupos"))


@bp.route("/admin/grupos/<int:group_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_deletar_grupo(group_id: int):
    """Soft-delete de um ReservaGroup (marca deleted_at). Desvincula itens filhos.

    Ao deletar um grupo, os itens vinculados a ele têm group_id resetado para NULL
    automaticamente via ON DELETE SET NULL no banco de dados.

    Args:
        group_id: ID do ReservaGroup a deletar.

    Returns:
        Redirect para admin_listar_grupos().

    Raises:
        404: Se o grupo não for encontrado ou já estiver deletado.
    """
    from datetime import datetime, timezone

    group = db.session.get(ReservaGroup, group_id)
    if not group or group.deleted_at is not None:
        abort(404)

    # Soft-delete sub-groups as well
    for child in group.children:
        if child.deleted_at is None:
            child.deleted_at = datetime.now(timezone.utc)

    group.deleted_at = datetime.now(timezone.utc)
    db.session.commit()


    flash(f"Grupo '{group.name}' deletado com sucesso.", "success")
    return redirect(url_for("reservas.admin_listar_grupos"))


# ---------------------------------------------------------------------------
# ReservaItem Admin Routes (Pricing/Tariffs)
# ---------------------------------------------------------------------------


@bp.route("/admin/itens", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_listar_itens():
    """Lista todos os ReservaItem ativos (deleted_at IS NULL) em árvore de ReservaGroup.

    Organiza os dados em uma estrutura de árvore baseada em ReservaGroup, no mesmo
    padrão de admin_listar_recursos() — grupos raiz expansíveis revelando sub-grupos
    e itens (ao invés do agrupamento de nível único usado anteriormente).

    Também fornece ``slugs_data`` para o modal de dicionário de slugs, contendo
    apenas itens com ``internal_slug`` preenchido.

    Query params:
        source_id (int, opcional): filtra itens e grupos exibidos por
            local/venue. Itens são filtrados via join em ReservaCategory
            (ReservaItem não tem source_id direto). Uma raiz de agrupamento
            aparece se seus `sources` incluírem aquele local — sub-grupos
            sempre herdam do ancestral raiz. Quando ausente, mostra tudo de
            todos os locais (padrão atual).
    """
    selected_source_id = request.args.get("source_id", type=int)

    # 1. Fetch all active groups (ordered) — filtragem por venue acontece
    # depois de montar a árvore (ver root_groups abaixo), pois sub-grupos
    # não têm `sources` próprios.
    all_groups = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )

    # 2. Fetch all active items (optionally filtered by venue via category join)
    items_query = db.session.query(ReservaItem).filter(
        ReservaItem.deleted_at.is_(None)
    )
    if selected_source_id:
        items_query = items_query.join(ReservaItem.category).filter(
            ReservaCategory.source_id == selected_source_id
        )
    all_items = items_query.order_by(ReservaItem.name).all()

    # 2b. Sources for the venue filter dropdown
    sources = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None)
    ).order_by(ReservaSource.name).all()

    # 3. Organize recursively
    items_by_group = {}
    ungrouped_items = []
    for item in all_items:
        if item.group_id:
            if item.group_id not in items_by_group:
                items_by_group[item.group_id] = []
            items_by_group[item.group_id].append(item)
        else:
            ungrouped_items.append(item)

    # Build tree
    group_map = {g.id: {"group": g, "children": [], "items_list": items_by_group.get(g.id, [])} for g in all_groups}
    root_groups = []
    for g in all_groups:
        if g.parent_id and g.parent_id in group_map:
            group_map[g.parent_id]["children"].append(group_map[g.id])
        else:
            root_groups.append(group_map[g.id])

    if selected_source_id:
        root_groups = [
            rg for rg in root_groups
            if selected_source_id in {s.id for s in rg["group"].sources}
        ]

    # Slug dictionary — apenas itens com internal_slug preenchido
    slug_items = (
        db.session.query(ReservaItem)
        .join(ReservaItem.category)
        .filter(
            ReservaItem.internal_slug.isnot(None),
            ReservaItem.deleted_at.is_(None),
        )
        .order_by(ReservaItem.internal_slug)
        .all()
    )
    slugs_data = [
        {
            "slug": item.internal_slug,
            "name": item.name,
            "category": item.category.name if item.category else "",
            "source": item.category.source.name if item.category and item.category.source else "",
            "item_id": item.id,
        }
        for item in slug_items
    ]

    return render_template(
        "reservas/admin/items_list.html",
        root_groups=root_groups,
        ungrouped_items=ungrouped_items,
        slugs_data=slugs_data,
        sources=sources,
        selected_source_id=selected_source_id,
    )


@bp.route("/admin/itens/novo", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_criar_item():
    """GET: Exibe formulário vazio. POST: Cria novo ReservaItem.

    Query params (GET):
        group_id (int, opcional): pré-seleciona o grupo — usado pelo botão
            "Criar item aqui" na árvore de agrupamentos.

    Returns:
        GET: Template com formulário vazio.
        POST: Redirect para admin_listar_itens() ou renderiza formulário com erro.
    """
    # Query active categories and groups for dropdowns
    categories = db.session.query(ReservaCategory).filter(
        ReservaCategory.deleted_at.is_(None)
    ).order_by(ReservaCategory.name).all()

    groups = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )

    if request.method == "GET":
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            selected_group_id=request.args.get("group_id", type=int),
        )

    # POST: Create new item
    name = request.form.get("name", "").strip()
    category_id = request.form.get("category_id", "").strip()
    group_id_raw = request.form.get("group_id", "").strip()
    price_input = request.form.get("price", "").strip()
    billing_type = request.form.get("billing_type", "").strip()
    requires_deposit = request.form.get("requires_deposit") == "on"
    requires_resource = request.form.get("requires_resource") == "on"
    is_entry_ticket = request.form.get("is_entry_ticket") == "on"
    visible_to_bot = request.form.get("visible_to_bot") == "on"
    visible_to_web = request.form.get("visible_to_web") == "on"
    bot_description = request.form.get("bot_description", "").strip() or None
    emoji_label = request.form.get("emoji_label", "").strip() or None
    internal_slug = request.form.get("internal_slug", "").strip() or None
    price_weekday_input = request.form.get("price_weekday", "").strip() or None
    price_weekend_input = request.form.get("price_weekend", "").strip() or None
    price_holiday_input = request.form.get("price_holiday", "").strip() or None

    # Validate required fields
    if not name:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Nome do item é obrigatório."
        ), 400

    if not category_id:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Categoria é obrigatória."
        ), 400

    if not price_input:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Preço é obrigatório."
        ), 400

    if not billing_type:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Tipo de cobrança é obrigatório."
        ), 400

    # Parse and validate category_id
    try:
        category_id = int(category_id)
    except ValueError:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Categoria inválida."
        ), 400

    # Verify category exists and is active
    category = db.session.get(ReservaCategory, category_id)
    if not category or category.deleted_at is not None:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Categoria inválida."
        ), 400

    # Parse and validate price
    try:
        price = float(price_input)
        if price < 0:
            raise ValueError("Preço não pode ser negativo.")
    except ValueError as e:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error=f"Preço inválido: {str(e)}"
        ), 400

    # Parse optional price tiers
    try:
        price_weekday = float(price_weekday_input) if price_weekday_input is not None else None
        price_weekend = float(price_weekend_input) if price_weekend_input is not None else None
        price_holiday = float(price_holiday_input) if price_holiday_input is not None else None
    except ValueError:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Tarifa de dia inválida — use somente números."
        ), 400

    # Validate billing_type
    valid_billing_types = [bt.value for bt in BillingType]
    if billing_type not in valid_billing_types:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Tipo de cobrança inválido."
        ), 400

    # Parse group_id (optional)
    group_id = None
    if group_id_raw:
        try:
            group_id = int(group_id_raw)
            grp = db.session.get(ReservaGroup, group_id)
            if not grp or grp.deleted_at is not None:
                group_id = None
        except (ValueError, TypeError):
            group_id = None
    if requires_resource and group_id is None:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error="Itens que exigem recurso físico precisam estar em um grupo."
        ), 400

    # Check unique constraint (category_id, name)
    existing = db.session.query(ReservaItem).filter(
        ReservaItem.category_id == category_id,
        db.func.lower(ReservaItem.name) == name.lower(),
        ReservaItem.deleted_at.is_(None)
    ).first()

    if existing:
        return render_template(
            "reservas/admin/item_form.html",
            item=None,
            categories=categories,
            groups=groups,
            error=f"Item '{name}' já existe nesta categoria."
        ), 400

    # Create item
    item = ReservaItem(
        category_id=category_id,
        group_id=group_id,
        name=name,
        internal_slug=internal_slug,
        price=price,
        price_weekday=price_weekday,
        price_weekend=price_weekend,
        price_holiday=price_holiday,
        billing_type=billing_type,
        requires_deposit=requires_deposit,
        requires_resource=requires_resource,
        is_entry_ticket=is_entry_ticket,
        visible_to_bot=visible_to_bot,
        visible_to_web=visible_to_web,
        bot_description=bot_description,
        emoji_label=emoji_label,
    )
    db.session.add(item)
    db.session.commit()


    flash(f"Item '{name}' criado com sucesso.", "success")
    return redirect(url_for("reservas.admin_listar_itens"))


@bp.route("/admin/itens/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("reservas", "create")
def admin_editar_item(item_id):
    """GET: Exibe formulário preenchido. POST: Atualiza ReservaItem.

    Args:
        item_id: ID do ReservaItem a editar.

    Returns:
        GET: Template com formulário preenchido.
        POST: Redirect para admin_listar_itens() ou renderiza formulário com erro.
    """
    item = db.session.get(ReservaItem, item_id)
    if not item or item.deleted_at is not None:
        abort(404)

    # Query active categories and groups for dropdowns
    categories = db.session.query(ReservaCategory).filter(
        ReservaCategory.deleted_at.is_(None)
    ).order_by(ReservaCategory.name).all()

    groups = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.name)
        .all()
    )

    if request.method == "GET":
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
        )

    # POST: Update item
    name = request.form.get("name", "").strip()
    category_id = request.form.get("category_id", "").strip()
    group_id_raw = request.form.get("group_id", "").strip()
    price_input = request.form.get("price", "").strip()
    billing_type = request.form.get("billing_type", "").strip()
    requires_deposit = request.form.get("requires_deposit") == "on"
    requires_resource = request.form.get("requires_resource") == "on"
    is_entry_ticket = request.form.get("is_entry_ticket") == "on"
    visible_to_bot = request.form.get("visible_to_bot") == "on"
    visible_to_web = request.form.get("visible_to_web") == "on"
    bot_description = request.form.get("bot_description", "").strip() or None
    emoji_label = request.form.get("emoji_label", "").strip() or None
    internal_slug = request.form.get("internal_slug", "").strip() or None
    price_weekday_input = request.form.get("price_weekday", "").strip() or None
    price_weekend_input = request.form.get("price_weekend", "").strip() or None
    price_holiday_input = request.form.get("price_holiday", "").strip() or None

    # Validate required fields
    if not name:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Nome do item é obrigatório."
        ), 400

    if not category_id:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Categoria é obrigatória."
        ), 400

    if not price_input:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Preço é obrigatório."
        ), 400

    if not billing_type:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Tipo de cobrança é obrigatório."
        ), 400

    # Parse and validate category_id
    try:
        category_id = int(category_id)
    except ValueError:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Categoria inválida."
        ), 400

    # Verify category exists and is active
    category = db.session.get(ReservaCategory, category_id)
    if not category or category.deleted_at is not None:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Categoria inválida."
        ), 400

    # Parse and validate price
    try:
        price = float(price_input)
        if price < 0:
            raise ValueError("Preço não pode ser negativo.")
    except ValueError as e:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error=f"Preço inválido: {str(e)}"
        ), 400

    # Parse optional price tiers
    try:
        price_weekday = float(price_weekday_input) if price_weekday_input is not None else None
        price_weekend = float(price_weekend_input) if price_weekend_input is not None else None
        price_holiday = float(price_holiday_input) if price_holiday_input is not None else None
    except ValueError:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Tarifa de dia inválida — use somente números."
        ), 400

    # Validate billing_type
    valid_billing_types = [bt.value for bt in BillingType]
    if billing_type not in valid_billing_types:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Tipo de cobrança inválido."
        ), 400

    # Parse group_id (optional)
    group_id = None
    if group_id_raw:
        try:
            group_id = int(group_id_raw)
            grp = db.session.get(ReservaGroup, group_id)
            if not grp or grp.deleted_at is not None:
                group_id = None
        except (ValueError, TypeError):
            group_id = None
    if requires_resource and group_id is None:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error="Itens que exigem recurso físico precisam estar em um grupo."
        ), 400

    # Check unique constraint (category_id, name) excluding current item
    existing = db.session.query(ReservaItem).filter(
        ReservaItem.id != item_id,
        ReservaItem.category_id == category_id,
        db.func.lower(ReservaItem.name) == name.lower(),
        ReservaItem.deleted_at.is_(None)
    ).first()

    if existing:
        return render_template(
            "reservas/admin/item_form.html",
            item=item,
            categories=categories,
            groups=groups,
            error=f"Item '{name}' já existe nesta categoria."
        ), 400

    # Update item
    item.category_id = category_id
    item.group_id = group_id
    item.name = name
    item.internal_slug = internal_slug
    item.price = price
    item.price_weekday = price_weekday
    item.price_weekend = price_weekend
    item.price_holiday = price_holiday
    item.billing_type = billing_type
    item.requires_deposit = requires_deposit
    item.requires_resource = requires_resource
    item.is_entry_ticket = is_entry_ticket
    item.visible_to_bot = visible_to_bot
    item.visible_to_web = visible_to_web
    item.bot_description = bot_description
    item.emoji_label = emoji_label
    db.session.commit()


    flash(f"Item '{name}' atualizado com sucesso.", "success")
    return redirect(url_for("reservas.admin_listar_itens"))


@bp.route("/admin/itens/<int:item_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_deletar_item(item_id):
    """Soft-delete de um ReservaItem (marca com deleted_at).

    Args:
        item_id: ID do ReservaItem a deletar.

    Returns:
        Redirect para admin_listar_itens()
    """
    item = db.session.get(ReservaItem, item_id)

    if not item or item.deleted_at is not None:
        abort(404)

    from datetime import datetime, timezone
    item.deleted_at = datetime.now(timezone.utc)
    db.session.commit()


    flash(f"Item '{item.name}' deletado com sucesso.", "success")
    return redirect(url_for("reservas.admin_listar_itens"))



@bp.route("/admin/itens/todos", methods=["GET"])
@login_required
@require_permission("reservas", "create")
def admin_todos_itens():
    """Lista todos os ReservaItem, incluindo os soft-deletados.

    Permite visualizar, restaurar ou excluir definitivamente itens que não
    aparecem na listagem principal por estarem marcados com deleted_at.

    Returns:
        Renderização do template items_all.html com todos os itens.
    """
    all_items = (
        db.session.query(ReservaItem)
        .order_by(ReservaItem.deleted_at.is_(None).desc(), ReservaItem.name)
        .all()
    )
    return render_template(
        "reservas/admin/items_all.html",
        all_items=all_items,
    )


@bp.route("/admin/itens/<int:item_id>/restaurar", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_restaurar_item(item_id):
    """Restaura um ReservaItem soft-deletado (limpa deleted_at).

    Args:
        item_id: ID do item a ser restaurado.

    Returns:
        Redirect para admin_todos_itens().
    """
    item = db.session.get(ReservaItem, item_id)

    if not item or item.deleted_at is None:
        abort(404)

    item.deleted_at = None
    db.session.commit()

    flash(f"Item '{item.name}' restaurado com sucesso.", "success")
    return redirect(url_for("reservas.admin_todos_itens"))


@bp.route("/admin/itens/<int:item_id>/excluir-definitivo", methods=["POST"])
@login_required
@require_permission("reservas", "create")
def admin_excluir_definitivo_item(item_id):
    """Exclui permanentemente um ReservaItem já soft-deletado.

    Só é permitido para itens já marcados com deleted_at. Libera o nome
    para reutilização dentro da categoria (desfaz a constraint unique category_id + name).

    Args:
        item_id: ID do item a ser excluído permanentemente.

    Returns:
        Redirect para admin_todos_itens().
    """
    item = db.session.get(ReservaItem, item_id)

    if not item or item.deleted_at is None:
        abort(404)

    nome = item.name
    db.session.delete(item)
    db.session.commit()

    flash(f"Item '{nome}' excluído permanentemente.", "success")
    return redirect(url_for("reservas.admin_todos_itens"))


# ---------------------------------------------------------------------------
# Mapa de Ativos — Visual Slot-Based Occupancy Radar
# ---------------------------------------------------------------------------


@bp.route("/radar/ativos", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def mapa_ativos():
    """Exibe o mapa visual de ocupação de recursos físicos para uma data.

    Gera um grid de slots para cada recurso com estoque finito, colorindo
    cada slot conforme o estado: livre (verde), ocupado sem caução (vermelho),
    ocupado com caução (roxo).

    Query params:
        date (str): Data no formato YYYY-MM-DD. Default: hoje.

    Returns:
        Renderização de reservas/mapa_ativos.html com dados por recurso.
    """
    from datetime import date as _date, timedelta
    from collections import defaultdict

    # ── Resolve selected date ──────────────────────────────────────────────
    date_str = request.args.get("date", "").strip()
    try:
        selected_date = _date.fromisoformat(date_str) if date_str else _date.today()
    except ValueError:
        selected_date = _date.today()

    today = _date.today()
    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    # ── Query all finite resources (stock_quantity not null and > 0) ───────
    resources = (
        db.session.query(ReservaResource)
        .filter(
            ReservaResource.deleted_at.is_(None),
            ReservaResource.stock_quantity.isnot(None),
            ReservaResource.stock_quantity > 0,
        )
        .order_by(ReservaResource.source_id, ReservaResource.name)
        .all()
    )

    # ── Carrega configuração dinâmica de status ────────────────────────────
    status_configs = db.session.query(ReservaStatusConfig).all()
    status_config_map = {s.internal_name: s for s in status_configs}

    # Statuses que bloqueiam recurso (para filtrar links visíveis no mapa).
    # Usa strings diretamente para incluir status customizados criados via ReservaStatusConfig.
    blocking_statuses = [
        name for name, cfg in status_config_map.items()
        if cfg.blocks_resource
    ] or ["PENDING", "PAID"]  # fallback de seguranca

    # ── Single query: all active links for all resources on selected_date ──
    # Usa func.date() para ignorar o componente de hora em check_in/check_out
    # (campos são DateTime mas a comparação deve ser apenas por data).
    # check_out_date NULL é tratado como reserva de dia único (sem saída definida).
    from sqlalchemy import func, or_

    if resources:
        resource_ids = [r.id for r in resources]
        raw_links = (
            db.session.query(ReservaItemLink, Reserva, ReservaItem)
            .join(Reserva, ReservaItemLink.reserva_id == Reserva.id)
            .outerjoin(ReservaItem, ReservaItemLink.item_id == ReservaItem.id)
            .filter(
                ReservaItemLink.resource_id.in_(resource_ids),
                ReservaItemLink.deleted_at.is_(None),
                Reserva.deleted_at.is_(None),
                Reserva.status.in_(blocking_statuses),
                func.date(Reserva.check_in_date) <= selected_date,
                func.date(
                    func.coalesce(Reserva.check_out_date, Reserva.check_in_date)
                ) >= selected_date,
            )
            .all()
        )
    else:
        raw_links = []

    # ── Cauções ativas por (reserva_id, resource_id) ──────────────────────
    # Inclui qualquer caução não-devolvida e não-deletada (pendente ou paga).
    # Usado para colorir slots individuais no mapa com a cor DEPOSIT.
    reserva_resource_deposit: set[tuple[int, int]] = set()
    resources_with_active_deposit: set[int] = set()
    if resources and raw_links:
        reserva_ids_no_mapa = list({r.id for _, r, _ in raw_links})
        deposit_pairs = (
            db.session.query(SecurityDeposit.reserva_id, PhysicalAsset.resource_id)
            .join(PhysicalAsset, SecurityDeposit.linked_asset_id == PhysicalAsset.id)
            .filter(
                SecurityDeposit.reserva_id.in_(reserva_ids_no_mapa),
                SecurityDeposit.is_refunded.is_(False),
                SecurityDeposit.deleted_at.is_(None),
                PhysicalAsset.resource_id.isnot(None),
                PhysicalAsset.deleted_at.is_(None),
            )
            .distinct()
            .all()
        )
        for reserva_id, resource_id in deposit_pairs:
            reserva_resource_deposit.add((reserva_id, resource_id))
            resources_with_active_deposit.add(resource_id)

    # ── Group occupation entries by resource_id ────────────────────────────
    # Each booking with quantity=N occupies N sequential slots.
    occupations_by_resource = defaultdict(list)
    for link, reserva, item in raw_links:
        qty = max(link.quantity or 1, 1)
        has_dep = (reserva.id, link.resource_id) in reserva_resource_deposit
        for _ in range(qty):
            occupations_by_resource[link.resource_id].append(
                {
                    "customer_name": reserva.customer_name or "Sem nome",
                    "has_deposit": has_dep,
                    "reserva_id": reserva.id,
                    "status": reserva.status.value,
                }
            )

    # ── Map resources to groups ───────────────────────────────────────────
    resources_by_group = defaultdict(list)
    ungrouped_resources_data = []
    total_slots_all = 0
    total_occupied_all = 0

    for resource in resources:
        occs = occupations_by_resource.get(resource.id, [])
        total = resource.stock_quantity
        occupied_count = min(len(occs), total)

        slots = []
        for i in range(total):
            if i < len(occs):
                occ = occs[i]
                slots.append(
                    {
                        "slot_number": i + 1,
                        "is_occupied": True,
                        "customer_name": occ["customer_name"],
                        "has_deposit": occ["has_deposit"],
                        "reserva_id": occ["reserva_id"],
                    }
                )
            else:
                slots.append(
                    {
                        "slot_number": i + 1,
                        "is_occupied": False,
                        "customer_name": None,
                        "has_deposit": False,
                        "reserva_id": None,
                    }
                )

        ratio = occupied_count / total if total else 0
        if ratio == 0:
            occ_level = "empty"
        elif ratio < 0.5:
            occ_level = "low"
        elif ratio < 0.85:
            occ_level = "medium"
        else:
            occ_level = "high"

        # Verifica se há aviso de bloqueio (source ou items) ativo na data selecionada
        _src_id = resource.source_id
        _has_blocking_notice = False
        if _src_id is not None:
            _has_blocking_notice = (
                ResourceNoticeService.tem_bloqueio_source(_src_id, selected_date)
                or ResourceNoticeService.tem_bloqueio_items(_src_id, selected_date)
            )

        res_data = {
            "resource": resource,
            "source_name": resource.source.name if resource.source else "—",
            "slots": slots,
            "total": total,
            "occupied": occupied_count,
            "available": total - occupied_count,
            "occ_level": occ_level,
            # Flag para frontend colorir o recurso quando há caução ativa vinculada
            "has_active_deposit": resource.id in resources_with_active_deposit,
            # Flag para frontend exibir badge de aviso de bloqueio ativo
            "has_blocking_notice": _has_blocking_notice,
        }

        if resource.group_id:
            resources_by_group[resource.group_id].append(res_data)
        else:
            ungrouped_resources_data.append(res_data)

        total_slots_all += total
        total_occupied_all += occupied_count

    # ── Build hierarchical group tree ─────────────────────────────────────
    all_groups = db.session.query(ReservaGroup).filter(
        ReservaGroup.deleted_at.is_(None)
    ).order_by(ReservaGroup.sort_order, ReservaGroup.name).all()

    group_map = {
        g.id: {
            "group": g,
            "children": [],
            "resources_list": resources_by_group.get(g.id, [])
        } for g in all_groups
    }

    root_groups = []
    for g in all_groups:
        if g.parent_id and g.parent_id in group_map:
            group_map[g.parent_id]["children"].append(group_map[g.id])
        else:
            root_groups.append(group_map[g.id])

    # Filter out empty branches
    def has_content(node):
        return bool(node["resources_list"]) or any(has_content(child) for child in node["children"])

    root_groups = [g for g in root_groups if has_content(g)]

    return render_template(
        "reservas/mapa_ativos.html",
        root_groups=root_groups,
        ungrouped_resources=ungrouped_resources_data,
        selected_date=selected_date,
        prev_date=prev_date,
        next_date=next_date,
        today=today,
        total_slots=total_slots_all,
        total_occupied=total_occupied_all,
        total_available=total_slots_all - total_occupied_all,
        status_config_map=status_config_map,
    )


# ---------------------------------------------------------------------------
# Timeline / Gantt API
# ---------------------------------------------------------------------------


@bp.route("/api/timeline", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def api_timeline():
    """Retorna o timeline mensal de ocupação por recurso físico (visão Gantt).

    Usado pelo frontend para renderizar o calendário Gantt de camping e
    estadias multi-dia. Cada recurso retorna suas reservas ativas para o mês
    com o papel de cada dia: CHECK_IN, STAY ou CHECK_OUT.

    Query params:
        source_id (int): ID da fonte/venue. Obrigatório.
        year  (int): Ano. Default: ano atual.
        month (int): Mês (1–12). Default: mês atual.

    Returns:
        200 JSON: {
            source_id, source_name, year, month, num_days,
            resources: [{
                resource_id, resource_name, group_id, group_name,
                group_path, stock_quantity,
                reservations: [{
                    reserva_id, reserva_group_id, customer_name,
                    status, check_in_date, check_out_date, quantity,
                    subtotal, total,
                    days: { day_number: "CHECK_IN"|"STAY"|"CHECK_OUT" }
                }]
            }]
        }
        400 JSON: {"erro": "..."}
    """
    from datetime import date as _date

    raw_source = request.args.get("source_id", "").strip()
    if not raw_source or not raw_source.isdigit():
        return jsonify({"erro": "source_id é obrigatório e deve ser inteiro."}), 400

    source_id = int(raw_source)

    today = _date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except (ValueError, TypeError):
        return jsonify({"erro": "year e month devem ser inteiros válidos."}), 400

    if not (1 <= month <= 12):
        return jsonify({"erro": "month deve estar entre 1 e 12."}), 400

    try:
        data = ReservaService.timeline_mensal(
            source_id=source_id,
            year=year,
            month=month,
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400

    return jsonify(data), 200


# ---------------------------------------------------------------------------
# Internal Ingestion API Routes (Require Internal API Key or Admin Auth)
# ---------------------------------------------------------------------------


@bp.route("/api/internal/ingest", methods=["POST"])
@require_permission("reservas", "create")
def api_internal_ingest():
    """Internal API para ingestão de reservas com validação server-side de preço.

    Endpoint interno para sistemas que integram com SISTUR (POS, WordPress, etc.).
    Implementa double-check de preço: calcula o total servidor-side e rejeita
    se houver divergência > 0.01 com o valor enviado pelo frontend.

    Request body (JSON):
        source_name: str — nome da fonte (cria se não existir)
        category_name: str — nome da categoria
        customer_name: str — nome do cliente
        customer_document: str — CPF ou CNPJ (opcional)
        origin: str — "WEB", "BALCAO" ou "BOT"
        check_in_date: str — data de check-in (YYYY-MM-DD)
        check_out_date: str — data de check-out (YYYY-MM-DD, opcional)
        items: list — [{"item_id": int, "quantity": int, "price_override": str}]
        metadata_info: dict — metadados customizados (opcional)

    Returns:
        201 JSON com reserva criada, pricing snapshots e expires_at
        400 Se validação falhar ou divergência de preço detectada
        422 Se request payload estiver malformado

    Error examples:
        - Divergência de preço: "Divergência de preço: O valor calculado pelo servidor (100.00) difere do valor enviado (105.00). Diferença: 5.00."
        - Item não encontrado: "Item com ID 999 não encontrado."
        - Estoque insuficiente: "Estoque insuficiente para o item 'Inteira'. Disponível: 5, solicitado: 10."
    """
    data = request.get_json(silent=True) or {}

    # Validate required fields
    required = ["source_name", "category_name", "customer_name", "origin",
                "check_in_date", "items"]
    for field in required:
        if not data.get(field):
            return jsonify({"erro": f"Campo obrigatório ausente: '{field}'."}), 400

    # Parse dates
    try:
        check_in = date.fromisoformat(data["check_in_date"])
        check_out = (
            date.fromisoformat(data["check_out_date"])
            if data.get("check_out_date")
            else None
        )
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    # Create reservation with server-side pricing validation
    try:
        reserva = ReservaService.criar(
            source_name=data["source_name"],
            category_name=data["category_name"],
            customer_name=data["customer_name"],
            customer_document=data.get("customer_document"),
            origin=data["origin"],
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=data["items"],
            metadata_info=data.get("metadata_info"),  # Sweep arbitrary fields
            ator_id=_get_ator_id(),
        )
    except ValueError as exc:
        # Return 400 with specific error message (includes price divergence)
        return jsonify({"erro": str(exc)}), 400
    except Exception as exc:
        # Log unexpected errors
        import sys
        print(f"Error in api_internal_ingest: {exc}", file=sys.stderr)
        return jsonify({
            "erro": "Erro ao processar ingesta. Tente novamente mais tarde."
        }), 500

    return jsonify({
        "reserva": _serializar_reserva(reserva, include_items=True),
        "pricing": {
            "subtotal": float(reserva.subtotal_amount),
            "discount": float(reserva.discount_amount),
            "service_fee": float(reserva.service_fee_amount),
            "total": float(reserva.total_amount),
        }
    }), 201


# ========================================================================
# ANÁLISE DE DADOS
# ========================================================================


@bp.route("/analytics/", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def analytics_dashboard():
    """Renderiza o dashboard de análise de dados de reservas.

    Exibe KPIs com indicadores de crescimento, gráficos de origem/categoria,
    top itens, antecedência média, taxa de conversão inteligente, vendas do
    PDV por método de pagamento e movimentações de caixa (sangria/suprimento)
    por tipo e por centro de custo. Os blocos de PDV/caixa ignoram o filtro
    de status — ver docstring da seção "PDV" em AnalyticsService.

    Query params:
        data_inicio (str): Data inicial no formato YYYY-MM-DD.
                           Default: hoje − 30 dias.
        data_fim    (str): Data final no formato YYYY-MM-DD.
                           Default: hoje.
        source_id   (int): Filtro opcional por venue/source.
        status      (str): Pode ser repetido — lista de internal_names a incluir.
                           Default: todos exceto CANCELED e ARCHIVED_VERSION.

    Returns:
        200 HTML: Página analytics.html com todos os dados de análise.
    """
    from app.models.reservas import ReservaStatusConfig as _RSC

    hoje = date.today()
    default_inicio = hoje - timedelta(days=30)

    try:
        data_inicio = date.fromisoformat(request.args.get("data_inicio", ""))
    except ValueError:
        data_inicio = default_inicio

    try:
        data_fim = date.fromisoformat(request.args.get("data_fim", ""))
    except ValueError:
        data_fim = hoje

    raw_source = request.args.get("source_id", "")
    source_id: int | None = None
    if raw_source and raw_source.isdigit():
        source_id = int(raw_source)

    # Filtro de status via chips — lista vazia = sem filtro (usa padrão do service)
    selected_statuses: list[str] = request.args.getlist("status")
    statuses: list[str] | None = selected_statuses if selected_statuses else None

    # Carrega todos os status configurados para renderizar os chips
    # (exclui ARCHIVED_VERSION — é um artefato interno de versionamento)
    all_status_configs = (
        db.session.query(_RSC)
        .filter(_RSC.internal_name != "ARCHIVED_VERSION")
        .order_by(_RSC.id)
        .all()
    )

    resumo       = AnalyticsService.resumo_periodo(data_inicio, data_fim, statuses, source_id)
    por_origem   = AnalyticsService.por_origem(data_inicio, data_fim, statuses, source_id)
    por_categoria = AnalyticsService.por_categoria(data_inicio, data_fim, source_id, statuses)
    top_itens    = AnalyticsService.itens_mais_vendidos_por_origem(
        data_inicio, data_fim, statuses=statuses, source_id=source_id
    )
    antecedencia_stats = AnalyticsService.antecedencia_stats(data_inicio, data_fim, statuses, source_id)
    conversao    = AnalyticsService.taxa_conversao_inteligente(data_inicio, data_fim, source_id)
    serie_temporal = AnalyticsService.serie_temporal(data_inicio, data_fim, statuses, source_id)
    conversao_origem = AnalyticsService.conversao_por_origem(data_inicio, data_fim, source_id)

    pdv_resumo = AnalyticsService.pdv_resumo_periodo(data_inicio, data_fim, source_id)
    pdv_por_metodo = AnalyticsService.pdv_por_metodo_pagamento(data_inicio, data_fim, source_id)
    movimentacoes_resumo = AnalyticsService.movimentacoes_caixa_resumo(data_inicio, data_fim, source_id)
    movimentacoes_por_tipo = AnalyticsService.movimentacoes_caixa_por_tipo(data_inicio, data_fim, source_id)
    movimentacoes_por_centro_custo = AnalyticsService.movimentacoes_caixa_por_centro_custo(
        data_inicio, data_fim, source_id
    )

    sources = (
        db.session.query(ReservaSource)
        .filter(ReservaSource.deleted_at.is_(None))
        .order_by(ReservaSource.name)
        .all()
    )

    return render_template(
        "reservas/analytics.html",
        resumo=resumo,
        por_origem=por_origem,
        por_categoria=por_categoria,
        top_itens=top_itens,
        antecedencia=antecedencia_stats["media"],
        antecedencia_stats=antecedencia_stats,
        conversao=conversao,
        serie_temporal=serie_temporal,
        conversao_origem=conversao_origem,
        pdv_resumo=pdv_resumo,
        pdv_por_metodo=pdv_por_metodo,
        movimentacoes_resumo=movimentacoes_resumo,
        movimentacoes_por_tipo=movimentacoes_por_tipo,
        movimentacoes_por_centro_custo=movimentacoes_por_centro_custo,
        sources=sources,
        all_status_configs=all_status_configs,
        selected_statuses=selected_statuses,
        data_inicio=data_inicio.isoformat(),
        data_fim=data_fim.isoformat(),
        source_id=source_id,
    )


@bp.route("/analytics/export/fluxo-caixa.csv", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def analytics_export_fluxo_caixa():
    """Exporta o fluxo de caixa diário do período em CSV.

    Agrega pagamentos aprovados (entradas) e reembolsos efetivados (saídas)
    pelo dia em que o dinheiro movimentou no Mercado Pago, permitindo
    comparação em planilha (Excel, Google Sheets).

    Query params:
        data_inicio (str): Data inicial YYYY-MM-DD. Default: hoje − 30 dias.
        data_fim    (str): Data final YYYY-MM-DD. Default: hoje.
        source_id   (int): Filtro opcional por venue.

    Returns:
        200 text/csv: Arquivo com cabeçalho e uma linha por dia com movimentação.
        Encoding UTF-8 com BOM para abrir corretamente no Excel em pt-BR.
    """
    import csv
    import io

    hoje = date.today()
    default_inicio = hoje - timedelta(days=30)

    try:
        data_inicio = date.fromisoformat(request.args.get("data_inicio", ""))
    except ValueError:
        data_inicio = default_inicio

    try:
        data_fim = date.fromisoformat(request.args.get("data_fim", ""))
    except ValueError:
        data_fim = hoje

    raw_source = request.args.get("source_id", "")
    source_id: int | None = int(raw_source) if raw_source and raw_source.isdigit() else None

    linhas = AnalyticsService.fluxo_caixa(data_inicio, data_fim, source_id)

    # Ledger detalhado — uma linha por transação para conciliação bancária.
    # Nome + data/hora completa + valor + IDs MP permitem casar cada movimento
    # com a entrada correspondente no extrato do banco/MP.
    # BOM UTF-8 (﻿) no início garante que Excel abra em UTF-8 em pt-BR;
    # delimitador ";" é o padrão esperado pelo Excel brasileiro (vírgula é decimal).
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Data/Hora",
        "Tipo",
        "Valor (R$)",
        "Cliente",
        "CPF/CNPJ",
        "Método",
        "ID Pagamento MP",
        "ID Extorno MP",
        "Reserva",
        "Venue",
        "Status Detail",
    ])
    # Saldo acumulado ao final da exportação — útil para conferência rápida
    saldo_entradas = 0.0
    saldo_saidas = 0.0
    for linha in linhas:
        valor = linha["valor"]
        if linha["tipo"] == "Entrada":
            saldo_entradas += valor
            valor_str = f"{valor:.2f}".replace(".", ",")
        else:
            saldo_saidas += valor
            valor_str = f"-{valor:.2f}".replace(".", ",")
        writer.writerow([
            linha["data_hora"],
            linha["tipo"],
            valor_str,
            linha["cliente"],
            linha["documento"],
            linha["metodo"],
            linha["mp_payment_id"],
            linha["mp_refund_id"],
            f"#{linha['reserva_id']}" if linha["reserva_id"] else "",
            linha["venue"],
            linha["status_detail"],
        ])

    # Linha de totalização — facilita conferência do saldo sem precisar de fórmula
    if linhas:
        saldo_liquido = saldo_entradas - saldo_saidas
        writer.writerow([])  # linha em branco como separador visual
        writer.writerow([
            "TOTAL", "", f"{saldo_liquido:.2f}".replace(".", ","),
            f"Entradas: {saldo_entradas:.2f}".replace(".", ","),
            f"Saídas: {saldo_saidas:.2f}".replace(".", ","),
            "", "", "", "", "", "",
        ])

    filename = f"fluxo-caixa_{data_inicio.isoformat()}_{data_fim.isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/analytics/api/dados", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def analytics_api_dados():
    """Retorna os dados analíticos do período em formato JSON.

    Mesmos parâmetros e filtros do dashboard HTML, mas retorna JSON
    para consumo via AJAX ou integração com outros clientes.

    Query params:
        data_inicio (str): Data inicial no formato YYYY-MM-DD.
                           Default: hoje − 30 dias.
        data_fim    (str): Data final no formato YYYY-MM-DD.
                           Default: hoje.
        source_id   (int): Filtro opcional por venue/source.
        status      (str): Pode ser repetido — lista de internal_names a incluir.

    Returns:
        200 JSON: {
            "resumo": {total_reservas, receita_total, ticket_medio, growth_*},
            "por_origem": [...],
            "por_categoria": [...],
            "top_itens": [...],
            "antecedencia": float | null,
            "conversao": {total_intents, converted_intents, taxa},
            "pdv_resumo": {total_vendas, receita_pdv, ticket_medio_pdv, growth_*},
            "pdv_por_metodo": [...],
            "movimentacoes_resumo": {total_sangria, total_suprimento, saldo_liquido, count_movimentacoes},
            "movimentacoes_por_tipo": [...],
            "movimentacoes_por_centro_custo": [...]
        }
    """
    hoje = date.today()
    default_inicio = hoje - timedelta(days=30)

    try:
        data_inicio = date.fromisoformat(request.args.get("data_inicio", ""))
    except ValueError:
        data_inicio = default_inicio

    try:
        data_fim = date.fromisoformat(request.args.get("data_fim", ""))
    except ValueError:
        data_fim = hoje

    raw_source = request.args.get("source_id", "")
    source_id: int | None = None
    if raw_source and raw_source.isdigit():
        source_id = int(raw_source)

    selected_statuses = request.args.getlist("status")
    statuses: list[str] | None = selected_statuses if selected_statuses else None

    return jsonify({
        "resumo":       AnalyticsService.resumo_periodo(data_inicio, data_fim, statuses, source_id),
        "por_origem":   AnalyticsService.por_origem(data_inicio, data_fim, statuses, source_id),
        "por_categoria": AnalyticsService.por_categoria(data_inicio, data_fim, source_id, statuses),
        "top_itens":    AnalyticsService.itens_mais_vendidos(
            data_inicio, data_fim, statuses=statuses, source_id=source_id
        ),
        "antecedencia": AnalyticsService.antecedencia_media(data_inicio, data_fim, statuses, source_id),
        "antecedencia_stats": AnalyticsService.antecedencia_stats(data_inicio, data_fim, statuses, source_id),
        "conversao":    AnalyticsService.taxa_conversao_inteligente(data_inicio, data_fim, source_id),
        "serie_temporal": AnalyticsService.serie_temporal(data_inicio, data_fim, statuses, source_id),
        "conversao_origem": AnalyticsService.conversao_por_origem(data_inicio, data_fim, source_id),
        "pdv_resumo": AnalyticsService.pdv_resumo_periodo(data_inicio, data_fim, source_id),
        "pdv_por_metodo": AnalyticsService.pdv_por_metodo_pagamento(data_inicio, data_fim, source_id),
        "movimentacoes_resumo": AnalyticsService.movimentacoes_caixa_resumo(data_inicio, data_fim, source_id),
        "movimentacoes_por_tipo": AnalyticsService.movimentacoes_caixa_por_tipo(data_inicio, data_fim, source_id),
        "movimentacoes_por_centro_custo": AnalyticsService.movimentacoes_caixa_por_centro_custo(
            data_inicio, data_fim, source_id
        ),
    })


# ========================================================================
# ALIAS ROUTES — Compatibilidade com WordPress
# ========================================================================
# O WordPress espera estes endpoints sob /reservas/api/public/
# Importamos a função de catálogo da api_public blueprint e re-exponemos aqui.
# A função simular_preco já existe neste próprio arquivo em /api/public/simular,
# então criamos um alias que aponta para o mesmo endpoint.

from app.blueprints.api_public.routes import (
    catalogo as _api_public_catalogo,
)


@bp.route("/api/public/catalogo", methods=["GET"])
def api_public_catalogo_alias():
    """Alias: GET /reservas/api/public/catalogo → /api/public/reservas/catalogo

    Compatibilidade com WordPress client que espera este endpoint aqui.
    Delega para a implementação de api_public blueprint.
    """
    return _api_public_catalogo()


# ---------------------------------------------------------------------------
# Gerenciamento de Avisos de Recursos (Resource Notices) — CRUD admin
# ---------------------------------------------------------------------------

@bp.route("/admin/config/avisos", methods=["GET"])
@login_required
@require_permission("reservas", "configurar")
def admin_avisos_lista():
    """Exibe a lista de avisos de recursos e o formulário de criação.

    Returns:
        Renderização de reservas/admin/resource_notices.html com notices,
        sources e items (agrupados por source_id para o multi-select do formulário).
    """
    from app.models.reservas import ResourceNotice as RN
    notices = (
        db.session.query(RN)
        .filter(RN.deleted_at.is_(None))
        .order_by(RN.start_date.desc())
        .all()
    )
    sources = (
        db.session.query(ReservaSource)
        .filter(ReservaSource.deleted_at.is_(None))
        .order_by(ReservaSource.name)
        .all()
    )
    items = (
        db.session.query(ReservaItem)
        .join(ReservaCategory, ReservaItem.category_id == ReservaCategory.id)
        .filter(
            ReservaItem.deleted_at.is_(None),
            ReservaCategory.deleted_at.is_(None),
        )
        .order_by(ReservaCategory.source_id, ReservaCategory.name, ReservaItem.name)
        .all()
    )
    return render_template(
        "reservas/admin/resource_notices.html",
        notices=notices,
        sources=sources,
        items=items,
    )


@bp.route("/admin/config/avisos/criar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_avisos_criar():
    """Cria um novo aviso de recurso.

    Form fields:
        event_name (str): Nome do evento ou motivo do aviso.
        start_date (str): Data de início no formato YYYY-MM-DD.
        end_date (str): Data de fim no formato YYYY-MM-DD.
        source_id (int|''): ID do source afetado. Vazio = aviso global.
        blocks_source (checkbox): Se presente, bloqueia reservas do source.
        blocks_items (checkbox): Se presente, bloqueia itens no picker.
        blocked_item_ids (list[int]): IDs de itens a bloquear. Vazio = bloqueia todos.
        recurring_weekday (int|''): 0=segunda ... 6=domingo. Vazio = intervalo
            contínuo (sem recorrência).

    Returns:
        Redirect para admin_avisos_lista com flash de resultado.
    """
    event_name = request.form.get("event_name", "").strip()
    start_str = request.form.get("start_date", "").strip()
    end_str = request.form.get("end_date", "").strip()
    source_id_str = request.form.get("source_id", "").strip()
    blocks_source = request.form.get("blocks_source") == "1"
    blocks_items = request.form.get("blocks_items") == "1"
    recurring_weekday_str = request.form.get("recurring_weekday", "").strip()

    raw_ids = request.form.getlist("blocked_item_ids")
    blocked_item_ids: list[int] | None = (
        [int(x) for x in raw_ids if x.isdigit()] or None
    ) if blocks_items else None

    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        flash("Datas inválidas. Use o formato AAAA-MM-DD.", "erro")
        return redirect(url_for("reservas.admin_avisos_lista"))

    source_id = int(source_id_str) if source_id_str else None
    recurring_weekday = int(recurring_weekday_str) if recurring_weekday_str.isdigit() else None

    try:
        ResourceNoticeService.criar(
            event_name=event_name,
            start_date=start_date,
            end_date=end_date,
            blocks_source=blocks_source,
            blocks_items=blocks_items,
            blocked_item_ids=blocked_item_ids,
            source_id=source_id,
            recurring_weekday=recurring_weekday,
            ator_id=_get_ator_id(),
        )
        flash(f"Aviso '{event_name}' criado com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("reservas.admin_avisos_lista"))


@bp.route("/admin/config/avisos/<int:notice_id>/editar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_avisos_editar(notice_id: int):
    """Atualiza um aviso de recurso existente.

    Args:
        notice_id: ID do aviso a atualizar.

    Form fields:
        event_name, start_date, end_date, source_id, blocks_source,
        blocks_items, blocked_item_ids (list), recurring_weekday.

    Returns:
        Redirect para admin_avisos_lista com flash de resultado.
    """
    event_name = request.form.get("event_name", "").strip()
    start_str = request.form.get("start_date", "").strip()
    end_str = request.form.get("end_date", "").strip()
    source_id_str = request.form.get("source_id", "").strip()
    blocks_source = request.form.get("blocks_source") == "1"
    blocks_items = request.form.get("blocks_items") == "1"
    recurring_weekday_str = request.form.get("recurring_weekday", "").strip()

    raw_ids = request.form.getlist("blocked_item_ids")
    blocked_item_ids: list[int] | None = (
        [int(x) for x in raw_ids if x.isdigit()] or None
    ) if blocks_items else None
    clear_blocked_items = blocks_items and not blocked_item_ids

    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        flash("Datas inválidas.", "erro")
        return redirect(url_for("reservas.admin_avisos_lista"))

    source_id = int(source_id_str) if source_id_str else None
    clear_source = not bool(source_id_str)
    recurring_weekday = int(recurring_weekday_str) if recurring_weekday_str.isdigit() else None
    clear_recurring_weekday = not recurring_weekday_str

    try:
        ResourceNoticeService.atualizar(
            notice_id=notice_id,
            event_name=event_name,
            start_date=start_date,
            end_date=end_date,
            blocks_source=blocks_source,
            blocks_items=blocks_items,
            blocked_item_ids=blocked_item_ids,
            clear_blocked_items=clear_blocked_items,
            source_id=source_id,
            clear_source=clear_source,
            recurring_weekday=recurring_weekday,
            clear_recurring_weekday=clear_recurring_weekday,
            ator_id=_get_ator_id(),
        )
        flash("Aviso atualizado com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("reservas.admin_avisos_lista"))


@bp.route("/admin/config/avisos/<int:notice_id>/deletar", methods=["POST"])
@login_required
@require_permission("reservas", "configurar")
def admin_avisos_deletar(notice_id: int):
    """Remove um aviso de recurso via soft-delete.

    Args:
        notice_id: ID do aviso a remover.

    Returns:
        Redirect para admin_avisos_lista com flash de resultado.
    """
    try:
        ResourceNoticeService.deletar(notice_id=notice_id, ator_id=_get_ator_id())
        flash("Aviso removido.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("reservas.admin_avisos_lista"))


@bp.route("/api/public/simular", methods=["POST"])
def api_public_simular_alias():
    """Alias: POST /reservas/api/public/simular → /api/public/reservas/simular

    Compatibilidade com WordPress client que espera este endpoint aqui.
    Delega para a função simular_preco já definida neste arquivo.
    """
    return simular_preco()


@bp.route("/api/public/desconto-antecipado", methods=["GET"])
def api_public_desconto_antecipado_alias():
    """Alias: GET /reservas/api/public/desconto-antecipado

    Compatibilidade com WordPress client que pode chamar com prefixo /reservas.
    Delega para api_public_desconto_antecipado já definido neste arquivo.
    """
    return api_public_desconto_antecipado()


# ---------------------------------------------------------------------------
# Consumo de Itens — visão agregada dentro do módulo Reservas
# ---------------------------------------------------------------------------

# Statuses excluídos do consumo: canceladas e versões arquivadas nunca representam
# consumo real. Todos os outros (incluindo status customizados de operadores) são
# considerados — por isso usamos notin_ em vez de in_.
_STATUSES_EXCLUIDOS_VIEW = (
    ReservaStatus.CANCELED,
    ReservaStatus.ARCHIVED_VERSION,
)


@bp.route("/consumo-itens", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def consumo_itens():
    """Exibe o relatório agregado de consumo de itens dentro do módulo Reservas.

    Lista os itens consumidos nas reservas ativas/concluídas no período
    selecionado, somando as quantidades. Ao clicar num item o front-end
    faz uma chamada assíncrona para ``consumo_itens_detalhe`` e exibe
    as reservas individuais que incluíram aquele item.

    Query params:
        start_date (str, opcional): Data inicial YYYY-MM-DD.
                                     Default: primeiro dia do mês atual.
        end_date   (str, opcional): Data final YYYY-MM-DD.
                                     Default: igual a start_date.

    Returns:
        Renderização de reservas/itens_consumo.html.
    """
    from sqlalchemy import func as _func

    hoje = date.today()
    start_raw = request.args.get("start_date", "").strip()
    end_raw   = request.args.get("end_date", "").strip()

    try:
        start_date = date.fromisoformat(start_raw) if start_raw else hoje.replace(day=1)
    except ValueError:
        start_date = hoje.replace(day=1)

    try:
        end_date = date.fromisoformat(end_raw) if end_raw else start_date
    except ValueError:
        end_date = start_date

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    dt_inicio = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    dt_fim    = datetime(end_date.year,   end_date.month,   end_date.day,   23, 59, 59, 999999)

    rows = (
        db.session.query(
            ReservaItem.id.label("item_id"),
            ReservaItem.name.label("item_name"),
            _func.sum(ReservaItemLink.quantity).label("total_qty"),
        )
        .join(ReservaItemLink, ReservaItemLink.item_id == ReservaItem.id)
        .join(Reserva, Reserva.id == ReservaItemLink.reserva_id)
        .filter(
            ReservaItemLink.deleted_at.is_(None),
            Reserva.deleted_at.is_(None),
            ReservaItem.deleted_at.is_(None),
            Reserva.status.notin_(_STATUSES_EXCLUIDOS_VIEW),
            Reserva.status.in_([
                cfg.internal_name
                for cfg in db.session.query(ReservaStatusConfig).all()
                if cfg.show_on_dashboard
            ] or ["PAID", "EM_USO"]),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        )
        .group_by(ReservaItem.id, ReservaItem.name)
        .order_by(_func.sum(ReservaItemLink.quantity).desc(), ReservaItem.name)
        .all()
    )

    itens = [
        {"item_id": r.item_id, "item_name": r.item_name, "total_qty": int(r.total_qty)}
        for r in rows
    ]

    return render_template(
        "reservas/itens_consumo.html",
        itens=itens,
        start_date=start_date,
        end_date=end_date,
    )


@bp.route("/consumo-itens/<int:item_id>/reservas-detalhe", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def consumo_itens_detalhe(item_id: int):
    """Retorna JSON com as reservas individuais que consumiram um item no período.

    Endpoint de drill-down chamado assincronamente pelo front-end ao clicar
    numa linha do relatório de consumo de itens.

    Query params:
        start_date (str, obrigatório): Data inicial YYYY-MM-DD.
        end_date   (str, obrigatório): Data final YYYY-MM-DD.

    Returns:
        JSON: {"item_name": str, "reservations": [{reserva_id, group_id,
               customer_name, check_in_date, status, quantity, detail_url}]}

        200 — Lista (pode ser vazia).
        400 — Parâmetros de data ausentes ou inválidos.
        403 — Sem permissão (tratado pelo decorator).
        404 — Item não encontrado.
    """
    from flask import jsonify as _jsonify

    item = db.session.query(ReservaItem).filter(
        ReservaItem.id == item_id,
        ReservaItem.deleted_at.is_(None),
    ).first()
    if not item:
        return _jsonify({"erro": f"Item {item_id} não encontrado."}), 404

    start_raw = request.args.get("start_date", "").strip()
    end_raw   = request.args.get("end_date", "").strip()

    if not start_raw or not end_raw:
        return _jsonify({"erro": "start_date e end_date são obrigatórios."}), 400

    try:
        start_date = date.fromisoformat(start_raw)
        end_date   = date.fromisoformat(end_raw)
    except ValueError:
        return _jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    dt_inicio = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    dt_fim    = datetime(end_date.year,   end_date.month,   end_date.day,   23, 59, 59, 999999)

    links = (
        db.session.query(
            Reserva.id.label("reserva_id"),
            Reserva.group_id.label("group_id"),
            Reserva.customer_name.label("customer_name"),
            Reserva.check_in_date.label("check_in_date"),
            Reserva.status.label("status"),
            ReservaItemLink.quantity.label("quantity"),
        )
        .join(ReservaItemLink, ReservaItemLink.reserva_id == Reserva.id)
        .filter(
            ReservaItemLink.item_id == item_id,
            ReservaItemLink.deleted_at.is_(None),
            Reserva.deleted_at.is_(None),
            Reserva.status.notin_(_STATUSES_EXCLUIDOS_VIEW),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        )
        .order_by(Reserva.check_in_date.desc())
        .all()
    )

    resultado = []
    for lk in links:
        nome = (lk.customer_name or "").strip() or "Balcão"
        status_val = lk.status.value if hasattr(lk.status, "value") else str(lk.status)
        check_in_iso = lk.check_in_date.date().isoformat() if lk.check_in_date else None
        resultado.append({
            "reserva_id": lk.reserva_id,
            "group_id": lk.group_id,
            "customer_name": nome,
            "check_in_date": check_in_iso,
            "status": status_val,
            "quantity": lk.quantity,
            "detail_url": url_for("reservas.detalhe", reserva_id=lk.reserva_id),
        })

    return _jsonify({"item_name": item.name, "reservations": resultado}), 200


# Redirect de compatibilidade — URL antiga redirecionada para a nova
@bp.route("/consumo-itens", methods=["GET"])
@login_required
def consumo_itens_redirect():
    """Redireciona a URL legada /consumo-itens para /controle-itens."""
    return redirect(url_for("reservas.controle_itens", **request.args), 301)


@bp.route("/consumo-itens/<int:item_id>/reservas-detalhe", methods=["GET"])
@login_required
def consumo_itens_detalhe_redirect(item_id: int):
    """Redireciona a URL legada de drill-down para o novo endpoint."""
    return redirect(
        url_for("reservas.controle_itens_detalhe", item_id=item_id, **request.args), 301
    )


# ─────────────────────────────────────────────────────────────
# Controle de Itens — gestão de entrega por período
# ─────────────────────────────────────────────────────────────


@bp.route("/controle-itens", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def controle_itens():
    """Exibe o controle de itens por período com visão agregada e por reserva.

    Substituiu /consumo-itens com suporte a confirmação de entrega.

    Query params:
        start_date (str): Data inicial YYYY-MM-DD. Default: hoje.
        end_date   (str): Data final YYYY-MM-DD. Default: igual a start_date.
        view       (str): Modo de exibição — 'por_dia' (padrão) ou 'por_reserva'.

    Returns:
        Renderização de reservas/controle_itens.html.
    """
    from sqlalchemy import func as _func, or_
    from sqlalchemy.orm import selectinload as _sl

    hoje = date.today()
    start_raw = request.args.get("start_date", "").strip()
    end_raw   = request.args.get("end_date", "").strip()
    view_mode = request.args.get("view", "por_dia")
    q         = request.args.get("q", "").strip()

    try:
        start_date = date.fromisoformat(start_raw) if start_raw else hoje
    except ValueError:
        start_date = hoje

    try:
        end_date = date.fromisoformat(end_raw) if end_raw else start_date
    except ValueError:
        end_date = start_date

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    dt_inicio = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    dt_fim    = datetime(end_date.year,   end_date.month,   end_date.day,   23, 59, 59, 999999)

    # Statuses válidos (respeita show_on_dashboard como o dashboard semanal)
    status_configs = db.session.query(ReservaStatusConfig).all()
    statuses_validos = [
        cfg.internal_name for cfg in status_configs if cfg.show_on_dashboard
    ] or ["PAID", "PENDING"]

    # Condição de busca livre (nome, CPF, telefone, #ID)
    search_cond = None
    if q:
        like_q = f"%{q}%"
        q_id = q.lstrip("#")
        or_parts = [
            Reserva.customer_name.ilike(like_q),
            Reserva.customer_document.ilike(like_q),
            _func.COALESCE(
                Reserva.metadata_info["telefone"].as_string(),
                ""
            ).like(like_q),
        ]
        if q_id.isdigit():
            or_parts.append(Reserva.id == int(q_id))
        search_cond = or_(*or_parts)

    # Dados para visão "Por Dia" (agregado)
    itens_agregados = []
    if view_mode == "por_dia":
        base_filters = [
            ReservaItemLink.deleted_at.is_(None),
            Reserva.deleted_at.is_(None),
            ReservaItem.deleted_at.is_(None),
            Reserva.status.in_(statuses_validos),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        ]
        if search_cond is not None:
            base_filters.append(search_cond)
        rows = (
            db.session.query(
                ReservaItem.id.label("item_id"),
                ReservaItem.name.label("item_name"),
                ReservaItem.emoji_label.label("emoji_label"),
                _func.sum(ReservaItemLink.quantity).label("total_qty"),
                _func.sum(
                    db.case((ReservaItemLink.entregue_em.isnot(None), ReservaItemLink.quantity), else_=0)
                ).label("total_entregue"),
            )
            .join(ReservaItemLink, ReservaItemLink.item_id == ReservaItem.id)
            .join(Reserva, Reserva.id == ReservaItemLink.reserva_id)
            .filter(*base_filters)
            .group_by(ReservaItem.id, ReservaItem.name, ReservaItem.emoji_label)
            .order_by(_func.sum(ReservaItemLink.quantity).desc(), ReservaItem.name)
            .all()
        )
        itens_agregados = [
            {
                "item_id": r.item_id,
                "item_name": r.item_name,
                "emoji_label": r.emoji_label or "",
                "total_qty": int(r.total_qty),
                "total_entregue": int(r.total_entregue or 0),
                "total_pendente": int(r.total_qty) - int(r.total_entregue or 0),
            }
            for r in rows
        ]

    # Dados para visão "Por Reserva"
    reservas_com_itens = []
    if view_mode == "por_reserva":
        reserva_filters = [
            Reserva.deleted_at.is_(None),
            Reserva.status.in_(statuses_validos),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        ]
        if search_cond is not None:
            reserva_filters.append(search_cond)
        reservas_com_itens = (
            db.session.query(Reserva)
            .options(
                _sl(Reserva.items).options(
                    _sl(ReservaItemLink.item),
                    _sl(ReservaItemLink.entregue_por),
                )
            )
            .filter(*reserva_filters)
            .order_by(Reserva.check_in_date, Reserva.criado_em)
            .all()
        )
        # Filtra só reservas com pelo menos um item ativo
        reservas_com_itens = [
            r for r in reservas_com_itens
            if any(lk.deleted_at is None for lk in r.items)
        ]

    status_config_map = {s.internal_name: s for s in status_configs}

    return render_template(
        "reservas/controle_itens.html",
        itens_agregados=itens_agregados,
        reservas_com_itens=reservas_com_itens,
        start_date=start_date,
        end_date=end_date,
        view_mode=view_mode,
        status_config_map=status_config_map,
        q=q,
    )


@bp.route("/controle-itens/<int:item_id>/reservas-detalhe", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def controle_itens_detalhe(item_id: int):
    """Retorna JSON com as reservas individuais que contêm um item no período.

    Mesmo comportamento do legado consumo_itens_detalhe, agora sob a nova URL.

    Query params:
        start_date (str): Data inicial YYYY-MM-DD.
        end_date   (str): Data final YYYY-MM-DD.

    Returns:
        JSON: {item_name, reservations: [{reserva_id, customer_name, check_in_date,
               status, quantity, entregue_em, detail_url}]}
    """
    from flask import jsonify as _jfy

    item = db.session.query(ReservaItem).filter(
        ReservaItem.id == item_id,
        ReservaItem.deleted_at.is_(None),
    ).first()
    if not item:
        return _jfy({"erro": f"Item {item_id} não encontrado."}), 404

    start_raw = request.args.get("start_date", "").strip()
    end_raw   = request.args.get("end_date", "").strip()

    if not start_raw or not end_raw:
        return _jfy({"erro": "start_date e end_date são obrigatórios."}), 400

    try:
        start_date = date.fromisoformat(start_raw)
        end_date   = date.fromisoformat(end_raw)
    except ValueError:
        return _jfy({"erro": "Formato inválido. Use YYYY-MM-DD."}), 400

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    dt_inicio = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    dt_fim    = datetime(end_date.year,   end_date.month,   end_date.day,   23, 59, 59, 999999)

    status_configs = db.session.query(ReservaStatusConfig).all()
    statuses_validos = [
        cfg.internal_name for cfg in status_configs if cfg.show_on_dashboard
    ] or ["PAID", "EM_USO"]

    links = (
        db.session.query(
            Reserva.id.label("reserva_id"),
            Reserva.customer_name.label("customer_name"),
            Reserva.check_in_date.label("check_in_date"),
            Reserva.status.label("status"),
            ReservaItemLink.id.label("link_id"),
            ReservaItemLink.quantity.label("quantity"),
            ReservaItemLink.entregue_em.label("entregue_em"),
        )
        .join(ReservaItemLink, ReservaItemLink.reserva_id == Reserva.id)
        .filter(
            ReservaItemLink.item_id == item_id,
            ReservaItemLink.deleted_at.is_(None),
            Reserva.deleted_at.is_(None),
            Reserva.status.in_(statuses_validos),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        )
        .order_by(Reserva.check_in_date.desc())
        .all()
    )

    resultado = []
    for lk in links:
        nome = (lk.customer_name or "").strip() or "Balcão"
        status_val = lk.status.value if hasattr(lk.status, "value") else str(lk.status)
        resultado.append({
            "reserva_id": lk.reserva_id,
            "link_id": lk.link_id,
            "customer_name": nome,
            "check_in_date": lk.check_in_date.date().isoformat() if lk.check_in_date else None,
            "status": status_val,
            "quantity": lk.quantity,
            "entregue_em": lk.entregue_em.isoformat() if lk.entregue_em else None,
            "detail_url": url_for("reservas.detalhe", reserva_id=lk.reserva_id),
        })

    return _jfy({"item_name": item.name, "reservations": resultado}), 200


@bp.route("/controle-itens/link/<int:link_id>/entregar", methods=["POST"])
@login_required
@require_permission("reservas", "edit")
def marcar_item_entregue(link_id: int):
    """Alterna o estado de entrega de um ReservaItemLink (toggle).

    Se não entregue → marca com timestamp e operador.
    Se já entregue → reverte para pendente (desfaz a marcação).

    Args:
        link_id: ID do ReservaItemLink.

    Returns:
        JSON: {ok, action ('marked'|'unmarked'), entregue_em}
    """
    from datetime import timezone as _tz
    from app.core.audit import AuditService

    link = db.session.get(ReservaItemLink, link_id)
    if not link or link.deleted_at is not None:
        return jsonify({"erro": "Item não encontrado."}), 404

    ator_id = session.get("funcionario_id")
    anterior = link.entregue_em.isoformat() if link.entregue_em else None

    if link.entregue_em is not None:
        link.entregue_em = None
        link.entregue_por_id = None
        action = "unmarked"
    else:
        link.entregue_em = datetime.now(_tz.utc)
        link.entregue_por_id = ator_id
        action = "marked"

    AuditService.log_update(
        "reservas",
        entity_id=link.id,
        previous_state={"entregue_em": anterior},
        new_state={"entregue_em": link.entregue_em.isoformat() if link.entregue_em else None},
        actor_id=ator_id,
    )
    db.session.commit()

    return jsonify({
        "ok": True,
        "action": action,
        "entregue_em": link.entregue_em.isoformat() if link.entregue_em else None,
    })


# ─────────────────────────────────────────────────────────────
# Relatório Diário — visão operacional por data
# ─────────────────────────────────────────────────────────────


@bp.route("/relatorio-dia/<date_str>", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def relatorio_dia(date_str: str):
    """Relatório operacional de um dia específico.

    Duas abas:
      - "total": itens por check-in do dia (multi-dia: só conta no check-in).
        Inclui visão por reserva e resumo agregado com texto WhatsApp.
      - "pendentes": itens ainda não entregues em reservas presentes no dia
        (multi-dia: permanece até ser marcado como entregue).

    Args:
        date_str: Data no formato YYYY-MM-DD.

    Query params:
        aba (str): 'total' (padrão) ou 'pendentes'.

    Returns:
        Renderização de reservas/relatorio_dia.html.
    """
    from sqlalchemy.orm import selectinload as _sl
    from sqlalchemy import or_ as _or

    try:
        dia = date.fromisoformat(date_str)
    except ValueError:
        abort(404)

    aba = request.args.get("aba", "total")
    q   = request.args.get("q", "").strip()

    dt_inicio = datetime(dia.year, dia.month, dia.day, 0, 0, 0)
    dt_fim    = datetime(dia.year, dia.month, dia.day, 23, 59, 59, 999999)

    # Statuses que aparecem no dashboard semanal (mesma regra)
    status_configs = db.session.query(ReservaStatusConfig).all()
    status_config_map = {s.internal_name: s for s in status_configs}
    statuses_validos = [
        cfg.internal_name for cfg in status_configs if cfg.show_on_dashboard
    ] or ["PAID", "PENDING"]

    # Condição de busca livre (nome, CPF, telefone, #ID)
    from sqlalchemy import func as _func_rd
    search_cond = None
    if q:
        like_q = f"%{q}%"
        q_id = q.lstrip("#")
        or_parts = [
            Reserva.customer_name.ilike(like_q),
            Reserva.customer_document.ilike(like_q),
            _func_rd.COALESCE(
                Reserva.metadata_info["telefone"].as_string(),
                ""
            ).like(like_q),
        ]
        if q_id.isdigit():
            or_parts.append(Reserva.id == int(q_id))
        search_cond = _or(*or_parts)

    # ── Aba TOTAL: apenas check-ins do dia ──────────────────────────────────
    reservas_total = []
    resumo_total: list[dict] = []   # [{item_id, item_name, emoji_label, total_qty}]
    whatsapp_text = ""

    if aba == "total":
        _total_filters = [
            Reserva.deleted_at.is_(None),
            Reserva.status.in_(statuses_validos),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        ]
        if search_cond is not None:
            _total_filters.append(search_cond)
        reservas_total = (
            db.session.query(Reserva)
            .options(
                _sl(Reserva.items).options(
                    _sl(ReservaItemLink.item),
                    _sl(ReservaItemLink.entregue_por),
                )
            )
            .filter(*_total_filters)
            .order_by(Reserva.check_in_date, Reserva.criado_em)
            .all()
        )
        # Monta resumo agregado
        _agg: dict[int, dict] = {}
        for r in reservas_total:
            for lk in r.items:
                if lk.deleted_at is not None or lk.item is None:
                    continue
                iid = lk.item.id
                if iid not in _agg:
                    _agg[iid] = {
                        "item_id": iid,
                        "item_name": lk.item.name,
                        "emoji_label": lk.item.emoji_label or "",
                        "total_qty": 0,
                    }
                _agg[iid]["total_qty"] += lk.quantity
        resumo_total = sorted(_agg.values(), key=lambda x: -x["total_qty"])

        # Gera texto WhatsApp
        _day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        _lines = [f"📅 *{_day_names[dia.weekday()]}, {dia.strftime('%d/%m')}*\n"]
        for it in resumo_total:
            _emoji = it["emoji_label"] or "▫️"
            _name  = it["item_name"]
            _lines.append(f"{_emoji} x{it['total_qty']}  {_name}")
        _lines.append("\n_Gerado pelo SISTUR_")
        whatsapp_text = "\n".join(_lines)

    # ── Aba PENDENTES: reservas que cobrem o dia com itens não entregues ────
    reservas_pendentes: list[dict] = []   # [{"reserva": r, "links_pendentes": [...], "links_entregues": [...]}]
    resumo_pendentes: list[dict] = []     # agregado por item
    whatsapp_pendentes_text = ""

    if aba == "pendentes":
        _pend_filters = [
            Reserva.deleted_at.is_(None),
            Reserva.status.in_(statuses_validos),
            Reserva.check_in_date <= dt_fim,
            db.or_(
                db.and_(
                    Reserva.check_out_date.is_(None),
                    Reserva.check_in_date >= dt_inicio,
                ),
                Reserva.check_out_date >= dt_inicio,
            ),
        ]
        if search_cond is not None:
            _pend_filters.append(search_cond)
        _query = (
            db.session.query(Reserva)
            .options(
                _sl(Reserva.items).options(
                    _sl(ReservaItemLink.item),
                    _sl(ReservaItemLink.entregue_por),
                )
            )
            .filter(*_pend_filters)
            .order_by(Reserva.check_in_date, Reserva.criado_em)
            .all()
        )

        _agg_pend: dict[int, dict] = {}
        for r in _query:
            # Tickets de entrada são excluídos dos pendentes — só válidos antes da entrada
            links_ativos = [
                lk for lk in r.items
                if lk.deleted_at is None and not (lk.item and lk.item.is_entry_ticket)
            ]
            links_pend   = [lk for lk in links_ativos if lk.entregue_em is None]
            links_entg   = [lk for lk in links_ativos if lk.entregue_em is not None]

            if not links_ativos:
                continue

            reservas_pendentes.append({
                "reserva": r,
                "links_pendentes": links_pend,
                "links_entregues": links_entg,
                "total_links": len(links_ativos),
                "pct_entregue": int(len(links_entg) / len(links_ativos) * 100) if links_ativos else 0,
            })

            # Agrega para resumo por item (apenas pendentes)
            for lk in links_pend:
                if lk.item is None:
                    continue
                iid = lk.item.id
                if iid not in _agg_pend:
                    _agg_pend[iid] = {
                        "item_id": iid,
                        "item_name": lk.item.name,
                        "emoji_label": lk.item.emoji_label or "",
                        "total_pendente": 0,
                    }
                _agg_pend[iid]["total_pendente"] += lk.quantity

        resumo_pendentes = sorted(_agg_pend.values(), key=lambda x: -x["total_pendente"])

        # Gera texto WhatsApp para pendentes (por reserva)
        _day_names_p = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        _wa_lines = [f"⏳ *Pendentes — {_day_names_p[dia.weekday()]}, {dia.strftime('%d/%m')}*"]
        for _entry in reservas_pendentes:
            _r = _entry["reserva"]
            _pending = _entry["links_pendentes"]
            if not _pending:
                continue
            _wa_lines.append("")
            _wa_lines.append(f"*{_r.customer_name}* (#{_r.id})")
            for _lk in _pending:
                if _lk.item:
                    _e = _lk.item.emoji_label or "▫️"
                    _wa_lines.append(f"  {_e} x{_lk.quantity}  {_lk.item.name}")
            if _r.nota_relatorio:
                _wa_lines.append(f"  📌 {_r.nota_relatorio}")
        _wa_lines.append("\n_Gerado pelo SISTUR_")
        whatsapp_pendentes_text = "\n".join(_wa_lines)

    # Dias adjacentes para navegação
    dia_ant = dia - timedelta(days=1)
    dia_prox = dia + timedelta(days=1)

    return render_template(
        "reservas/relatorio_dia.html",
        dia=dia,
        dia_ant=dia_ant,
        dia_prox=dia_prox,
        aba=aba,
        q=q,
        reservas_total=reservas_total,
        resumo_total=resumo_total,
        whatsapp_text=whatsapp_text,
        reservas_pendentes=reservas_pendentes,
        resumo_pendentes=resumo_pendentes,
        whatsapp_pendentes_text=whatsapp_pendentes_text,
        status_config_map=status_config_map,
    )


# ─────────────────────────────────────────────────────────────
# Chat — Chatwoot embutido dentro do módulo de Reservas
# ─────────────────────────────────────────────────────────────

# URL pública — usada apenas para o iframe exibido no browser do usuário
_CHATWOOT_URL = "https://atendimento.cachoeiradogirassol.com.br"
# URL interna — usada para chamadas server-side à API do Chatwoot (evita 401 via proxy HTTPS)
_CHATWOOT_API_URL = "http://chatwoot-rails-1:3000"


@bp.route("/chat", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def chat():
    """Renderiza o painel Chatwoot embutido em iframe dentro do layout de Reservas.

    Mantém a sidebar do módulo totalmente funcional, permitindo que o atendente
    navegue entre as seções de reservas sem perder o contexto do chat.

    Returns:
        Template reservas/chat.html com o iframe apontando para o Chatwoot.
    """
    return render_template(
        "reservas/chat.html",
        chatwoot_url=_CHATWOOT_URL,
    )


@bp.route("/chat/abrir-contato", methods=["GET"])
@login_required
@require_permission("reservas", "view")
def chat_abrir_contato():
    """Busca ou cria o contato no Chatwoot pelo telefone e abre direto na conversa.

    Tenta localizar o contato por múltiplas normalizações do número. Se não
    encontrar, cria o contato com o nome da reserva. Depois abre a conversa
    mais recente ou o perfil do contato.

    Query params:
        tel (str): Número de telefone (aceita qualquer formato).
        nome (str): Nome do cliente para criar o contato caso não exista.

    Returns:
        Template reservas/chat.html com a URL resolvida do Chatwoot.
    """
    import re as _re
    import urllib.request as _urllib
    import urllib.parse as _urlparse
    import json as _json
    from flask import current_app

    tel = request.args.get("tel", "").strip()
    nome = request.args.get("nome", "").strip()
    api_token = current_app.config.get("CHATWOOT_API_TOKEN", "")
    account_id = current_app.config.get("CHATWOOT_ACCOUNT_ID", "1")

    # Fallback imediato se não houver telefone ou token
    if not tel or not api_token:
        return render_template("reservas/chat.html", chatwoot_url=_CHATWOOT_URL)

    # Normaliza: só dígitos
    digits = _re.sub(r"\D", "", tel)

    # Remove +55 se presente para trabalhar com o número local
    local = digits[2:] if digits.startswith("55") and len(digits) >= 12 else digits

    # Variante sem o "9" de discagem: DDD(2) + remove pos[2] se for "9" e total=11
    local_sem9 = (local[:2] + local[3:]) if len(local) == 11 and local[2] == "9" else None

    # Constrói lista de candidatos em ordem: mais específico primeiro
    seen: list[str] = []
    candidates: list[str] = []
    for variant in ([local, local_sem9] if local_sem9 else [local]):
        if variant and variant not in seen:
            seen.append(variant)
            candidates.append("55" + variant)
            candidates.append(variant)

    if len(local) >= 8:
        suf8 = local[-8:]
        if suf8 not in seen:
            candidates.append(suf8)

    def _cw_search(query: str) -> list[dict]:
        """Chama GET /api/v1/accounts/{id}/contacts/search?q=QUERY via rede interna."""
        encoded = _urlparse.quote(query)
        url = (
            f"{_CHATWOOT_API_URL}/api/v1/accounts/{account_id}"
            f"/contacts/search?q={encoded}&include_contacts=true&page=1"
        )
        req = _urllib.Request(url, headers={
            "api_access_token": api_token,
            "Content-Type": "application/json",
        })
        try:
            with _urllib.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
                payload = data.get("payload", [])
                return payload if isinstance(payload, list) else payload.get("contacts", [])
        except Exception:
            return []

    def _cw_criar_contato(contact_nome: str, phone: str) -> dict | None:
        """Cria um contato no Chatwoot e retorna o objeto criado."""
        # Normaliza para E.164 (+55XXXXXXXXXXX)
        d = _re.sub(r"\D", "", phone)
        if not d.startswith("55"):
            d = "55" + d
        e164 = "+" + d
        payload_bytes = _json.dumps({
            "name": contact_nome or e164,
            "phone_number": e164,
        }).encode()
        url = f"{_CHATWOOT_API_URL}/api/v1/accounts/{account_id}/contacts"
        req = _urllib.Request(url, data=payload_bytes, headers={
            "api_access_token": api_token,
            "Content-Type": "application/json",
        })
        try:
            with _urllib.urlopen(req, timeout=5) as resp:
                return _json.loads(resp.read())
        except Exception:
            return None

    def _cw_conversations(contact_id: int) -> list[dict]:
        """Retorna as conversas de um contato via rede interna."""
        url = (
            f"{_CHATWOOT_API_URL}/api/v1/accounts/{account_id}"
            f"/contacts/{contact_id}/conversations"
        )
        req = _urllib.Request(url, headers={
            "api_access_token": api_token,
            "Content-Type": "application/json",
        })
        try:
            with _urllib.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
                return data.get("payload", [])
        except Exception:
            return []

    # Tenta cada candidato até encontrar
    contact = None
    for candidate in candidates:
        results = _cw_search(candidate)
        if results:
            contact = results[0]
            break

    if not contact:
        # Não encontrou → cria o contato com nome e telefone da reserva
        created = _cw_criar_contato(nome, tel)
        if created:
            # A API de criação pode retornar o contato direto ou aninhado em "contact"
            contact = created.get("contact", created)

    if not contact or not contact.get("id"):
        # Criação também falhou → fallback para a lista de contatos
        fallback_url = f"{_CHATWOOT_URL}/app/accounts/{account_id}/contacts"
        return render_template(
            "reservas/chat.html",
            chatwoot_url=fallback_url,
            contact_not_found=True,
            tel_display=tel,
        )

    contact_id = contact.get("id")

    # Tenta abrir a conversa mais recente
    conversations = _cw_conversations(contact_id)
    if conversations:
        latest = sorted(conversations, key=lambda c: c.get("id", 0), reverse=True)[0]
        conv_id = latest.get("id")
        chatwoot_url = (
            f"{_CHATWOOT_URL}/app/accounts/{account_id}/conversations/{conv_id}"
        )
    else:
        # Contato existe mas sem conversas → vai para o perfil
        chatwoot_url = (
            f"{_CHATWOOT_URL}/app/accounts/{account_id}/contacts/{contact_id}"
        )

    return render_template("reservas/chat.html", chatwoot_url=chatwoot_url)
