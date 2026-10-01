# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint Avisos — Módulo de Notificações Internas e Ajuste de Ponto.

Rotas
-----
GET  /avisos/                              -> index()           — tabs: notificações, aprovações, minhas solicitações
POST /avisos/<id>/marcar-lido             -> marcar_lido()     — marca aviso como lido (JSON)
POST /avisos/marcar-todos-lidos           -> marcar_todos()    — marca todos como lidos
POST /avisos/<id>/deletar                 -> deletar()         — exclui aviso (com guarda IDOR)
POST /avisos/ajuste/<id>/aprovar          -> aprovar_ajuste()  — aprova solicitação de ajuste
POST /avisos/ajuste/<id>/rejeitar         -> rejeitar_ajuste() — rejeita solicitação de ajuste
POST /avisos/pdv/turno/<id>/aprovar       -> aprovar_pdv_turno() — aprova quebra de caixa PDV

Todas as rotas exigem sessão ativa (@login_required).
Segurança IDOR: toda mutação verifica aviso.destinatario_id == fid antes de agir.
Permissão de aprovação: ajuste_ponto.aprovar (RBAC via has_permission).
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

from app.blueprints.portal.routes import login_required
from app.core.permissions import has_permission
from app.extensions import db
from app.models.avisos import Aviso
from app.services.aviso_service import AvisoService
from app.services.ajuste_ponto_service import AjustePontoService
from app.services.pdv_service import PdvService

bp = Blueprint("avisos", __name__)


# ---------------------------------------------------------------------------
# Helper interno
# ---------------------------------------------------------------------------

def _get_fid() -> int:
    """Retorna o ID do funcionário logado a partir da sessão Flask."""
    return session["funcionario_id"]


# ---------------------------------------------------------------------------
# Histórico / listagem (com tabs)
# ---------------------------------------------------------------------------

@bp.route("/", methods=["GET"])
@login_required
def index():
    """Exibe a lista de notificações do colaborador com tabs de navegação.

    Tabs disponíveis:
        notificacoes  — avisos gerais do colaborador (padrão)
        aprovacoes    — solicitações de ajuste PENDENTE (requer ajuste_ponto.aprovar)
        minhas        — histórico de solicitações do próprio colaborador

    Query params:
        page (int): Página atual para tab notificacoes. Padrão: 1.
        tab  (str): Tab ativa. Padrão: 'notificacoes'.

    Returns:
        Renderização de avisos/index.html com dados das três tabs.
    """
    fid = _get_fid()
    tab = request.args.get("tab", "notificacoes")
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    avisos_pag = AvisoService.listar(fid, page=page)
    tem_permissao_ajuste = has_permission(fid, "ajuste_ponto", "aprovar")
    tem_permissao_pdv = has_permission(fid, "pdv", "manage_shift")
    tem_permissao_aprovar = tem_permissao_ajuste or tem_permissao_pdv

    ajustes_pendentes = []
    if tem_permissao_ajuste:
        ajustes_pendentes = AjustePontoService.listar_pendentes()
    pdv_turnos_pendentes = []
    if tem_permissao_pdv:
        pdv_turnos_pendentes = PdvService.listar_turnos_pendentes()

    minhas_solicitacoes = AjustePontoService.listar_por_funcionario(fid)

    return render_template(
        "avisos/index.html",
        avisos=avisos_pag,
        page=page,
        tab=tab,
        tem_permissao_aprovar=tem_permissao_aprovar,
        tem_permissao_ajuste=tem_permissao_ajuste,
        tem_permissao_pdv=tem_permissao_pdv,
        ajustes_pendentes=ajustes_pendentes,
        pdv_turnos_pendentes=pdv_turnos_pendentes,
        minhas_solicitacoes=minhas_solicitacoes,
    )


# ---------------------------------------------------------------------------
# Marcar como lido (AJAX-friendly — retorna JSON)
# ---------------------------------------------------------------------------

@bp.route("/<int:aviso_id>/marcar-lido", methods=["POST"])
@login_required
def marcar_lido(aviso_id: int):
    """Marca um aviso específico como lido.

    Verifica que o aviso pertence ao funcionário logado antes de agir (IDOR guard).

    Args:
        aviso_id: PK do aviso a marcar.

    Returns:
        JSON {"ok": true} em sucesso ou {"ok": false, "erro": "..."} em falha.
        Redireciona para /avisos/ se chamado sem Accept: application/json.
    """
    fid = _get_fid()
    aviso = db.session.get(Aviso, aviso_id)

    if not aviso or aviso.destinatario_id != fid:
        if request.headers.get("Accept") == "application/json":
            return jsonify({"ok": False, "erro": "Aviso não encontrado."}), 404
        abort(404)

    try:
        AvisoService.marcar_lido(aviso_id, ator_id=fid)
        if request.headers.get("Accept") == "application/json":
            return jsonify({"ok": True})
    except ValueError as exc:
        if request.headers.get("Accept") == "application/json":
            return jsonify({"ok": False, "erro": str(exc)}), 400
        flash(str(exc), "erro")

    return redirect(url_for("avisos.index"))


# ---------------------------------------------------------------------------
# Marcar todos como lidos
# ---------------------------------------------------------------------------

@bp.route("/marcar-todos-lidos", methods=["POST"])
@login_required
def marcar_todos():
    """Marca todos os avisos não lidos do colaborador logado como lidos.

    Returns:
        Redirect para /avisos/ com flash de confirmação.
    """
    fid = _get_fid()
    count = AvisoService.marcar_todos_lidos(fid, ator_id=fid)
    if count > 0:
        flash(f"{count} aviso(s) marcado(s) como lido(s).", "sucesso")
    return redirect(url_for("avisos.index"))


# ---------------------------------------------------------------------------
# Excluir aviso
# ---------------------------------------------------------------------------

@bp.route("/<int:aviso_id>/deletar", methods=["POST"])
@login_required
def deletar(aviso_id: int):
    """Remove um aviso do colaborador logado.

    Verifica que o aviso pertence ao funcionário logado antes de agir (IDOR guard).

    Args:
        aviso_id: PK do aviso a remover.

    Returns:
        Redirect para /avisos/ com flash de confirmação ou erro.
    """
    fid = _get_fid()
    aviso = db.session.get(Aviso, aviso_id)

    if not aviso or aviso.destinatario_id != fid:
        abort(404)

    try:
        AvisoService.deletar(aviso_id, ator_id=fid)
        flash("Aviso excluído.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("avisos.index"))


# ---------------------------------------------------------------------------
# Aprovar ajuste de ponto
# ---------------------------------------------------------------------------

@bp.route("/ajuste/<int:ajuste_id>/aprovar", methods=["POST"])
@login_required
def aprovar_ajuste(ajuste_id: int):
    """Aprova uma solicitação de ajuste de ponto.

    Requer permissão ajuste_ponto.aprovar. Chama AjustePontoService.aprovar(),
    que cria/edita a TimeEntry e recalcula o TimeDay automaticamente.

    Args:
        ajuste_id: PK da AjustePontoRequest a aprovar.

    Returns:
        Redirect para /avisos/?tab=aprovacoes com flash de resultado.
    """
    fid = _get_fid()
    if not has_permission(fid, "ajuste_ponto", "aprovar"):
        abort(403)

    try:
        AjustePontoService.aprovar(ajuste_id, supervisor_id=fid)
        flash("Solicitação de ajuste aprovada com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("avisos.index", tab="aprovacoes"))


# ---------------------------------------------------------------------------
# Rejeitar ajuste de ponto
# ---------------------------------------------------------------------------

@bp.route("/ajuste/<int:ajuste_id>/rejeitar", methods=["POST"])
@login_required
def rejeitar_ajuste(ajuste_id: int):
    """Rejeita uma solicitação de ajuste de ponto com motivo obrigatório.

    Requer permissão ajuste_ponto.aprovar. O campo 'motivo_rejeicao' do
    formulário é obrigatório.

    Args:
        ajuste_id: PK da AjustePontoRequest a rejeitar.

    Returns:
        Redirect para /avisos/?tab=aprovacoes com flash de resultado.
    """
    fid = _get_fid()
    if not has_permission(fid, "ajuste_ponto", "aprovar"):
        abort(403)

    motivo_rejeicao = (request.form.get("motivo_rejeicao") or "").strip()
    if not motivo_rejeicao:
        flash("Informe o motivo da rejeição.", "erro")
        return redirect(url_for("avisos.index", tab="aprovacoes"))

    try:
        AjustePontoService.rejeitar(ajuste_id, supervisor_id=fid, motivo_rejeicao=motivo_rejeicao)
        flash("Solicitação de ajuste rejeitada.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("avisos.index", tab="aprovacoes"))


# ---------------------------------------------------------------------------
# Aprovar quebra de caixa PDV
# ---------------------------------------------------------------------------

@bp.route("/pdv/turno/<int:shift_id>/aprovar", methods=["POST"])
@login_required
def aprovar_pdv_turno(shift_id: int):
    """Aprova fechamento de turno PDV com quebra de caixa."""
    fid = _get_fid()
    if not has_permission(fid, "pdv", "manage_shift"):
        abort(403)

    try:
        PdvService.aprovar_fechamento(shift_id, aprovador_id=fid, ator_id=fid)
        flash("Fechamento de caixa PDV aprovado com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("avisos.index", tab="aprovacoes"))
