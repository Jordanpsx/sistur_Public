# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Blueprint de Relatórios — Central de Relatórios do RH (2.6, Caminho A).

Prefixo: /rh/relatorios
Permissões: relatorios.[view|view_all|imprimir]

Uma tela única: escolher tipo de relatório + filtros + período → preview HTML
→ baixar PDF. Preview e PDF renderizam o mesmo template (mesmo padrão da
Folha de Ponto) — o documento em si é a fonte da verdade visual.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, session, url_for

from app.blueprints.portal.routes import login_required
from app.core.models import Area, AuditAction
from app.core.audit import AuditService
from app.core.permissions import has_permission, require_permission
from app.extensions import db
from app.services.configuracao_service import (
    CHAVE_BRANDING_EMPRESA_NOME,
    CHAVE_EMPRESA_CNPJ,
    CHAVE_EMPRESA_ENDERECO,
    CHAVE_EMPRESA_RAZAO_SOCIAL,
    ConfiguracaoService,
)
from app.services.organizacao_service import OrganizacaoService
from app.services.relatorio_service import RelatorioService

bp = Blueprint("relatorios", __name__)

_TIPOS_VALIDOS = ("funcionarios_ativos", "folgas", "escala", "equipe")


def _get_ator_id() -> int:
    return session["funcionario_id"]


def _pode_tudo() -> bool:
    """True se o ator enxerga relatórios de qualquer área/equipe (view_all)."""
    return has_permission(session["funcionario_id"], "relatorios", "view_all")


def _equipe_no_escopo(equipe_id: int) -> bool:
    """Guard de escopo: view_all sempre pode; senão, só equipes das quais é membro."""
    if _pode_tudo():
        return True
    from app.models.organizacao import EquipeMembro

    return db.session.query(EquipeMembro).filter_by(
        equipe_id=equipe_id, funcionario_id=session["funcionario_id"]
    ).first() is not None


def _area_no_escopo(area_id: int | None) -> bool:
    """Guard de escopo para relatórios por área: view_all ou a própria área."""
    if area_id is None or _pode_tudo():
        return True
    from app.models.funcionario import Funcionario

    ator = db.session.get(Funcionario, session["funcionario_id"])
    return bool(ator and ator.area_id == area_id)


def _parse_date(valor: str | None, default: date) -> date:
    if not valor:
        return default
    try:
        return date.fromisoformat(valor)
    except ValueError:
        return default


def _contexto_form():
    areas = db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()
    equipes = OrganizacaoService.listar_equipes(apenas_ativas=True)
    if not _pode_tudo():
        from app.models.organizacao import EquipeMembro

        minhas = {
            m.equipe_id for m in
            db.session.query(EquipeMembro.equipe_id)
            .filter_by(funcionario_id=session["funcionario_id"]).all()
        }
        equipes = [e for e in equipes if e.id in minhas]
    return {"areas": areas, "equipes": equipes}


def _contexto_branding() -> dict:
    """Variáveis de identidade visual da empresa para o cabeçalho dos PDFs."""
    return {
        "empresa_nome": ConfiguracaoService.get(CHAVE_BRANDING_EMPRESA_NOME) or "",
        "empresa_razao_social": ConfiguracaoService.get(CHAVE_EMPRESA_RAZAO_SOCIAL) or "",
        "empresa_cnpj": ConfiguracaoService.get(CHAVE_EMPRESA_CNPJ) or "",
        "empresa_endereco": ConfiguracaoService.get(CHAVE_EMPRESA_ENDERECO) or "",
    }


@bp.route("/")
@login_required
@require_permission("relatorios", "view")
def central():
    """Central de Relatórios: seletor de tipo + filtros + período.

    Query params:
        tipo (str): Pré-seleciona o tipo de relatório no formulário.
    """
    return render_template(
        "rh/relatorios/index.html",
        tipo=request.args.get("tipo", "funcionarios_ativos"),
        hoje=date.today(),
        **_contexto_form(),
    )


def _montar_relatorio(tipo: str, args) -> tuple[str, dict] | tuple[None, None]:
    """Resolve tipo + query params em (template, contexto). None se inválido/bloqueado.

    Levanta ValueError com mensagem amigável em caso de dados inválidos —
    quem chama trata e faz flash + redirect.
    """
    hoje = date.today()

    if tipo == "funcionarios_ativos":
        area_id = int(args["area_id"]) if args.get("area_id") else None
        if not _area_no_escopo(area_id):
            return None, None
        data = _parse_date(args.get("data"), hoje)
        dados = RelatorioService.funcionarios_ativos_dia(data, area_id=area_id)
        return "rh/relatorios/funcionarios_ativos.html", {"dados": dados}

    if tipo == "folgas":
        area_id = int(args["area_id"]) if args.get("area_id") else None
        if not _area_no_escopo(area_id):
            return None, None
        data_inicio = _parse_date(args.get("data_inicio"), hoje)
        data_fim = _parse_date(args.get("data_fim"), hoje)
        dados = RelatorioService.folgas_periodo(data_inicio, data_fim, area_id=area_id)
        return "rh/relatorios/folgas.html", {"dados": dados}

    if tipo == "escala":
        area_id = int(args["area_id"]) if args.get("area_id") else None
        equipe_id = int(args["equipe_id"]) if args.get("equipe_id") else None
        if equipe_id is not None and not _equipe_no_escopo(equipe_id):
            return None, None
        if equipe_id is None and not _area_no_escopo(area_id):
            return None, None
        data_inicio = _parse_date(args.get("data_inicio"), hoje)
        data_fim = _parse_date(args.get("data_fim"), hoje)
        dados = RelatorioService.escala_periodo(
            data_inicio, data_fim, area_id=area_id, equipe_id=equipe_id
        )
        return "rh/relatorios/escala.html", {"dados": dados}

    if tipo == "equipe":
        if not args.get("equipe_id"):
            raise ValueError("Selecione uma equipe.")
        equipe_id = int(args["equipe_id"])
        if not _equipe_no_escopo(equipe_id):
            return None, None
        data_inicio = _parse_date(args.get("data_inicio"), hoje - timedelta(days=6))
        data_fim = _parse_date(args.get("data_fim"), hoje)
        dados = RelatorioService.relatorio_equipe(equipe_id, data_inicio, data_fim)
        return "rh/relatorios/equipe.html", {"dados": dados}

    return None, None


@bp.route("/preview")
@login_required
@require_permission("relatorios", "view")
def preview():
    """Renderiza o relatório em HTML (mesmo template usado no PDF).

    Query params:
        tipo (str): "funcionarios_ativos" | "folgas" | "escala" | "equipe".
        Demais params variam por tipo — ver `_montar_relatorio`.
    """
    tipo = request.args.get("tipo", "")
    if tipo not in _TIPOS_VALIDOS:
        abort(400)

    try:
        template, contexto = _montar_relatorio(tipo, request.args)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("relatorios.central", tipo=tipo))

    if template is None:
        abort(404)

    return render_template(
        template, gerado_em=None, modo="preview", tipo=tipo, **contexto,
        **_contexto_branding(),
    )


@bp.route("/pdf")
@login_required
@require_permission("relatorios", "imprimir")
def pdf():
    """Gera o PDF do relatório via WeasyPrint (mesmo template do preview).

    Query params: os mesmos de `preview`, mais `tipo` obrigatório.
    """
    from datetime import datetime, timezone

    from weasyprint import HTML

    tipo = request.args.get("tipo", "")
    if tipo not in _TIPOS_VALIDOS:
        abort(400)

    try:
        template, contexto = _montar_relatorio(tipo, request.args)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("relatorios.central", tipo=tipo))

    if template is None:
        abort(404)

    agora = datetime.now(timezone.utc)
    html_str = render_template(
        template, gerado_em=agora, modo="pdf", tipo=tipo, **contexto,
        **_contexto_branding(),
    )
    pdf_bytes = HTML(string=html_str).write_pdf()

    AuditService.log(
        AuditAction.export,
        "relatorios",
        entity_id=None,
        new_state={"tipo": tipo, "filtros": dict(request.args)},
        actor_id=_get_ator_id(),
    )

    filename = f"relatorio_{tipo}_{agora.strftime('%Y%m%d_%H%M')}.pdf"
    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response
