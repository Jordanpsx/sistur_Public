# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
RH Blueprint — Gestão de Funcionários.

Rotas
-----
GET  /rh/                               → dashboard com abas (Visão Geral + Colaboradores)
GET  /rh/funcionarios                   → lista de funcionários (com filtros avançados)
GET  /rh/funcionarios/novo              → formulário de cadastro
POST /rh/funcionarios/novo              → salva novo funcionário
GET  /rh/funcionarios/<id>/editar       → formulário de edição pré-preenchido
POST /rh/funcionarios/<id>/editar       → salva edição
POST /rh/funcionarios/<id>/desativar    → desativa funcionário (soft-delete)
GET  /rh/funcionarios/<id>/banco-horas  → banco de horas detalhado do funcionário (visão RH)

Todas as rotas exigem sessão ativa (@login_required) e a permissão
correspondente no role do funcionário logado (@require_permission).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, session, url_for

from app.blueprints.portal.routes import login_required
from app.core.models import Area
from app.core.permissions import has_permission, require_permission
from app.extensions import db
from app.models.funcionario import Funcionario
from app.services.configuracao_service import (
    ConfiguracaoService, CHAVE_BRANDING_EMPRESA_NOME, CHAVE_EMPRESA_RAZAO_SOCIAL, CHAVE_EMPRESA_CNPJ, CHAVE_EMPRESA_ENDERECO,
)
from app.services.escala_service import EscalaService
from app.services.funcionario_service import FuncionarioService
from app.services.organizacao_service import OrganizacaoService
from app.services.qr_service import QRService
from app.services.rh_service import RHService
from app.services.role_service import RoleService

bp = Blueprint("rh", __name__)


# ---------------------------------------------------------------------------
# Dashboard Principal (abas)
# ---------------------------------------------------------------------------

@bp.route("/", methods=["GET"])
@login_required
@require_permission("funcionarios", "view")
def dashboard():
    """Dashboard RH com duas abas: Visão Geral e Colaboradores.

    Aba 1 (visao-geral): resumo mensal agregado via func.sum() e central de
    alertas com dias marcados como needs_review nos últimos 7 dias.

    Aba 2 (colaboradores): listagem filtrada por nome/CPF, área e status
    (ativo/inativo/todos), com saldo do banco de horas em destaque.

    Query params:
        aba (str): "visao-geral" | "colaboradores". Padrão: "visao-geral".
        q (str): Busca livre por nome ou CPF (Aba 2).
        area_id (int): Filtro de área (Aba 2).
        status (str): "ativo" | "inativo" | "todos" (Aba 2). Padrão: "ativo".

    Returns:
        Renderização de rh/index.html com todos os dados de ambas as abas.
    """
    hoje = date.today()

    # Navegação de mês (padrão: mês atual)
    try:
        mes = int(request.args.get("mes") or hoje.month)
        ano = int(request.args.get("ano") or hoje.year)
    except (ValueError, TypeError):
        mes, ano = hoje.month, hoje.year

    # --- Aba 1: Visão Geral ---
    try:
        pag_matriz = max(1, int(request.args.get("pag_matriz") or 1))
    except (ValueError, TypeError):
        pag_matriz = 1

    resumo = RHService.resumo_mensal(ano, mes)
    alertas = RHService.alertas_revisao(7)
    folgas_hoje = RHService.funcionarios_de_folga(hoje)
    presenca_hoje = EscalaService.presenca_dia(hoje)
    equipes_hoje = OrganizacaoService.equipes_do_dia(hoje, folgas=folgas_hoje)
    matriz = RHService.matriz_batidas_mes(ano, mes, page=pag_matriz)

    # Meses adjacentes para navegação ← →
    primeiro_do_mes = date(ano, mes, 1)
    mes_ant = (primeiro_do_mes - timedelta(days=1)).replace(day=1)
    mes_prx = (primeiro_do_mes.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Rótulos abreviados de dia da semana para cada dia do mês (1-indexed)
    _abrev = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    dias_semana_labels = {
        d: _abrev[date(ano, mes, d).weekday()]
        for d in range(1, matriz["dias_no_mes"] + 1)
    }

    # --- Aba 2: Colaboradores ---
    q = (request.args.get("q") or "").strip()
    area_id = int(request.args["area_id"]) if request.args.get("area_id") else None
    status = request.args.get("status", "ativo")
    aba = request.args.get("aba", "visao-geral")
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError):
        page = 1

    funcionarios = RHService.listar_com_filtros(
        q=q, area_id=area_id, status=status, page=page,
        escopo_area_id=_escopo_area_listagem(),
    )
    areas = db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()

    # Integridade do banco de horas (2.9c) — só para quem opera folha de ponto,
    # que é quem pode acionar o recálculo. Divergência é sinal de dado financeiro
    # inconsistente, não informação de dashboard geral.
    divergencias_banco = (
        RHService.divergencias_banco_horas()
        if has_permission(session["funcionario_id"], "folha_ponto", "view")
        else []
    )

    return render_template(
        "rh/index.html",
        resumo=resumo,
        alertas=alertas,
        divergencias_banco=divergencias_banco,
        folgas_hoje=folgas_hoje,
        presenca_hoje=presenca_hoje,
        equipes_hoje=equipes_hoje,
        hoje=hoje,
        matriz=matriz,
        dias_semana_labels=dias_semana_labels,
        funcionarios=funcionarios,
        areas=areas,
        q=q,
        area_id=area_id,
        status=status,
        aba=aba,
        mes=mes,
        ano=ano,
        page=page,
        pag_matriz=pag_matriz,
        mes_ant_mes=mes_ant.month,
        mes_ant_ano=mes_ant.year,
        mes_prx_mes=mes_prx.month,
        mes_prx_ano=mes_prx.year,
    )


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _get_ator_id() -> int:
    """Retorna o ID do funcionário logado a partir da sessão Flask."""
    return session["funcionario_id"]


def _escopo_area_listagem() -> int | None:
    """Restrição de área na listagem de funcionários (D17).

    Espelha o padrão do audit viewer: super admin e quem tem
    `funcionarios.view_all` enxergam todas as áreas (None); qualquer outro
    supervisor com `funcionarios.view` fica restrito à própria área.

    Returns:
        area_id do ator para restringir a query, ou None se sem restrição.
    """
    fid = session.get("funcionario_id")
    if not fid or has_permission(fid, "funcionarios", "view_all"):
        return None
    ator = db.session.get(Funcionario, fid)
    return ator.area_id if ator else None


def _popular_selects() -> tuple[list[Area], list]:
    """
    Retorna as listas necessárias para popular os selects do formulário.

    Returns:
        Tupla (areas, roles) com registros ativos de cada entidade.
    """
    areas = db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()
    roles = RoleService.listar(apenas_ativos=True)
    return areas, roles


_DIAS_SEMANA = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")


def _parse_jornada(form) -> dict:
    """
    Constrói o dicionário jornada_semanal a partir dos campos do formulário.

    Para cada dia da semana lê três inputs:
        jornada_<dia>_ativo    (checkbox — presente se marcado)
        jornada_<dia>_minutos  (number)
        jornada_<dia>_almoco   (number)

    Returns:
        Dict com estrutura {"segunda": {"ativo": bool, "minutos": int, "almoco": int}, ...}
    """
    jornada = {}
    for dia in _DIAS_SEMANA:
        ativo = form.get(f"jornada_{dia}_ativo") is not None
        minutos = int(form.get(f"jornada_{dia}_minutos") or 0) if ativo else 0
        almoco = int(form.get(f"jornada_{dia}_almoco") or 0) if ativo else 0
        jornada[dia] = {"ativo": ativo, "minutos": minutos, "almoco": almoco}
    return jornada


def _aplicar_senha(funcionario: Funcionario, form, ator_id: int) -> None:
    """
    Aplica nova senha ao funcionário se o campo 'nova_senha' estiver preenchido.

    Valida se nova_senha == confirmar_senha antes de persistir.
    Levanta ValueError se as senhas não coincidem ou são muito curtas.

    Args:
        funcionario: Instância já persistida do Funcionario.
        form:        ImmutableMultiDict do request.form.
        ator_id:     ID do ator que realiza a ação.

    Raises:
        ValueError: Se as senhas não coincidem ou têm menos de 6 caracteres.
    """
    nova = (form.get("nova_senha") or "").strip()
    confirmar = (form.get("confirmar_senha") or "").strip()

    if not nova:
        return  # Campo vazio → mantém senha atual

    if len(nova) < 6:
        raise ValueError("A senha deve ter pelo menos 6 caracteres.")
    if nova != confirmar:
        raise ValueError("As senhas não coincidem.")

    FuncionarioService.definir_senha(funcionario.id, nova, ator_id)


def _aplicar_role(funcionario: Funcionario, role_id_str: str | None, ator_id: int) -> None:
    """
    Aplica ou remove o role de um funcionário conforme o valor recebido do formulário.

    Se role_id_str for vazio/None e o funcionário já tiver um role, remove.
    Se role_id_str for preenchido e diferente do atual, atribui o novo role.
    Caso contrário, não faz nada (evita logs desnecessários).

    Args:
        funcionario:  Instância do Funcionario já persistida.
        role_id_str:  Valor bruto do campo 'role_id' do formulário.
        ator_id:      ID do funcionário que está realizando a ação.
    """
    role_id_novo = int(role_id_str) if role_id_str else None

    if role_id_novo and role_id_novo != funcionario.role_id:
        RoleService.atribuir_ao_funcionario(funcionario.id, role_id_novo, ator_id)
    elif not role_id_novo and funcionario.role_id:
        RoleService.remover_do_funcionario(funcionario.id, ator_id)


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------

@bp.route("/funcionarios", methods=["GET"])
@login_required
@require_permission("funcionarios", "view")
def listar_funcionarios():
    """Exibe a tabela de funcionários com filtros por nome/CPF, área e status.

    Query params:
        q (str): Termo de busca livre filtrado em nome e CPF.
        area_id (int): Filtra por área específica.
        status (str): "ativo" | "inativo" | "todos". Padrão: "ativo".

    Returns:
        Renderização de rh/funcionarios/list.html com a lista filtrada.
    """
    q = (request.args.get("q") or "").strip()
    area_id = int(request.args["area_id"]) if request.args.get("area_id") else None
    status = request.args.get("status", "ativo")
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError):
        page = 1

    funcionarios = RHService.listar_com_filtros(
        q=q, area_id=area_id, status=status, page=page,
        escopo_area_id=_escopo_area_listagem(),
    )
    areas = db.session.query(Area).filter_by(is_active=True).order_by(Area.name).all()

    return render_template(
        "rh/funcionarios/list.html",
        funcionarios=funcionarios,
        areas=areas,
        q=q,
        area_id=area_id,
        status=status,
        page=page,
    )


# ---------------------------------------------------------------------------
# Criar
# ---------------------------------------------------------------------------

@bp.route("/funcionarios/novo", methods=["GET", "POST"])
@login_required
@require_permission("funcionarios", "create")
def novo_funcionario():
    """
    GET  → exibe o formulário em branco para cadastro de novo funcionário.
    POST → valida e persiste o novo funcionário, redireciona para a lista.

    Form fields:
        nome (str): Nome completo — obrigatório.
        cpf (str): CPF em qualquer formato — obrigatório, validado pelo algoritmo.
        email, telefone, cargo, matricula, data_admissao (str): Opcionais.
        ctps, ctps_uf, cbo (str): Documentação trabalhista — opcionais.
        area_id (int): FK para sistur_areas — opcional.
        role_id (int): FK para sistur_roles — opcional, tratado via RoleService.
        minutos_esperados_dia (int): Padrão 480.
        minutos_almoco (int): Padrão 60.

    Returns:
        GET:  Renderização de rh/funcionarios/form.html.
        POST (sucesso): Redirect para rh.listar_funcionarios com flash de sucesso.
        POST (erro):    Renderização do formulário com HTTP 400 e flash de erro.
    """
    areas, roles = _popular_selects()

    if request.method == "GET":
        return render_template(
            "rh/funcionarios/form.html",
            funcionario=None,
            areas=areas,
            roles=roles,
        )

    # --- POST ---
    ator_id = _get_ator_id()

    try:
        _vd_raw = (request.form.get("valor_diaria") or "").strip()
        _valor_diaria = float(_vd_raw.replace(",", ".")) if _vd_raw else None

        f = FuncionarioService.criar(
            nome=request.form.get("nome", ""),
            cpf=request.form.get("cpf", ""),
            cargo=request.form.get("cargo") or None,
            matricula=request.form.get("matricula") or None,
            area_id=int(request.form["area_id"]) if request.form.get("area_id") else None,
            minutos_esperados_dia=int(request.form.get("minutos_esperados_dia") or 480),
            minutos_almoco=int(request.form.get("minutos_almoco") or 60),
            tipo_contrato=request.form.get("tipo_contrato") or "CLT",
            valor_diaria=_valor_diaria,
            ator_id=ator_id,
        )
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template(
            "rh/funcionarios/form.html",
            funcionario=None,
            areas=areas,
            roles=roles,
        ), 400

    # Campos adicionais + jornada semanal
    dados_extras: dict = {}
    for campo in ("email", "telefone", "data_admissao", "ctps", "ctps_uf", "cbo"):
        valor = request.form.get(campo) or None
        if valor:
            dados_extras[campo] = valor
    dados_extras["jornada_semanal"] = _parse_jornada(request.form)

    # Horário de entrada padrão para monitoramento de atrasos (campo time HTML → datetime.time)
    _hep = (request.form.get("horario_entrada_padrao") or "").strip()
    if _hep:
        from datetime import time as _dtime
        try:
            _h, _m = _hep.split(":")
            dados_extras["horario_entrada_padrao"] = _dtime(int(_h), int(_m))
        except (ValueError, AttributeError):
            dados_extras["horario_entrada_padrao"] = None
    else:
        dados_extras["horario_entrada_padrao"] = None

    FuncionarioService.atualizar(f.id, dados_extras, ator_id)

    # Senha (opcional)
    try:
        _aplicar_senha(f, request.form, ator_id)
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template(
            "rh/funcionarios/form.html",
            funcionario=f,
            areas=areas,
            roles=roles,
        ), 400

    # Aplicar role separadamente (auditado como permission_change)
    _aplicar_role(f, request.form.get("role_id"), ator_id)

    flash(f"Funcionário {f.nome} cadastrado com sucesso.", "sucesso")
    return redirect(url_for("rh.listar_funcionarios"))


# ---------------------------------------------------------------------------
# Editar
# ---------------------------------------------------------------------------

@bp.route("/funcionarios/<int:funcionario_id>/editar", methods=["GET", "POST"])
@login_required
@require_permission("funcionarios", "edit")
def editar_funcionario(funcionario_id: int):
    """
    GET  → exibe o formulário pré-preenchido com os dados atuais do funcionário.
    POST → aplica as alterações e redireciona para a lista.

    Path params:
        funcionario_id (int): PK do funcionário a editar.

    Returns:
        GET:  Renderização de rh/funcionarios/form.html com dados preenchidos.
        POST (sucesso): Redirect para rh.listar_funcionarios com flash de sucesso.
        POST (erro):    Renderização do formulário com HTTP 400 e flash de erro.
    """
    f = db.session.get(Funcionario, funcionario_id)
    if not f or not f.ativo:
        flash("Funcionário não encontrado.", "erro")
        return redirect(url_for("rh.listar_funcionarios"))

    areas, roles = _popular_selects()

    if request.method == "GET":
        return render_template(
            "rh/funcionarios/form.html",
            funcionario=f,
            areas=areas,
            roles=roles,
        )

    # --- POST ---
    ator_id = _get_ator_id()

    dados = {
        campo: request.form.get(campo) or None
        for campo in (
            "nome", "cargo", "matricula", "email", "telefone",
            "data_admissao", "ctps", "ctps_uf", "cbo",
        )
    }
    # Campos numéricos
    if request.form.get("area_id"):
        dados["area_id"] = int(request.form["area_id"])
    else:
        dados["area_id"] = None

    for campo in ("minutos_esperados_dia", "minutos_almoco"):
        if request.form.get(campo):
            dados[campo] = int(request.form[campo])

    dados["jornada_semanal"] = _parse_jornada(request.form)

    # Tipo de contrato e valor de diária (2.2)
    dados["tipo_contrato"] = request.form.get("tipo_contrato") or "CLT"
    _vd_raw = (request.form.get("valor_diaria") or "").strip()
    dados["valor_diaria"] = float(_vd_raw.replace(",", ".")) if _vd_raw else None

    # Horário de entrada padrão para monitoramento de atrasos (campo time HTML → datetime.time)
    _hep = (request.form.get("horario_entrada_padrao") or "").strip()
    if _hep:
        from datetime import time as _dtime
        try:
            _h, _m = _hep.split(":")
            dados["horario_entrada_padrao"] = _dtime(int(_h), int(_m))
        except (ValueError, AttributeError):
            dados["horario_entrada_padrao"] = None
    else:
        dados["horario_entrada_padrao"] = None

    try:
        f = FuncionarioService.atualizar(funcionario_id, dados, ator_id)
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template(
            "rh/funcionarios/form.html",
            funcionario=f,
            areas=areas,
            roles=roles,
        ), 400

    # Senha (opcional)
    try:
        _aplicar_senha(f, request.form, ator_id)
    except ValueError as exc:
        flash(str(exc), "erro")
        return render_template(
            "rh/funcionarios/form.html",
            funcionario=f,
            areas=areas,
            roles=roles,
        ), 400

    _aplicar_role(f, request.form.get("role_id"), ator_id)

    flash(f"Dados de {f.nome} atualizados com sucesso.", "sucesso")
    return redirect(url_for("rh.listar_funcionarios"))


# ---------------------------------------------------------------------------
# Desativar
# ---------------------------------------------------------------------------

@bp.route("/funcionarios/<int:funcionario_id>/desativar", methods=["POST"])
@login_required
@require_permission("funcionarios", "desativar")
def desativar_funcionario(funcionario_id: int):
    """
    Desativa o funcionário (soft-delete: ativo=False) e redireciona para a lista.

    A ação é auditada automaticamente por FuncionarioService.desativar.
    Após desativação, o funcionário não aparece mais na lista nem consegue
    fazer login no portal.

    Path params:
        funcionario_id (int): PK do funcionário a desativar.

    Returns:
        Redirect para rh.listar_funcionarios com flash de confirmação ou erro.
    """
    ator_id = _get_ator_id()

    try:
        f = FuncionarioService.desativar(funcionario_id, ator_id)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("rh.listar_funcionarios"))

    flash(f"Funcionário {f.nome} desativado.", "aviso")
    return redirect(url_for("rh.listar_funcionarios"))


@bp.route("/funcionarios/<int:funcionario_id>/reativar", methods=["POST"])
@login_required
@require_permission("funcionarios", "desativar")
def reativar_funcionario(funcionario_id: int):
    """Reverte o soft-delete de um funcionário (ativo=True).

    Usa a mesma permissão de 'desativar' pois governa o ciclo ativo/inativo.
    A ação é auditada automaticamente por FuncionarioService.reativar.

    Path params:
        funcionario_id (int): PK do funcionário a reativar.

    Returns:
        Redirect para rh.listar_funcionarios com flash de confirmação ou erro.
    """
    ator_id = _get_ator_id()

    try:
        f = FuncionarioService.reativar(funcionario_id, ator_id)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("rh.listar_funcionarios", status="inativo"))

    flash(f"Funcionário {f.nome} reativado.", "sucesso")
    return redirect(url_for("rh.listar_funcionarios", status="inativo"))


# ---------------------------------------------------------------------------
# Reprocessamento em Lote (Ponto Eletrônico)
# ---------------------------------------------------------------------------

@bp.route("/funcionarios/<int:funcionario_id>/reprocessar_ponto", methods=["POST"])
@login_required
@require_permission("funcionarios", "edit")
def reprocessar_ponto(funcionario_id: int):
    """
    Aciona o reprocessamento em lote do saldo de horas para o funcionário dado.

    Form fields:
        data_inicio (str): ISO Format (YYYY-MM-DD).
        data_fim (str): ISO Format (YYYY-MM-DD).
        sobrescrever_jornada (bool opcional): Se presente, atualiza o snapshot do
                                              passado com os dados do contrato atual.

    Returns:
        Redirect para a listagem ou perfil com flash message.
    """
    from datetime import datetime
    from app.services.ponto_service import PontoService

    ator_id = _get_ator_id()
    
    data_inicio_str = request.form.get("data_inicio")
    data_fim_str = request.form.get("data_fim")
    sobrescrever = request.form.get("sobrescrever_jornada") is not None

    if not data_inicio_str or not data_fim_str:
        flash("Ambas as datas de início e fim são obrigatórias.", "erro")
        return redirect(url_for("rh.listar_funcionarios"))

    try:
        data_inicio = datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
        data_fim = datetime.strptime(data_fim_str, "%Y-%m-%d").date()
    except ValueError:
        flash("Formato de data inválido.", "erro")
        return redirect(url_for("rh.listar_funcionarios"))

    try:
        resultado = PontoService.reprocess_interval(
            funcionario_id=funcionario_id,
            start_date=data_inicio,
            end_date=data_fim,
            ator_id=ator_id,
            override_snapshots=sobrescrever
        )
        
        dias = resultado["dias_processados"]
        if dias > 0:
            flash(f"Reprocessamento concluído! {dias} dia(s) atualizado(s) com sucesso.", "sucesso")
        else:
            flash(f"Nenhum registro de ponto encontrado para reprocessar neste período.", "aviso")
            
    except Exception as exc:
        flash(f"Erro ao reprocessar ponto: {str(exc)}", "erro")

    # Idealmente poderia redirecionar para a página de edição do funcionário, mantemos na listagem por padrão
    return redirect(url_for("rh.editar_funcionario", funcionario_id=funcionario_id))


@bp.route("/funcionarios/<int:funcionario_id>/recalcular-banco", methods=["POST"])
@login_required
@require_permission("ponto", "edit_admin")
def recalcular_banco_horas(funcionario_id: int):
    """Refaz o `banco_horas_acumulado` do zero, corrigindo divergência (2.9c).

    Contrapartida do detector `RHService.divergencias_banco_horas`: o cache é
    mantido por delta e pode divergir da soma real; aqui ele é reconstruído a
    partir dos TimeDay menos os abatimentos. Auditado pelo PontoService.

    Args:
        funcionario_id: PK do funcionário a reconciliar.

    Returns:
        Redirect para o dashboard de RH com mensagem de resultado.
    """
    from app.services.ponto_service import PontoService

    try:
        anterior = int(
            (db.session.get(Funcionario, funcionario_id) or Funcionario()).banco_horas_acumulado or 0
        )
        novo = PontoService.recalcular_banco_global(funcionario_id, ator_id=_get_ator_id())
        if novo == anterior:
            flash("Banco de horas já estava correto — nenhum ajuste necessário.", "aviso")
        else:
            flash(
                f"Banco de horas recalculado: {anterior} min → {novo} min "
                f"({novo - anterior:+d} min).",
                "sucesso",
            )
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("rh.dashboard") + "#integridade-banco")


# ---------------------------------------------------------------------------
# Banco de Horas Detalhado — visão do RH para qualquer funcionário
# ---------------------------------------------------------------------------

@bp.route("/funcionarios/<int:funcionario_id>/banco-horas", methods=["GET"])
@login_required
@require_permission("funcionarios", "view")
def banco_horas_funcionario(funcionario_id: int):
    """Exibe o banco de horas detalhado de um funcionário específico para o RH.

    Reutiliza o template ponto/analise.html com visão administrativa:
    o gestor pode ver o histórico de qualquer colaborador ativo, com gráficos,
    estatísticas e tabela diária no layout CLT. A navegação de meses e o
    botão "Voltar" apontam para o contexto do RH (não do portal do colaborador).

    Path params:
        funcionario_id (int): PK do funcionário a consultar.

    Query params:
        mes (int): mês a exibir (1–12). Padrão: mês corrente.
        ano (int): ano a exibir. Padrão: ano corrente.

    Returns:
        Renderização de ponto/analise.html com dados do funcionário alvo.
        HTTP 404 se o funcionário não for encontrado.
    """
    from app.models.ponto import TimeDay, TimeEntry

    funcionario = db.session.get(Funcionario, funcionario_id)
    if not funcionario:
        flash("Funcionário não encontrado.", "erro")
        return redirect(url_for("rh.dashboard", aba="colaboradores"))

    hoje = date.today()
    try:
        mes = int(request.args.get("mes") or hoje.month)
        ano = int(request.args.get("ano") or hoje.year)
    except (TypeError, ValueError):
        mes, ano = hoje.month, hoje.year

    inicio = date(ano, mes, 1)
    fim = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

    days = (
        db.session.query(TimeDay)
        .filter(
            TimeDay.funcionario_id == funcionario_id,
            TimeDay.shift_date >= inicio,
            TimeDay.shift_date < fim,
        )
        .order_by(TimeDay.shift_date)
        .all()
    )

    entries_raw = (
        db.session.query(TimeEntry)
        .filter(
            TimeEntry.funcionario_id == funcionario_id,
            TimeEntry.shift_date >= inicio,
            TimeEntry.shift_date < fim,
        )
        .order_by(TimeEntry.shift_date, TimeEntry.punch_time)
        .all()
    )
    entries_by_day: dict[date, list] = defaultdict(list)
    for e in entries_raw:
        entries_by_day[e.shift_date].append(e)

    DOW_ABBR = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    accumulated = 0
    chart_labels, chart_worked, chart_balance, chart_expected = [], [], [], []
    for d in days:
        dow = DOW_ABBR[d.shift_date.weekday()]
        chart_labels.append(f"{dow} {d.shift_date.strftime('%d/%m')}")
        chart_worked.append(round(d.minutos_trabalhados / 60, 2))
        chart_expected.append(round(d.expected_minutes_snapshot / 60, 2))
        accumulated += d.saldo_calculado_minutos
        chart_balance.append(round(accumulated / 60, 2))

    total_min = sum(d.minutos_trabalhados for d in days)
    total_saldo = sum(d.saldo_calculado_minutos for d in days)
    dias_ok = sum(1 for d in days if not d.needs_review)
    dias_revisar = sum(1 for d in days if d.needs_review)
    maior_credito = max((d.saldo_calculado_minutos for d in days), default=0)
    maior_debito = min((d.saldo_calculado_minutos for d in days), default=0)

    TYPE_ORDER = ["clock_in", "lunch_start", "lunch_end", "clock_out"]
    tabela_rows = []
    for d in days:
        entries = entries_by_day.get(d.shift_date, [])
        by_type: dict[str, str] = {}
        for e in entries:
            key = e.punch_type.value
            if key not in by_type:
                by_type[key] = e.punch_time.strftime("%H:%M")
        tabela_rows.append({
            "shift_date": d.shift_date,
            "dow": DOW_ABBR[d.shift_date.weekday()],
            "clock_in":    by_type.get("clock_in", "—"),
            "lunch_start": by_type.get("lunch_start", "—"),
            "lunch_end":   by_type.get("lunch_end", "—"),
            "clock_out":   by_type.get("clock_out", "—"),
            "extras":      [e.punch_time.strftime("%H:%M") for e in entries
                            if e.punch_type.value not in TYPE_ORDER],
            "minutos_trabalhados": d.minutos_trabalhados,
            "saldo_calculado":     d.saldo_calculado_minutos,
            "needs_review":        d.needs_review,
            "is_today":            d.shift_date == hoje,
        })

    # Mesmo contexto do portal do colaborador — o template é compartilhado, e sem
    # estas duas chaves o bloco de abatimentos sumiria só na visão do RH.
    from app.services.ponto_service import PontoService
    abatimentos = PontoService.listar_abatimentos(funcionario_id, inicio=inicio, fim=fim)
    total_abatido = sum(a.minutos_abatidos for a in abatimentos)

    return render_template(
        "ponto/analise.html",
        funcionario=funcionario,
        mes=mes,
        ano=ano,
        hoje=hoje,
        days=days,
        tabela_rows=tabela_rows,
        abatimentos=abatimentos,
        total_abatido=total_abatido,
        total_min=total_min,
        total_saldo=total_saldo,
        dias_ok=dias_ok,
        dias_revisar=dias_revisar,
        maior_credito=maior_credito,
        maior_debito=maior_debito,
        chart_labels=json.dumps(chart_labels),
        chart_worked=json.dumps(chart_worked),
        chart_expected=json.dumps(chart_expected),
        chart_balance=json.dumps(chart_balance),
        # flags para o template adaptar navegação ao contexto RH
        from_rh=True,
        back_url=url_for("rh.dashboard", aba="colaboradores"),
    )


# ---------------------------------------------------------------------------
# Folha de Ponto — helper de permissão
# ---------------------------------------------------------------------------

def _verificar_permissao_folha(acao: str) -> bool:
    """Verifica se o funcionário logado possui a permissão 'folha_ponto.<acao>'.

    Retorna bool (não aborta) para exibição condicional de botões no template.
    Não levanta exceção; a rota final deve verificar independentemente.

    Args:
        acao: Ação a verificar ('view', 'edit', 'deducao', 'imprimir').

    Returns:
        True se o funcionário tiver a permissão, False caso contrário.
    """
    fid = session.get("funcionario_id")
    return bool(fid and has_permission(fid, "folha_ponto", acao))


# ---------------------------------------------------------------------------
# Folha de Ponto — Visualização
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>", methods=["GET"])
@login_required
@require_permission("folha_ponto", "view")
def folha_ponto(funcionario_id: int):
    """Exibe a Folha de Ponto CLT individual de um funcionário.

    Renderiza a tabela de 10 colunas conforme o padrão CLT, com navegação
    por mês e botões de ação condicionados pelas permissões do usuário.

    Path params:
        funcionario_id (int): PK do funcionário.

    Query params:
        mes (int): Mês a exibir (1–12). Padrão: mês corrente.
        ano (int): Ano a exibir. Padrão: ano corrente.

    Returns:
        Renderização de rh/folha_ponto.html.
    """
    hoje = date.today()
    try:
        mes = int(request.args.get("mes") or hoje.month)
        ano = int(request.args.get("ano") or hoje.year)
    except (TypeError, ValueError):
        mes, ano = hoje.month, hoje.year

    try:
        dados = RHService.folha_ponto_mes(funcionario_id, ano, mes)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("rh.dashboard", aba="colaboradores"))

    primeiro_do_mes = date(ano, mes, 1)
    mes_ant = (primeiro_do_mes - timedelta(days=1)).replace(day=1)
    mes_prx = (primeiro_do_mes.replace(day=28) + timedelta(days=4)).replace(day=1)

    from app.services.ponto_service import PontoService

    abatimentos = PontoService.listar_abatimentos(
        funcionario_id, inicio=primeiro_do_mes, fim=mes_prx,
    )

    return render_template(
        "rh/folha_ponto.html",
        dados=dados,
        mes=mes,
        ano=ano,
        mes_ant_mes=mes_ant.month,
        mes_ant_ano=mes_ant.year,
        mes_prx_mes=mes_prx.month,
        mes_prx_ano=mes_prx.year,
        abatimentos=abatimentos,
        pode_editar=_verificar_permissao_folha("edit"),
        pode_deducao=_verificar_permissao_folha("deducao"),
        pode_imprimir=_verificar_permissao_folha("imprimir"),
    )


# ---------------------------------------------------------------------------
# Folha de Ponto — Adicionar batida
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>/batida/adicionar", methods=["POST"])
@login_required
@require_permission("folha_ponto", "edit")
def folha_ponto_adicionar_batida(funcionario_id: int):
    """Registra uma nova batida administrativa na Folha de Ponto.

    Usa PontoService.registrar_batida com source='admin' e registra o
    AuditLog exigido pela Rule #3 do Ponto Eletrônico.

    Form fields:
        novo_horario (str): Datetime no formato datetime-local (YYYY-MM-DDTHH:MM).
        motivo (str): Justificativa obrigatória da inclusão.

    Returns:
        Redirect para folha_ponto com flash de resultado.
    """
    from app.services.ponto_service import PontoService

    ator_id = _get_ator_id()
    mes = int(request.form.get("mes") or date.today().month)
    ano = int(request.form.get("ano") or date.today().year)

    horario_str = (request.form.get("novo_horario") or "").strip()
    motivo = (request.form.get("motivo") or "").strip()

    try:
        if not horario_str:
            raise ValueError("O horário é obrigatório.")
        if not motivo:
            raise ValueError("O motivo é obrigatório.")

        from app.services.timezone_service import TimezoneService

        punch_time = TimezoneService.parse_datetime_local(horario_str)
        entry = PontoService.registrar_batida(
            funcionario_id=funcionario_id,
            punch_time=punch_time,
            source="admin",
            ator_id=ator_id,
        )
        flash("Batida adicionada com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("rh.folha_ponto", funcionario_id=funcionario_id, mes=mes, ano=ano))


# ---------------------------------------------------------------------------
# Folha de Ponto — Editar batida
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>/batida/<int:entry_id>/editar", methods=["POST"])
@login_required
@require_permission("folha_ponto", "edit")
def folha_ponto_editar_batida(funcionario_id: int, entry_id: int):
    """Edita o horário de uma batida existente na Folha de Ponto.

    Delega a PontoService.editar_batida_admin que exige motivo não vazio
    e registra AuditLog com snapshots before/after (Rule #1 e Rule #3).

    Path params:
        funcionario_id (int): PK do funcionário.
        entry_id (int): PK da TimeEntry a editar.

    Form fields:
        novo_horario (str): Novo datetime (YYYY-MM-DDTHH:MM).
        motivo (str): Justificativa obrigatória.

    Returns:
        Redirect para folha_ponto com flash de resultado.
    """
    from app.services.ponto_service import PontoService

    ator_id = _get_ator_id()
    mes = int(request.form.get("mes") or date.today().month)
    ano = int(request.form.get("ano") or date.today().year)

    horario_str = (request.form.get("novo_horario") or "").strip()
    motivo = (request.form.get("motivo") or "").strip()

    try:
        if not horario_str:
            raise ValueError("O horário é obrigatório.")
        from app.services.timezone_service import TimezoneService

        novo_horario = TimezoneService.parse_datetime_local(horario_str)
        PontoService.editar_batida_admin(
            time_entry_id=entry_id,
            novo_horario=novo_horario,
            motivo=motivo,
            ator_id=ator_id,
        )
        flash("Batida editada com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("rh.folha_ponto", funcionario_id=funcionario_id, mes=mes, ano=ano))


# ---------------------------------------------------------------------------
# Folha de Ponto — Deletar batida
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>/batida/<int:entry_id>/deletar", methods=["POST"])
@login_required
@require_permission("folha_ponto", "edit")
def folha_ponto_deletar_batida(funcionario_id: int, entry_id: int):
    """Remove uma batida da Folha de Ponto.

    Delega a PontoService.deletar_batida_admin que registra AuditLog
    com snapshot completo antes da exclusão (Rule #1 e Rule #3).

    Path params:
        funcionario_id (int): PK do funcionário.
        entry_id (int): PK da TimeEntry a remover.

    Form fields:
        motivo (str): Justificativa obrigatória da exclusão.

    Returns:
        Redirect para folha_ponto com flash de resultado.
    """
    from app.services.ponto_service import PontoService

    ator_id = _get_ator_id()
    mes = int(request.form.get("mes") or date.today().month)
    ano = int(request.form.get("ano") or date.today().year)
    motivo = (request.form.get("motivo") or "").strip()

    try:
        PontoService.deletar_batida_admin(
            time_entry_id=entry_id,
            motivo=motivo,
            ator_id=ator_id,
        )
        flash("Batida removida com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("rh.folha_ponto", funcionario_id=funcionario_id, mes=mes, ano=ano))


# ---------------------------------------------------------------------------
# Folha de Ponto — Registrar dedução de banco de horas
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>/deducao", methods=["POST"])
@login_required
@require_permission("folha_ponto", "deducao")
def folha_ponto_deducao(funcionario_id: int):
    """Registra uma dedução de banco de horas a partir da Folha de Ponto.

    Delega a PontoService.registrar_abatimento_horas que cria o registro
    TimeBankDeduction e atualiza o saldo do Funcionario, com AuditLog.

    Path params:
        funcionario_id (int): PK do funcionário.

    Form fields:
        deduction_type (str): "folga" ou "pagamento" (valores de DeductionType).
        minutos (int): Minutos a abater em pagamento. Ignorado em folga — o
            servidor abate a jornada prevista do dia.
        data_registro (str): Data de referência (YYYY-MM-DD); em folga, o dia
            da folga, que deixa de contar como falta.
        observacao (str): Texto exibido ao colaborador. Obrigatório em pagamento.
        pagamento_valor (str): Valor em R$. Obrigatório em pagamento; aceita
            vírgula como separador decimal.

    Returns:
        Redirect para folha_ponto com flash de resultado.
    """
    from app.services.ponto_service import PontoService

    ator_id = _get_ator_id()
    mes = int(request.form.get("mes") or date.today().month)
    ano = int(request.form.get("ano") or date.today().year)

    try:
        deduction_type = request.form.get("deduction_type") or ""
        minutos = int(request.form.get("minutos") or 0)
        data_str = (request.form.get("data_registro") or "").strip()
        observacao = (request.form.get("observacao") or "").strip()
        valor_str = (request.form.get("pagamento_valor") or "").strip().replace(",", ".")

        data_registro = datetime.strptime(data_str, "%Y-%m-%d").date() if data_str else date.today()

        lancamento = PontoService.registrar_abatimento_horas(
            funcionario_id=funcionario_id,
            deduction_type=deduction_type,
            minutos=minutos,
            data_registro=data_registro,
            observacao=observacao,
            ator_id=ator_id,
            pagamento_valor=valor_str or None,
        )
        if lancamento.deduction_type.value == "folga":
            m = lancamento.minutos_abatidos
            flash(
                f"Folga compensatória de {data_registro.strftime('%d/%m/%Y')} lançada: "
                f"{m // 60}h{m % 60:02d} abatidas do banco. O dia não conta mais como falta.",
                "sucesso",
            )
        else:
            flash("Lançamento registrado com sucesso.", "sucesso")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "erro")

    return redirect(url_for("rh.folha_ponto", funcionario_id=funcionario_id, mes=mes, ano=ano))


# ---------------------------------------------------------------------------
# Folha de Ponto — Estornar lançamento de banco de horas
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>/deducao/<int:deducao_id>/estornar", methods=["POST"])
@login_required
@require_permission("folha_ponto", "deducao")
def folha_ponto_estornar(funcionario_id: int, deducao_id: int):
    """Estorna um lançamento de banco de horas por contra-lançamento.

    Delega a PontoService.estornar_abatimento, que cria a linha inversa
    apontando para a original e devolve o saldo. Nada é apagado: as duas
    linhas ficam visíveis ao colaborador no extrato do portal.

    Path params:
        funcionario_id (int): PK do colaborador (usado no redirect e no guard).
        deducao_id (int): PK do lançamento a estornar.

    Form fields:
        motivo (str): Justificativa obrigatória, exibida ao colaborador.

    Returns:
        Redirect para folha_ponto com flash de resultado.
    """
    from app.models.ponto import TimeBankDeduction
    from app.services.ponto_service import PontoService

    ator_id = _get_ator_id()
    mes = int(request.form.get("mes") or date.today().month)
    ano = int(request.form.get("ano") or date.today().year)

    # Guard de IDOR: o lançamento precisa pertencer ao funcionário da URL,
    # senão um POST forjado estornaria o lançamento de outra pessoa.
    alvo = db.session.get(TimeBankDeduction, deducao_id)
    if not alvo or alvo.funcionario_id != funcionario_id:
        flash("Lançamento não encontrado para este colaborador.", "erro")
        return redirect(url_for("rh.folha_ponto", funcionario_id=funcionario_id, mes=mes, ano=ano))

    try:
        PontoService.estornar_abatimento(
            deduction_id=deducao_id,
            motivo=(request.form.get("motivo") or "").strip(),
            ator_id=ator_id,
        )
        flash("Lançamento estornado. O saldo foi devolvido ao colaborador.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("rh.folha_ponto", funcionario_id=funcionario_id, mes=mes, ano=ano))


# ---------------------------------------------------------------------------
# Folha de Ponto — Gerar PDF
# ---------------------------------------------------------------------------

@bp.route("/folha-ponto/<int:funcionario_id>/pdf", methods=["GET"])
@login_required
@require_permission("folha_ponto", "imprimir")
def folha_ponto_pdf(funcionario_id: int):
    """Gera e retorna o PDF da Folha de Ponto via WeasyPrint.

    Renderiza o template folha_ponto_pdf.html com os dados do mês,
    dados cadastrais da empresa (CNPJ/endereço) e converte para PDF
    usando WeasyPrint. Retorna o arquivo inline com nome padronizado.

    Path params:
        funcionario_id (int): PK do funcionário.

    Query params:
        mes (int): Mês do PDF (1–12). Padrão: mês corrente.
        ano (int): Ano do PDF. Padrão: ano corrente.

    Returns:
        Response com Content-Type application/pdf e filename padronizado.
    """
    from weasyprint import HTML

    hoje = date.today()
    try:
        mes = int(request.args.get("mes") or hoje.month)
        ano = int(request.args.get("ano") or hoje.year)
    except (TypeError, ValueError):
        mes, ano = hoje.month, hoje.year

    try:
        dados = RHService.folha_ponto_mes(funcionario_id, ano, mes)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("rh.dashboard", aba="colaboradores"))

    empresa_nome = ConfiguracaoService.get(CHAVE_BRANDING_EMPRESA_NOME) or ""
    empresa_razao_social = ConfiguracaoService.get(CHAVE_EMPRESA_RAZAO_SOCIAL) or ""
    empresa_cnpj = ConfiguracaoService.get(CHAVE_EMPRESA_CNPJ) or ""
    empresa_endereco = ConfiguracaoService.get(CHAVE_EMPRESA_ENDERECO) or ""

    html_str = render_template(
        "rh/folha_ponto_pdf.html",
        dados=dados,
        mes=mes,
        ano=ano,
        empresa_nome=empresa_nome,
        empresa_razao_social=empresa_razao_social,
        empresa_cnpj=empresa_cnpj,
        empresa_endereco=empresa_endereco,
        gerado_em=datetime.now(timezone.utc),
    )

    pdf_bytes = HTML(string=html_str).write_pdf()

    cpf = dados["funcionario"].cpf or "sem_cpf"
    filename = f"folha_ponto_{cpf}_{ano}-{mes:02d}.pdf"

    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# QR Code — geração e regeneração
# ---------------------------------------------------------------------------

@bp.route("/funcionarios/<int:funcionario_id>/qrcode.png")
@login_required
def qrcode_png(funcionario_id: int):
    """Serve a imagem PNG do QR code do funcionário.

    Exige permissão qrcode.view para ver o QR de outro funcionário.
    Colaboradores podem sempre visualizar o próprio QR (self-serve).

    Args:
        funcionario_id: PK do funcionário cujo QR code será gerado.

    Returns:
        Resposta HTTP com Content-Type image/png e os bytes do QR code.
    """
    from flask import current_app

    ator_id = session["funcionario_id"]
    # Permite visualização própria OU quem tem permissão específica de QR code
    if ator_id != funcionario_id and not has_permission(ator_id, "qrcode", "view"):
        abort(403)

    funcionario = db.session.get(Funcionario, funcionario_id)
    if not funcionario:
        abort(404)

    if not funcionario.token_qr:
        # Gera token se ainda não existir (funcionários cadastrados antes da feature)
        funcionario = FuncionarioService.regenerar_token_qr(
            funcionario_id=funcionario_id,
            ator_id=ator_id,
        )

    try:
        png_bytes = QRService.gerar_imagem_bytes(
            funcionario_id=funcionario.id,
            nome=funcionario.nome,
            token_qr=funcionario.token_qr,
            qr_secret_key=current_app.config["QR_SECRET_KEY"],
        )
    except RuntimeError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("rh.editar_funcionario", funcionario_id=funcionario_id))

    response = make_response(png_bytes)
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/funcionarios/<int:funcionario_id>/regenerar-qrcode", methods=["POST"])
@login_required
@require_permission("qrcode", "regenerar")
def regenerar_qrcode(funcionario_id: int):
    """Invalida o QR code atual e emite um novo token para o funcionário.

    Deve ser usado quando o QR code físico é perdido ou comprometido.
    Exige permissão qrcode.regenerar (ação sensível: revoga o crachá físico).

    Args:
        funcionario_id: PK do funcionário cujo QR code será regenerado.

    Returns:
        Redirect para o formulário de edição com flash de sucesso ou erro.
    """
    ator_id = session["funcionario_id"]
    try:
        FuncionarioService.regenerar_token_qr(
            funcionario_id=funcionario_id,
            ator_id=ator_id,
        )
        flash("QR code regenerado com sucesso. Imprima o novo código.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")
    return redirect(url_for("rh.editar_funcionario", funcionario_id=funcionario_id))
