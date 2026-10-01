# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint de Organizacao — Funções e Equipes operacionais.

Prefixo: /rh/organizacao
Permissões: equipes.[view|create|edit|delete], funcoes.[view|create|edit|delete]
"""

from __future__ import annotations

import calendar as _calendar
from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.blueprints.portal.routes import login_required
from app.core.models import Area
from app.core.permissions import require_permission
from app.extensions import db
from app.models.organizacao import Equipe, Funcao
from app.services.organizacao_service import OrganizacaoService

bp = Blueprint("organizacao", __name__)


def _get_ator_id() -> int:
    from flask import session
    return session["funcionario_id"]


def _areas() -> list[Area]:
    return db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()


# ---------------------------------------------------------------------------
# Funções
# ---------------------------------------------------------------------------

@bp.route("/funcoes")
@login_required
@require_permission("funcoes", "view")
def listar_funcoes():
    """Lista todas as funções com filtro opcional por área e status.

    Query params:
        area_id (int): Filtra por área.
        status (str):  "ativo" | "inativo" | "todos". Padrão: "ativo".
    """
    area_id = int(request.args["area_id"]) if request.args.get("area_id") else None
    status = request.args.get("status", "ativo")
    apenas_ativas = status != "inativo" if status == "todos" else status == "ativo"

    funcoes = OrganizacaoService.listar_funcoes(area_id=area_id, apenas_ativas=apenas_ativas)
    return render_template(
        "organizacao/funcoes.html",
        funcoes=funcoes,
        areas=_areas(),
        area_id_filtro=area_id,
        status_filtro=status,
    )


@bp.route("/funcoes/nova", methods=["GET", "POST"])
@login_required
@require_permission("funcoes", "create")
def nova_funcao():
    """Cadastra uma nova Funcao operacional.

    Form fields:
        nome (str): Obrigatório.
        area_id (int): Opcional.
    """
    if request.method == "GET":
        return render_template("organizacao/funcao_form.html", funcao=None, areas=_areas())

    try:
        f = OrganizacaoService.criar_funcao(
            nome=request.form.get("nome", ""),
            area_id=int(request.form["area_id"]) if request.form.get("area_id") else None,
            ator_id=_get_ator_id(),
        )
        flash(f"Função '{f.nome}' criada com sucesso.", "sucesso")
        return redirect(url_for("organizacao.listar_funcoes"))
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template("organizacao/funcao_form.html", funcao=None, areas=_areas()), 400


@bp.route("/funcoes/<int:funcao_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("funcoes", "edit")
def editar_funcao(funcao_id: int):
    """Edita nome e área de uma Funcao.

    Path params:
        funcao_id (int): PK da função.

    Form fields:
        nome (str): Obrigatório.
        area_id (int): Opcional.
    """
    funcao = db.session.get(Funcao, funcao_id)
    if not funcao:
        flash("Função não encontrada.", "erro")
        return redirect(url_for("organizacao.listar_funcoes"))

    if request.method == "GET":
        return render_template("organizacao/funcao_form.html", funcao=funcao, areas=_areas())

    try:
        f = OrganizacaoService.atualizar_funcao(
            funcao_id=funcao_id,
            nome=request.form.get("nome", ""),
            area_id=int(request.form["area_id"]) if request.form.get("area_id") else None,
            ator_id=_get_ator_id(),
        )
        flash(f"Função '{f.nome}' atualizada.", "sucesso")
        return redirect(url_for("organizacao.listar_funcoes"))
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template("organizacao/funcao_form.html", funcao=funcao, areas=_areas()), 400


@bp.route("/funcoes/<int:funcao_id>/desativar", methods=["POST"])
@login_required
@require_permission("funcoes", "delete")
def desativar_funcao(funcao_id: int):
    """Desativa uma Funcao (is_active=False).

    Path params:
        funcao_id (int): PK da função.
    """
    try:
        OrganizacaoService.desativar_funcao(funcao_id, ator_id=_get_ator_id())
        flash("Função desativada.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")
    return redirect(url_for("organizacao.listar_funcoes"))


# ---------------------------------------------------------------------------
# Equipes
# ---------------------------------------------------------------------------

@bp.route("/equipes")
@login_required
@require_permission("equipes", "view")
def listar_equipes():
    """Lista equipes com filtro opcional por área e status.

    Query params:
        area_id (int): Filtra por área.
        status (str):  "ativo" | "inativo" | "todos". Padrão: "ativo".
    """
    area_id = int(request.args["area_id"]) if request.args.get("area_id") else None
    status = request.args.get("status", "ativo")
    apenas_ativas = status != "inativo" if status == "todos" else status == "ativo"

    equipes = OrganizacaoService.listar_equipes(area_id=area_id, apenas_ativas=apenas_ativas)
    return render_template(
        "organizacao/equipes.html",
        equipes=equipes,
        areas=_areas(),
        area_id_filtro=area_id,
        status_filtro=status,
    )


@bp.route("/equipes/nova", methods=["GET", "POST"])
@login_required
@require_permission("equipes", "create")
def nova_equipe():
    """Cadastra uma nova Equipe.

    Form fields:
        nome (str): Obrigatório.
        area_id (int): Opcional.
    """
    if request.method == "GET":
        return render_template("organizacao/equipe_form.html", equipe=None, areas=_areas())

    try:
        e = OrganizacaoService.criar_equipe(
            nome=request.form.get("nome", ""),
            area_id=int(request.form["area_id"]) if request.form.get("area_id") else None,
            ator_id=_get_ator_id(),
        )
        flash(f"Equipe '{e.nome}' criada com sucesso.", "sucesso")
        return redirect(url_for("organizacao.listar_equipes"))
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template("organizacao/equipe_form.html", equipe=None, areas=_areas()), 400


@bp.route("/equipes/<int:equipe_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("equipes", "edit")
def editar_equipe(equipe_id: int):
    """Edita nome, área e composição de membros de uma Equipe.

    Path params:
        equipe_id (int): PK da equipe.

    Form fields (POST composição de membros):
        nome (str): Obrigatório.
        area_id (int): Opcional.
        membros[N][funcionario_id] (int): ID do membro.
        membros[N][funcao_id] (int): ID da função (opcional).
    """
    from app.models.funcionario import Funcionario
    from app.models.organizacao import EquipeMembro

    equipe = db.session.get(Equipe, equipe_id)
    if not equipe:
        flash("Equipe não encontrada.", "erro")
        return redirect(url_for("organizacao.listar_equipes"))

    funcionarios_ativos = (
        db.session.query(Funcionario)
        .filter_by(ativo=True)
        .order_by(Funcionario.nome)
        .all()
    )
    funcoes_ativas = OrganizacaoService.listar_funcoes(apenas_ativas=True)
    alertas_disp = OrganizacaoService.alertas_disponibilidade(date.today(), equipe_id=equipe_id)

    if request.method == "GET":
        return render_template(
            "organizacao/equipe_form.html",
            equipe=equipe,
            areas=_areas(),
            funcionarios=funcionarios_ativos,
            funcoes=funcoes_ativas,
            alertas_disp=alertas_disp,
        )

    ator_id = _get_ator_id()

    try:
        equipe = OrganizacaoService.atualizar_equipe(
            equipe_id=equipe_id,
            nome=request.form.get("nome", ""),
            area_id=int(request.form["area_id"]) if request.form.get("area_id") else None,
            ator_id=ator_id,
        )
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template(
            "organizacao/equipe_form.html",
            equipe=equipe,
            areas=_areas(),
            funcionarios=funcionarios_ativos,
            funcoes=funcoes_ativas,
            alertas_disp=alertas_disp,
        ), 400

    # Sincroniza membros: remove os que não vieram no form, adiciona/atualiza os que vieram
    fids_form = request.form.getlist("membro_funcionario_id")
    fids_form = [int(x) for x in fids_form if x.isdigit()]
    funcoes_form = {
        int(fid): (int(fid_f) if (fid_f := request.form.get(f"membro_funcao_{fid}")) and fid_f.isdigit() else None)
        for fid in fids_form
    }

    membros_atuais = {m.funcionario_id: m for m in equipe.membros}

    for fid in list(membros_atuais):
        if fid not in fids_form:
            try:
                OrganizacaoService.remover_membro(equipe_id, fid, ator_id)
            except ValueError:
                pass

    for fid in fids_form:
        fid_funcao = funcoes_form.get(fid)
        if fid in membros_atuais:
            if membros_atuais[fid].funcao_id != fid_funcao:
                try:
                    OrganizacaoService.atualizar_funcao_membro(equipe_id, fid, fid_funcao, ator_id)
                except ValueError:
                    pass
        else:
            try:
                OrganizacaoService.adicionar_membro(equipe_id, fid, fid_funcao, ator_id)
            except ValueError:
                pass

    flash(f"Equipe '{equipe.nome}' atualizada.", "sucesso")
    return redirect(url_for("organizacao.listar_equipes"))


@bp.route("/equipes/<int:equipe_id>/desativar", methods=["POST"])
@login_required
@require_permission("equipes", "delete")
def desativar_equipe(equipe_id: int):
    """Desativa uma Equipe (is_active=False).

    Path params:
        equipe_id (int): PK da equipe.
    """
    try:
        OrganizacaoService.desativar_equipe(equipe_id, ator_id=_get_ator_id())
        flash("Equipe desativada.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")
    return redirect(url_for("organizacao.listar_equipes"))


# ---------------------------------------------------------------------------
# Áreas
# ---------------------------------------------------------------------------

@bp.route("/areas")
@login_required
@require_permission("funcoes", "view")
def listar_areas():
    """Lista todas as áreas com filtro de status.

    Query params:
        status (str): "ativo" | "inativo" | "todos". Padrão: "ativo".
    """
    status = request.args.get("status", "ativo")
    apenas_ativas = status == "ativo"
    areas = OrganizacaoService.listar_areas(apenas_ativas=apenas_ativas if status != "todos" else False)
    return render_template(
        "organizacao/areas.html",
        areas=areas,
        status_filtro=status,
    )


@bp.route("/areas/nova", methods=["GET", "POST"])
@login_required
@require_permission("funcoes", "create")
def nova_area():
    """Cria uma nova área departamental.

    Form fields:
        nome (str): Obrigatório.
        descricao (str): Opcional.
        cor (int): Índice da paleta 0-7. Vazio = cor automática.
    """
    if request.method == "GET":
        return render_template("organizacao/area_form.html", area=None)

    try:
        a = OrganizacaoService.criar_area(
            nome=request.form.get("nome", ""),
            descricao=request.form.get("descricao") or None,
            cor=request.form.get("cor") or None,
            ator_id=_get_ator_id(),
        )
        flash(f"Área '{a.name}' criada com sucesso.", "sucesso")
        return redirect(url_for("organizacao.listar_areas"))
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template("organizacao/area_form.html", area=None), 400


@bp.route("/areas/<int:area_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("funcoes", "edit")
def editar_area(area_id: int):
    """Edita nome e descrição de uma área.

    Path params:
        area_id (int): PK da área.

    Form fields:
        nome (str): Obrigatório.
        descricao (str): Opcional.
        cor (int): Índice da paleta 0-7. Vazio = cor automática.
    """
    area = db.session.get(Area, area_id)
    if not area:
        flash("Área não encontrada.", "erro")
        return redirect(url_for("organizacao.listar_areas"))

    if request.method == "GET":
        return render_template("organizacao/area_form.html", area=area)

    try:
        a = OrganizacaoService.atualizar_area(
            area_id=area_id,
            nome=request.form.get("nome", ""),
            descricao=request.form.get("descricao") or None,
            cor=request.form.get("cor") or None,
            ator_id=_get_ator_id(),
        )
        flash(f"Área '{a.name}' atualizada.", "sucesso")
        return redirect(url_for("organizacao.listar_areas"))
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template("organizacao/area_form.html", area=area), 400


@bp.route("/areas/<int:area_id>/desativar", methods=["POST"])
@login_required
@require_permission("funcoes", "delete")
def desativar_area(area_id: int):
    """Desativa uma área (is_active=False).

    Path params:
        area_id (int): PK da área.
    """
    try:
        OrganizacaoService.desativar_area(area_id, ator_id=_get_ator_id())
        flash("Área desativada.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")
    return redirect(url_for("organizacao.listar_areas"))


# ---------------------------------------------------------------------------
# Timeline de Equipes (2.1b — equipes dinâmicas)
# ---------------------------------------------------------------------------

_DOW_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def _intervalo_visao(visao: str, ancora: date) -> tuple[date, date]:
    """Calcula [inicio, fim] do intervalo exibido conforme a visão."""
    if visao == "dia":
        return ancora, ancora
    if visao == "mes":
        ultimo = _calendar.monthrange(ancora.year, ancora.month)[1]
        return ancora.replace(day=1), ancora.replace(day=ultimo)
    # semana (padrão): segunda a domingo contendo a âncora
    inicio = ancora - timedelta(days=ancora.weekday())
    return inicio, inicio + timedelta(days=6)


@bp.route("/equipes/timeline")
@login_required
@require_permission("equipes", "view")
def timeline_equipes():
    """Linha do tempo de equipes agendadas (visão Dia / Semana / Mês).

    Query params:
        visao (str):  "dia" | "semana" | "mes". Padrão: "semana".
        data (str):   Âncora ISO (YYYY-MM-DD) dentro do intervalo. Padrão: hoje.
        detalhe (int): PK de EquipeAgendamento para abrir o breakdown
                       (composição membro×dia com disponibilidade).
    """
    visao = request.args.get("visao", "semana")
    if visao not in ("dia", "semana", "mes"):
        visao = "semana"
    try:
        ancora = date.fromisoformat(request.args.get("data", ""))
    except (ValueError, TypeError):
        ancora = date.today()

    inicio, fim = _intervalo_visao(visao, ancora)
    dias = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    agendamentos = OrganizacaoService.listar_agendamentos(inicio, fim)

    # Navegação anterior/próximo
    passo = {"dia": timedelta(days=1), "semana": timedelta(days=7)}.get(visao)
    if passo:
        ancora_ant, ancora_prx = ancora - passo, ancora + passo
    else:  # mês
        ancora_ant = (inicio - timedelta(days=1)).replace(day=1)
        ancora_prx = (fim + timedelta(days=1)).replace(day=1)

    # Breakdown do agendamento clicado (clamp de segurança no limite do service)
    detalhe = None
    detalhe_id = request.args.get("detalhe")
    if detalhe_id and detalhe_id.isdigit():
        ags = {a.id: a for a in agendamentos}
        ag = ags.get(int(detalhe_id))
        if ag:
            fim_detalhe = min(ag.data_fim, ag.data_inicio + timedelta(days=61))
            try:
                detalhe = OrganizacaoService.composicao_no_periodo(
                    ag.equipe_id, ag.data_inicio, fim_detalhe
                )
                detalhe["agendamento"] = ag
            except ValueError as exc:
                flash(str(exc), "erro")

    equipes_ativas = OrganizacaoService.listar_equipes(apenas_ativas=True)

    return render_template(
        "organizacao/timeline.html",
        visao=visao,
        ancora=ancora,
        inicio=inicio,
        fim=fim,
        dias=dias,
        dow_pt=_DOW_PT,
        agendamentos=agendamentos,
        detalhe=detalhe,
        equipes_ativas=equipes_ativas,
        ancora_ant=ancora_ant,
        ancora_prx=ancora_prx,
        hoje=date.today(),
    )


@bp.route("/equipes/agendar", methods=["POST"])
@login_required
@require_permission("equipes", "edit")
def agendar_equipe():
    """Agenda uma equipe para um período.

    Form fields:
        equipe_id (int):   PK da equipe. Obrigatório.
        data_inicio (str): ISO YYYY-MM-DD. Obrigatório.
        data_fim (str):    ISO YYYY-MM-DD. Obrigatório.
        observacao (str):  Opcional.
    """
    equipe_id = request.form.get("equipe_id", "")
    if not equipe_id.isdigit():
        flash("Equipe inválida.", "erro")
        return redirect(url_for("organizacao.timeline_equipes"))

    try:
        di = date.fromisoformat(request.form.get("data_inicio", ""))
        df = date.fromisoformat(request.form.get("data_fim", ""))
    except ValueError:
        flash("Datas inválidas.", "erro")
        return redirect(url_for("organizacao.timeline_equipes"))

    try:
        ag = OrganizacaoService.agendar_equipe(
            int(equipe_id), di, df,
            observacao=request.form.get("observacao") or None,
            ator_id=_get_ator_id(),
        )
        flash(f"Equipe '{ag.equipe.nome}' agendada de "
              f"{di.strftime('%d/%m')} a {df.strftime('%d/%m')}.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")
    return redirect(url_for("organizacao.timeline_equipes", data=di.isoformat()))


@bp.route("/agendamentos/<int:agendamento_id>/cancelar", methods=["POST"])
@login_required
@require_permission("equipes", "edit")
def cancelar_agendamento(agendamento_id: int):
    """Cancela (soft-delete) um agendamento de equipe.

    Path params:
        agendamento_id (int): PK do agendamento.
    """
    data_volta = request.form.get("data") or None
    try:
        OrganizacaoService.cancelar_agendamento(agendamento_id, ator_id=_get_ator_id())
        flash("Agendamento cancelado. Os membros foram avisados.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")
    return redirect(url_for("organizacao.timeline_equipes", data=data_volta))
