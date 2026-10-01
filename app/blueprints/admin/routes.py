# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Admin — Audit Log Viewer.

Rotas
-----
GET /admin/audit-logs  → visualizador de logs de auditoria com filtros e paginação

Controle de Acesso (3 níveis)
------------------------------
1. super_admin (Role.is_super_admin=True)  → acesso irrestrito a todos os logs
2. gerente    (permissão audit.view_all)   → acesso irrestrito a todos os logs
3. supervisor (permissão audit.view)       → acesso limitado aos logs do próprio area_id
"""

from __future__ import annotations

import functools
from datetime import date, datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, cast, Date as SADate

from app.core.models import AuditLog
from app.core.permissions import has_permission
from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.integration_log import IntegrationLog
from app.models.reservas import Reserva, ReservaItem, ReservaItemLink, ReservaStatus

bp = Blueprint("admin", __name__)

# Número de registros por página
_PER_PAGE = 50


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_ator_sessao() -> Funcionario:
    """Carrega o Funcionario autenticado na sessão ou aborta com 401.

    Returns:
        Instância de Funcionario ativa correspondente à sessão atual.

    Raises:
        HTTP 401: Se não houver sessão ativa ou o funcionário estiver inativo.
    """
    fid = session.get("funcionario_id")
    if not fid:
        abort(401)
    f = db.session.get(Funcionario, fid)
    if not f or not f.ativo:
        session.clear()
        abort(401)
    return f


def _login_required(fn):
    """Redireciona para login se não houver sessão ativa."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("funcionario_id"):
            return redirect(url_for("portal.login"))
        return fn(*args, **kwargs)
    return wrapper


def _can_view_audit(funcionario_id: int) -> bool:
    """Verifica se o funcionário tem acesso ao módulo de auditoria (qualquer nível).

    Args:
        funcionario_id: ID do funcionário a verificar.

    Returns:
        True se tiver audit.view_all OU audit.view.
    """
    return has_permission(funcionario_id, "audit", "view_all") or \
           has_permission(funcionario_id, "audit", "view")


def _can_view_all(funcionario_id: int) -> bool:
    """Verifica se o funcionário tem acesso irrestrito a todos os logs.

    Retorna True para super_admin (bypass automático via has_permission)
    e para funcionários com permissão audit.view_all.

    Args:
        funcionario_id: ID do funcionário a verificar.

    Returns:
        True se tiver acesso irrestrito (super_admin ou gerente).
    """
    return has_permission(funcionario_id, "audit", "view_all")


# ---------------------------------------------------------------------------
# Rota principal
# ---------------------------------------------------------------------------

@bp.route("/audit-logs", methods=["GET"])
@_login_required
def audit_logs():
    """Lista logs de auditoria com filtros dinâmicos e paginação server-side.

    Níveis de acesso:
        - super_admin / gerente (audit.view_all): vê todos os logs.
        - supervisor (audit.view): vê apenas logs onde area_id == seu area_id.
        - Sem permissão: 403.

    Query params:
        user_id (int, opcional):  Filtra pelo ID do ator (Funcionario).
        module  (str, opcional):  Filtra pelo módulo (e.g. 'auth', 'ponto').
        page    (int, opcional):  Número da página (default=1).

    Returns:
        Renderização de admin/audit_logs.html com paginação e filtros ativos.
    """
    ator = _get_ator_sessao()

    # Verificação de acesso
    if not _can_view_audit(ator.id):
        abort(403)

    acesso_total = _can_view_all(ator.id)

    # ── Filtros via query params ────────────────────────────────────────────
    filtro_user_id = request.args.get("user_id", type=int)
    filtro_module  = request.args.get("module", "").strip() or None
    page           = request.args.get("page", 1, type=int)

    # ── Construção da query base ────────────────────────────────────────────
    query = db.session.query(AuditLog)

    if not acesso_total:
        # Supervisor: restringe ao area_id do próprio funcionário
        if ator.area_id:
            query = query.filter(AuditLog.area_id == ator.area_id)
        else:
            # Supervisor sem área definida não vê nada
            query = query.filter(AuditLog.id == None)  # noqa: E711

    # Filtros dinâmicos adicionais
    if filtro_user_id:
        query = query.filter(AuditLog.user_id == filtro_user_id)
    if filtro_module:
        query = query.filter(AuditLog.module == filtro_module)

    # Ordenação e paginação
    query = query.order_by(AuditLog.created_at.desc())
    pagination = query.paginate(page=page, per_page=_PER_PAGE, error_out=False)

    # ── Dados auxiliares para os dropdowns ─────────────────────────────────
    # Lista de todos os módulos registrados nos logs (para o dropdown)
    modulos_query = db.session.query(AuditLog.module).distinct().order_by(AuditLog.module)
    if not acesso_total and ator.area_id:
        modulos_query = modulos_query.filter(AuditLog.area_id == ator.area_id)
    modulos = [row[0] for row in modulos_query.all()]

    # Lista de funcionários que aparecem nos logs (para o dropdown de ator)
    user_ids_query = (
        db.session.query(AuditLog.user_id)
        .filter(AuditLog.user_id.isnot(None))
        .distinct()
    )
    if not acesso_total and ator.area_id:
        user_ids_query = user_ids_query.filter(AuditLog.area_id == ator.area_id)
    user_ids = [row[0] for row in user_ids_query.all()]

    # Resolve nomes dos atores (user_id é polimórfico: aponta para sistur_funcionarios)
    funcionarios_map: dict[int, str] = {}
    if user_ids:
        funcs = (
            db.session.query(Funcionario.id, Funcionario.nome)
            .filter(Funcionario.id.in_(user_ids))
            .all()
        )
        funcionarios_map = {f.id: f.nome for f in funcs}

    return render_template(
        "admin/audit_logs.html",
        pagination=pagination,
        logs=pagination.items,
        modulos=modulos,
        funcionarios_map=funcionarios_map,
        filtro_user_id=filtro_user_id,
        filtro_module=filtro_module,
        acesso_total=acesso_total,
        ator=ator,
    )


# ---------------------------------------------------------------------------
# Logs de Integração — Módulo de monitoramento de erros de pagamento
# ---------------------------------------------------------------------------

_LOGS_PER_PAGE = 25


@bp.route("/logs", methods=["GET"])
@_login_required
def integration_logs():
    """Visualizador de logs de erros de integração (MP Bricks, WordPress, Sistur Sync).

    Suporta seleção de um dia único ou intervalo de datas via calendário interativo.
    A sidebar exibe um calendário com indicadores visuais nos dias que possuem erros.

    Acesso restrito a super_admin e gerente (audit.view_all) — os logs contêm
    dados sensíveis de pagamento.

    Query params:
        data (str, opcional): Data de início (YYYY-MM-DD). Default: hoje.
        data_fim (str, opcional): Data de fim (YYYY-MM-DD). Default: igual a data.
        source (str, opcional): Filtra por error_source (MP_BRICKS, WP_INTERNAL, SISTUR_QR, SISTUR_SYNC).
        category (str, opcional): Filtra por error_category (system, client_bank, invalid_data).
        page (int, opcional): Página (default=1).

    Returns:
        Renderização de admin/integration_logs.html.
    """
    ator = _get_ator_sessao()

    if not _can_view_all(ator.id):
        abort(403)

    # ── Todos os dias com erros — sem limite, para o calendário ─────────────
    dias_com_erros_rows = (
        db.session.query(
            cast(IntegrationLog.created_at, SADate).label("dia"),
            func.count(IntegrationLog.id).label("total"),
        )
        .group_by("dia")
        .all()
    )
    dias_com_erros_map = {
        (row.dia.isoformat() if hasattr(row.dia, "isoformat") else str(row.dia)): row.total
        for row in dias_com_erros_rows
    }

    # ── Período selecionado via query params ─────────────────────────────────
    data_param = request.args.get("data", "").strip()
    data_fim_param = request.args.get("data_fim", "").strip()
    try:
        data_inicio = date.fromisoformat(data_param) if data_param else date.today()
    except ValueError:
        data_inicio = date.today()
    try:
        data_fim = date.fromisoformat(data_fim_param) if data_fim_param else data_inicio
    except ValueError:
        data_fim = data_inicio
    if data_fim < data_inicio:
        data_inicio, data_fim = data_fim, data_inicio

    filtro_source   = request.args.get("source",   "").strip().upper() or None
    filtro_category = request.args.get("category", "").strip().lower() or None
    page = request.args.get("page", 1, type=int)

    # ── Query dos logs no período selecionado ────────────────────────────────
    inicio = datetime(data_inicio.year, data_inicio.month, data_inicio.day, 0, 0, 0)
    fim    = datetime(data_fim.year,    data_fim.month,    data_fim.day,    23, 59, 59, 999999)

    query = (
        db.session.query(IntegrationLog)
        .filter(
            IntegrationLog.created_at >= inicio,
            IntegrationLog.created_at <= fim,
        )
    )

    if filtro_source:
        query = query.filter(IntegrationLog.error_source == filtro_source)

    if filtro_category:
        query = query.filter(IntegrationLog.error_category == filtro_category)

    query = query.order_by(IntegrationLog.created_at.desc())
    pagination = query.paginate(page=page, per_page=_LOGS_PER_PAGE, error_out=False)

    return render_template(
        "admin/integration_logs.html",
        pagination=pagination,
        logs=pagination.items,
        dias_com_erros_map=dias_com_erros_map,
        data_inicio=data_inicio,
        data_fim=data_fim,
        filtro_source=filtro_source,
        filtro_category=filtro_category,
        ator=ator,
    )


# ---------------------------------------------------------------------------
# Relatório de Consumo de Itens
# ---------------------------------------------------------------------------

# Statuses que representam reservas com consumo real (excluem canceladas e versões arquivadas).
_STATUSES_CONSUMO = (
    ReservaStatus.PENDING,
    ReservaStatus.PARTIALLY_PAID,
    ReservaStatus.PAID,
    ReservaStatus.COMPLETED,
)


@bp.route("/reports/items-consumption", methods=["GET"])
@_login_required
def items_consumption():
    """Relatório agregado de consumo de itens por período.

    Lista todos os itens que aparecem em reservas ativas/concluídas dentro
    do intervalo de datas informado, somando as quantidades por item.
    Exclui reservas canceladas (CANCELED) e versões arquivadas (ARCHIVED_VERSION).

    Query params:
        start_date (str, opcional): Data inicial no formato YYYY-MM-DD.
                                    Default: primeiro dia do mês atual.
        end_date   (str, opcional): Data final no formato YYYY-MM-DD.
                                    Default: igual a start_date.

    Returns:
        Renderização de admin/items_consumption.html com a lista agregada
        e os filtros ativos.

    Raises:
        HTTP 403: Se o usuário não tiver reservas.view nem audit.view_all.
    """
    ator = _get_ator_sessao()

    # Acesso: quem pode ver relatórios de reservas ou audit geral
    pode_ver = (
        has_permission(ator.id, "reservas", "view")
        or _can_view_all(ator.id)
    )
    if not pode_ver:
        abort(403)

    # ── Período selecionado ─────────────────────────────────────────────────
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

    # Converte para datetime para comparação com check_in_date (DateTime)
    dt_inicio = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    dt_fim    = datetime(end_date.year,   end_date.month,   end_date.day,   23, 59, 59, 999999)

    # ── Query agregada: soma de quantity por item ───────────────────────────
    # Uma única query com dois JOINs:
    #   ReservaItemLink → Reserva  (filtro de status e período)
    #   ReservaItemLink → ReservaItem (nome do item)
    #
    # Índices usados:
    #   - sistur_reservas.check_in_date  (indexed)
    #   - sistur_reservas.status         (indexed)
    #   - sistur_reserva_item_links.reserva_id (indexed)
    #   - sistur_reserva_item_links.item_id    (indexed)
    rows = (
        db.session.query(
            ReservaItem.id.label("item_id"),
            ReservaItem.name.label("item_name"),
            func.sum(ReservaItemLink.quantity).label("total_qty"),
        )
        .join(ReservaItemLink, ReservaItemLink.item_id == ReservaItem.id)
        .join(Reserva, Reserva.id == ReservaItemLink.reserva_id)
        .filter(
            # Soft deletes em ambas as tabelas
            ReservaItemLink.deleted_at.is_(None),
            Reserva.deleted_at.is_(None),
            ReservaItem.deleted_at.is_(None),
            # Apenas reservas com consumo real
            Reserva.status.in_(_STATUSES_CONSUMO),
            # Filtro de período via check_in_date
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        )
        .group_by(ReservaItem.id, ReservaItem.name)
        .order_by(func.sum(ReservaItemLink.quantity).desc(), ReservaItem.name)
        .all()
    )

    itens = [
        {"item_id": r.item_id, "item_name": r.item_name, "total_qty": int(r.total_qty)}
        for r in rows
    ]

    return render_template(
        "admin/items_consumption.html",
        itens=itens,
        start_date=start_date,
        end_date=end_date,
        ator=ator,
    )


@bp.route("/reports/items-consumption/<int:item_id>/reservations", methods=["GET"])
@_login_required
def items_consumption_detail(item_id: int):
    """Retorna JSON com as reservas individuais que consumiram um item no período.

    Endpoint de drill-down: chamado assincronamente pelo frontend quando o
    usuário clica numa linha do relatório de consumo para ver o detalhamento.

    Query params:
        start_date (str, required): Data inicial (YYYY-MM-DD).
        end_date   (str, required): Data final (YYYY-MM-DD).

    Returns:
        JSON com lista de reservas:
            [{"reserva_id", "group_id", "customer_name", "check_in_date",
              "status", "quantity", "detail_url"}]

        200 — Lista (pode ser vazia).
        400 — Parâmetros de data ausentes ou inválidos.
        403 — Sem permissão.
        404 — Item não encontrado.
    """
    from flask import jsonify

    ator = _get_ator_sessao()

    pode_ver = (
        has_permission(ator.id, "reservas", "view")
        or _can_view_all(ator.id)
    )
    if not pode_ver:
        return jsonify({"erro": "Acesso negado."}), 403

    # ── Valida item ─────────────────────────────────────────────────────────
    item = db.session.query(ReservaItem).filter(
        ReservaItem.id == item_id,
        ReservaItem.deleted_at.is_(None),
    ).first()
    if not item:
        return jsonify({"erro": f"Item {item_id} não encontrado."}), 404

    # ── Valida datas ────────────────────────────────────────────────────────
    start_raw = request.args.get("start_date", "").strip()
    end_raw   = request.args.get("end_date", "").strip()

    if not start_raw or not end_raw:
        return jsonify({"erro": "start_date e end_date são obrigatórios."}), 400

    try:
        start_date = date.fromisoformat(start_raw)
        end_date   = date.fromisoformat(end_raw)
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    dt_inicio = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    dt_fim    = datetime(end_date.year,   end_date.month,   end_date.day,   23, 59, 59, 999999)

    # ── Query de drill-down ─────────────────────────────────────────────────
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
            Reserva.status.in_(_STATUSES_CONSUMO),
            Reserva.check_in_date >= dt_inicio,
            Reserva.check_in_date <= dt_fim,
        )
        .order_by(Reserva.check_in_date.desc())
        .all()
    )

    resultado = []
    for lk in links:
        # customer_name vazio ou nulo → "Balcão"
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

    return jsonify({"item_name": item.name, "reservations": resultado}), 200
