# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint de Escala Mensal de Turnos.

Prefixo: /rh/escala
Permissões: escala.[view|edit]
"""

from __future__ import annotations

import calendar
from datetime import date

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, session, url_for

from app.blueprints.portal.routes import login_required
from app.core.models import Area
from app.core.permissions import require_permission
from app.extensions import db
from app.models.escala import EscalaEntrada
from app.models.organizacao import Equipe
from app.services.escala_service import EscalaService

bp = Blueprint("escala", __name__)


def _ator_id() -> int:
    return session["funcionario_id"]


def _mes_ano_from_args(hoje: date) -> tuple[int, int]:
    try:
        mes = int(request.args.get("mes") or hoje.month)
        ano = int(request.args.get("ano") or hoje.year)
    except (ValueError, TypeError):
        mes, ano = hoje.month, hoje.year
    return mes, ano


def _labels_dias(ano: int, mes: int, dias_no_mes: int) -> dict[int, str]:
    abrev = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    return {d: abrev[date(ano, mes, d).weekday()] for d in range(1, dias_no_mes + 1)}


# ---------------------------------------------------------------------------
# Calendário mensal
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
@require_permission("escala", "view")
def calendario():
    """Exibe o calendário mensal de escalas.

    Query params:
        mes (int): Mês do calendário. Padrão: mês atual.
        ano (int): Ano do calendário. Padrão: ano atual.
        area_id (int): Filtra funcionários por área.
        equipe_id (int): Filtra funcionários por equipe.
    """
    hoje = date.today()
    mes, ano = _mes_ano_from_args(hoje)

    area_id = int(request.args["area_id"]) if request.args.get("area_id") else None
    equipe_id = int(request.args["equipe_id"]) if request.args.get("equipe_id") else None

    calendario = EscalaService.calendario_mes(ano, mes, area_id=area_id, equipe_id=equipe_id)

    # Navegação ← →
    primeiro = date(ano, mes, 1)
    import datetime as _dt
    mes_ant_d = (primeiro - _dt.timedelta(days=1)).replace(day=1)
    mes_prx_d = (primeiro.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)

    areas = db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()
    equipes = db.session.query(Equipe).filter_by(is_active=True).order_by(Equipe.nome).all()

    return render_template(
        "escala/calendario.html",
        calendario=calendario,
        labels_dias=_labels_dias(ano, mes, calendario["dias_no_mes"]),
        hoje=hoje,
        mes=mes,
        ano=ano,
        area_id=area_id,
        equipe_id=equipe_id,
        areas=areas,
        equipes=equipes,
        mes_ant_mes=mes_ant_d.month,
        mes_ant_ano=mes_ant_d.year,
        mes_prx_mes=mes_prx_d.month,
        mes_prx_ano=mes_prx_d.year,
        tipos=list(EscalaEntrada.TIPOS),
        tipo_labels=EscalaEntrada.TIPO_LABELS,
        tipo_cores=EscalaEntrada.TIPO_CORES,
        tipo_texto=EscalaEntrada.TIPO_TEXTO,
        tipo_abbr=EscalaEntrada.TIPO_ABBR,
        label_opcoes=list(EscalaEntrada.LABELS),
        label_labels=EscalaEntrada.LABEL_LABELS,
        label_abbr=EscalaEntrada.LABEL_ABBR,
        label_cores=EscalaEntrada.LABEL_CORES,
        label_texto=EscalaEntrada.LABEL_TEXTO,
    )


# ---------------------------------------------------------------------------
# Registrar / atualizar entrada (AJAX JSON)
# ---------------------------------------------------------------------------

@bp.route("/entrada", methods=["POST"])
@login_required
@require_permission("escala", "edit")
def registrar_entrada():
    """Registra ou atualiza uma entrada de escala via AJAX.

    JSON body:
        funcionario_id (int): Obrigatório.
        data (str): ISO date (YYYY-MM-DD). Obrigatório.
        tipo (str): NORMAL | FOLGA | FALTA_JUSTIFICADA. Obrigatório.
        labels (list[str]): Rótulos opcionais (MANHA/TARDE/NOITE/PLANTAO).
        equipe_id (int): Opcional.
        justificativa (str): Obrigatória quando tipo == FALTA_JUSTIFICADA;
            opcional nos demais. Pode conter URLs — a UI as reconhece como
            links, mas o texto sempre chega e é tratado como não-confiável.

    Returns:
        JSON {"ok": true, "id": int, "tipo": str, "labels": list} ou
        {"ok": false, "erro": str}.
    """
    dados = request.get_json(silent=True) or {}
    try:
        fid = int(dados["funcionario_id"])
        data_str = dados["data"]
        ano, mes_d, dia = (int(x) for x in data_str.split("-"))
        data = date(ano, mes_d, dia)
        tipo = dados["tipo"]
        labels = dados.get("labels") or []
        if not isinstance(labels, list):
            raise ValueError("labels deve ser uma lista.")
        equipe_id = int(dados["equipe_id"]) if dados.get("equipe_id") else None
        justificativa = (dados.get("justificativa") or "").strip() or None

        entrada = EscalaService.registrar(fid, data, tipo, labels, equipe_id, justificativa, _ator_id())
        return jsonify({"ok": True, "id": entrada.id, "tipo": entrada.tipo, "labels": entrada.labels or []})
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 400


# ---------------------------------------------------------------------------
# Remover entrada (AJAX JSON)
# ---------------------------------------------------------------------------

@bp.route("/entrada/remover", methods=["POST"])
@login_required
@require_permission("escala", "edit")
def remover_entrada():
    """Remove uma entrada de escala via AJAX.

    JSON body:
        funcionario_id (int): Obrigatório.
        data (str): ISO date (YYYY-MM-DD). Obrigatório.

    Returns:
        JSON {"ok": true} ou {"ok": false, "erro": str}.
    """
    dados = request.get_json(silent=True) or {}
    try:
        fid = int(dados["funcionario_id"])
        ano, mes_d, dia = (int(x) for x in dados["data"].split("-"))
        data = date(ano, mes_d, dia)
        EscalaService.remover(fid, data, _ator_id())
        return jsonify({"ok": True})
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 400


# ---------------------------------------------------------------------------
# Lote de entradas (form submit)
# ---------------------------------------------------------------------------

@bp.route("/lote", methods=["POST"])
@login_required
@require_permission("escala", "edit")
def registrar_lote():
    """Aplica um estado a múltiplos funcionários num intervalo de datas.

    Form fields:
        funcionario_ids (list[int]): Um ou mais IDs de funcionários.
        data_inicio (str): Data inicial (YYYY-MM-DD).
        data_fim (str): Data final (YYYY-MM-DD, inclusive).
        tipo (str): Estado base — NORMAL | FOLGA | FALTA_JUSTIFICADA.
        labels (list[str]): Rótulos opcionais de turno.
        equipe_id (int): Opcional.
        justificativa (str): Obrigatória quando tipo == FALTA_JUSTIFICADA —
            aplicada a todas as combinações funcionário×data do lote.
        mes (int): Para redirecionar de volta ao mês certo.
        ano (int): Idem.
    """
    try:
        fids = [int(x) for x in request.form.getlist("funcionario_ids") if x.isdigit()]
        if not fids:
            raise ValueError("Selecione ao menos um funcionário.")

        inicio = date.fromisoformat(request.form["data_inicio"])
        fim = date.fromisoformat(request.form["data_fim"])
        if fim < inicio:
            raise ValueError("Data final deve ser igual ou posterior à inicial.")

        import datetime as _dt
        datas = []
        d = inicio
        while d <= fim:
            datas.append(d)
            d += _dt.timedelta(days=1)

        tipo = request.form["tipo"]
        labels = request.form.getlist("labels")
        equipe_id = int(request.form["equipe_id"]) if request.form.get("equipe_id") else None
        justificativa = (request.form.get("justificativa") or "").strip() or None

        count = EscalaService.registrar_lote(fids, datas, tipo, labels, equipe_id, justificativa, _ator_id())
        flash(f"{count} entrada(s) de escala registrada(s).", "sucesso")
    except (KeyError, ValueError) as exc:
        flash(str(exc), "erro")

    mes = request.form.get("mes") or date.today().month
    ano = request.form.get("ano") or date.today().year
    return redirect(url_for("escala.calendario", mes=mes, ano=ano))


# ---------------------------------------------------------------------------
# Custo operacional com diaristas (2.5)
# ---------------------------------------------------------------------------

@bp.route("/custo")
@login_required
@require_permission("escala", "view")
def custo_diaristas():
    """Relatório de custo operacional com diaristas no mês.

    Query params:
        mes (int): Mês. Padrão: mês atual.
        ano (int): Ano. Padrão: ano atual.
        area_id (int): Filtra por área.
    """
    import calendar as _cal
    from app.services.custo_operacional_service import CustoOperacionalService

    hoje = date.today()
    try:
        mes = int(request.args.get("mes") or hoje.month)
        ano = int(request.args.get("ano") or hoje.year)
    except (ValueError, TypeError):
        mes, ano = hoje.month, hoje.year

    area_id = int(request.args["area_id"]) if request.args.get("area_id") else None

    relatorio = CustoOperacionalService.custo_diaristas_mes(ano, mes, area_id=area_id)
    areas = db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()

    import datetime as _dt
    primeiro = date(ano, mes, 1)
    mes_ant_d = (primeiro - _dt.timedelta(days=1)).replace(day=1)
    mes_prx_d = (primeiro.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)

    nomes_meses = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
                   'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']

    return render_template(
        "escala/custo.html",
        relatorio=relatorio,
        areas=areas,
        area_id=area_id,
        mes=mes,
        ano=ano,
        nome_mes=nomes_meses[mes - 1],
        mes_ant_mes=mes_ant_d.month,
        mes_ant_ano=mes_ant_d.year,
        mes_prx_mes=mes_prx_d.month,
        mes_prx_ano=mes_prx_d.year,
    )


# ---------------------------------------------------------------------------
# Gerar escala a partir do padrão de jornada (form submit)
# ---------------------------------------------------------------------------

@bp.route("/gerar", methods=["POST"])
@login_required
@require_permission("escala", "edit")
def gerar_do_padrao():
    """Gera a escala do mês a partir da jornada semanal dos funcionários.

    Para cada CLT ativo com jornada definida: dias ativos viram NORMAL (com
    rótulo derivado do horário de entrada) e dias inativos viram FOLGA.

    Form fields:
        mes (int), ano (int): Mês alvo.
        area_id (int): Opcional — restringe à área.
        equipe_id (int): Opcional — restringe à equipe.
        sobrescrever (str): Se presente, substitui entradas já existentes.
    """
    hoje = date.today()
    try:
        mes = int(request.form.get("mes") or hoje.month)
        ano = int(request.form.get("ano") or hoje.year)
    except (ValueError, TypeError):
        mes, ano = hoje.month, hoje.year

    area_id = int(request.form["area_id"]) if request.form.get("area_id") else None
    equipe_id = int(request.form["equipe_id"]) if request.form.get("equipe_id") else None
    sobrescrever = request.form.get("sobrescrever") is not None

    resultado = EscalaService.gerar_a_partir_do_padrao(
        ano, mes, area_id=area_id, equipe_id=equipe_id,
        sobrescrever=sobrescrever, ator_id=_ator_id(),
    )

    partes = [f"{resultado['criados']} dia(s) escalado(s)"]
    if resultado["atualizados"]:
        partes.append(f"{resultado['atualizados']} atualizado(s)")
    if resultado["pulados"]:
        partes.append(f"{resultado['pulados']} mantido(s)")
    flash("Escala gerada do padrão: " + ", ".join(partes) + ".", "sucesso")

    if resultado["sem_padrao"]:
        nomes = ", ".join(f.nome for f in resultado["sem_padrao"][:5])
        extra = "…" if len(resultado["sem_padrao"]) > 5 else ""
        flash(
            f"{len(resultado['sem_padrao'])} funcionário(s) sem jornada cadastrada "
            f"não foram escalados: {nomes}{extra}.",
            "aviso",
        )

    return redirect(url_for("escala.calendario", mes=mes, ano=ano,
                            area_id=area_id or "", equipe_id=equipe_id or ""))
