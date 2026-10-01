# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Configurações — Painel de Controle Global.

Rotas
-----
GET  /admin/configuracoes/                        → painel principal (módulos, branding)
POST /admin/configuracoes/modulos/<chave>/toggle  → liga/desliga módulo (JSON)
POST /admin/configuracoes/branding                → salva identidade visual
GET  /admin/configuracoes/roles                   → lista de roles (RBAC)
POST /admin/configuracoes/roles/criar             → cria novo role
GET  /admin/configuracoes/roles/<id>/permissoes   → editor de permissões do role
POST /admin/configuracoes/roles/<id>/permissoes   → salva permissões (bulk)
POST /admin/configuracoes/roles/<id>/desativar    → desativa role

Controle de Acesso
------------------
Configurações globais permanecem exclusivas para super_admin. O editor de
permissões de roles também aceita supervisores delegados por domínio, limitado
ao subconjunto de permissões que o próprio operador possui e pode administrar.
"""

from __future__ import annotations

import functools

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.core.access_policy import ActorContext, ReservaAccessPolicy, RoleDelegationPolicy
from app.core.permission_catalog import (
    PERMISSOES_DO_SISTEMA,
    label_acao,
    label_modulo,
)
from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.reservas import ReservaSource
from app.models.role import Role
from app.services.configuracao_service import (
    CHAVE_BRANDING_EMPRESA_LOGO,
    CHAVE_BRANDING_EMPRESA_NOME,
    CHAVE_EMPRESA_RAZAO_SOCIAL,
    CHAVE_EMPRESA_CNPJ,
    CHAVE_EMPRESA_ENDERECO,
    CHAVE_MP_ACCESS_TOKEN,
    CHAVE_MP_PUBLIC_KEY,
    CHAVE_SYSTEM_TIMEZONE,
    ConfiguracaoService,
)
from app.models.calendario import TipoEvento
from app.models.geofence import GeofenceLocation
from app.services.role_service import RoleService
from app.services.secret_service import SecretService

bp = Blueprint("configuracoes", __name__)

# Aninhado por módulo: o mesmo slug de ação pode ter rótulos diferentes conforme
# o grupo (ex.: `configurar` em `reservas`).
_LABELS_ACAO = {
    modulo: {acao: label_acao(acao, modulo) for acao in acoes}
    for modulo, acoes in PERMISSOES_DO_SISTEMA.items()
}
_LABELS_MODULO = {modulo: label_modulo(modulo) for modulo in PERMISSOES_DO_SISTEMA}

# Integrações cujo segredo fica no cofre criptografado (SecretService).
SECRETS_CATALOG: list[dict[str, str]] = [
    {
        "key": SecretService.MERCADOPAGO_ACCESS_TOKEN,
        "provider": "mercadopago",
        "label": "Mercado Pago Access Token",
        "ajuda": (
            "Token privado usado para gerar PIX, links de cartão e processar webhooks. "
            "Tem prioridade sobre o campo legado em Configurações > Mercado Pago."
        ),
        "placeholder": "APP_USR-... ou TEST-...",
    },
]

_SECRETS_POR_KEY = {item["key"]: item for item in SECRETS_CATALOG}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_ator_sessao() -> Funcionario:
    """Carrega o Funcionario autenticado na sessão ou aborta.

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


def _super_admin_required(fn):
    """Aborta com 403 se o funcionário não for super_admin.

    Deve ser usado após @_login_required para garantir que a sessão existe.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ator = _get_ator_sessao()
        if not ator.role or not ator.role.is_super_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def _get_ator_id() -> int:
    """Retorna o ID do funcionário em sessão.

    Returns:
        ID do funcionário autenticado.
    """
    return session["funcionario_id"]


def _get_actor_context() -> ActorContext:
    """Monta o contexto de autorização do funcionário autenticado."""
    return ActorContext.from_funcionario_id(_get_ator_id())


def _permissoes_editaveis_para_role(actor: ActorContext, role: Role) -> dict[str, list[str]]:
    """Filtra o catálogo para o conjunto que o ator pode alterar no role."""
    if actor.is_super_admin:
        return PERMISSOES_DO_SISTEMA

    permissoes: dict[str, list[str]] = {}
    for modulo, acoes in PERMISSOES_DO_SISTEMA.items():
        acoes_editaveis = [
            acao
            for acao in acoes
            if RoleDelegationPolicy.can_grant_permission(actor, role, modulo, acao)
        ]
        if acoes_editaveis:
            permissoes[modulo] = acoes_editaveis
    return permissoes


def _reserva_sources_editaveis(actor: ActorContext) -> list[ReservaSource]:
    """Lista venues que o ator pode usar no escopo de permissões de Reservas.

    Returns:
        Lista de `ReservaSource` ativas e não removidas. Super admins e atores
        com `reservas.view` global veem todas; atores com escopo por venue veem
        somente as fontes já visíveis por sua política.
    """
    query = db.session.query(ReservaSource).filter(
        ReservaSource.deleted_at.is_(None),
        ReservaSource.is_active.is_(True),
    )
    source_ids = ReservaAccessPolicy.visible_source_ids(actor)
    if source_ids is not None:
        if not source_ids:
            return []
        query = query.filter(ReservaSource.id.in_(source_ids))
    return query.order_by(ReservaSource.display_name, ReservaSource.name).all()


def _reservas_acoes_escopadas_editaveis(
    actor: ActorContext,
    role: Role,
    sources: list[ReservaSource],
) -> dict[str, set[int]]:
    """Mapeia ações de Reservas que o ator pode administrar por venue.

    Args:
        actor: Contexto do operador autenticado.
        role: Role alvo da edição.
        sources: Venues candidatas para edição.

    Returns:
        Dicionário `acao -> {source_id}`. Super admins recebem todas as ações
        em todas as venues; supervisores delegados recebem apenas pares
        ação/venue nos quais já possuem a mesma permissão.
    """
    if not RoleDelegationPolicy.can_manage_role(actor, role):
        return {}
    if not RoleDelegationPolicy.can_manage_domain(actor, "reservas"):
        return {}

    source_ids = {source.id for source in sources}
    if actor.is_super_admin:
        return {
            acao: set(source_ids)
            for acao in PERMISSOES_DO_SISTEMA.get("reservas", [])
        }

    acoes_por_source: dict[str, set[int]] = {}
    for acao in PERMISSOES_DO_SISTEMA.get("reservas", []):
        ids_permitidos = {
            source_id
            for source_id in source_ids
            if actor.has("reservas", acao, source_id)
        }
        if ids_permitidos:
            acoes_por_source[acao] = ids_permitidos
    return acoes_por_source


# ---------------------------------------------------------------------------
# Painel principal
# ---------------------------------------------------------------------------

@bp.route("/", methods=["GET"])
@_login_required
@_super_admin_required
def index():
    """Exibe o painel de controle com módulos, regras globais e identidade visual.

    Returns:
        Renderização de admin/configuracoes/index.html com todas as
        configurações atuais e o estado de cada módulo.
    """
    todas = ConfiguracaoService.get_all()

    modulos_info = [
        {
            "chave":    f"modulo.{nome}",
            "nome":     nome,
            "label":    nome.capitalize(),
            "ativo":    ConfiguracaoService.is_module_enabled(nome),
        }
        for nome in ConfiguracaoService.MODULOS
    ]

    # Nomes de exibição configuráveis (2.12): valor customizado atual (ou vazio)
    nomes_modulos_info = [
        {
            "modulo": modulo,
            "padrao": padrao,
            "atual": (ConfiguracaoService.get(f"modulo.{modulo}.nome") or "").strip(),
        }
        for modulo, padrao in ConfiguracaoService.NOMES_MODULOS_PADRAO.items()
    ]

    geofence_locations = (
        db.session.query(GeofenceLocation)
        .order_by(GeofenceLocation.name)
        .all()
    )

    from app.models.calendario import GlobalEvent
    eventos_calendario = (
        db.session.query(GlobalEvent)
        .order_by(GlobalEvent.data_evento.desc())
        .all()
    )

    return render_template(
        "admin/configuracoes/index.html",
        modulos_info=modulos_info,
        nomes_modulos_info=nomes_modulos_info,
        empresa_nome=todas.get(CHAVE_BRANDING_EMPRESA_NOME) or "",
        empresa_logo=todas.get(CHAVE_BRANDING_EMPRESA_LOGO) or "",
        empresa_razao_social=todas.get(CHAVE_EMPRESA_RAZAO_SOCIAL) or "",
        empresa_cnpj=todas.get(CHAVE_EMPRESA_CNPJ) or "",
        empresa_endereco=todas.get(CHAVE_EMPRESA_ENDERECO) or "",
        mp_access_token=todas.get(CHAVE_MP_ACCESS_TOKEN) or "",
        mp_public_key=todas.get(CHAVE_MP_PUBLIC_KEY) or "",
        system_timezone=todas.get(CHAVE_SYSTEM_TIMEZONE) or "America/Sao_Paulo",
        fusos_disponiveis=FUSOS_DISPONIVEIS,
        geofence_locations=geofence_locations,
        eventos_calendario=eventos_calendario,
    )


# ---------------------------------------------------------------------------
# Toggle de módulo (AJAX)
# ---------------------------------------------------------------------------

@bp.route("/modulos/<chave>/toggle", methods=["POST"])
@_login_required
@_super_admin_required
def toggle_modulo(chave: str):
    """Liga ou desliga um módulo pelo seu toggle switch.

    Recebe a chave no formato 'modulo.<nome>' e inverte o estado atual.
    Retorna JSON para que o frontend atualize a UI sem recarregar.

    Args:
        chave: Chave de configuração do módulo (e.g. 'modulo.ponto').

    Returns:
        JSON com o novo estado: {"ativo": bool, "chave": str}.

    Raises:
        HTTP 400: Se a chave não pertencer a um módulo válido.
        HTTP 403: Se o usuário não for super_admin.
    """
    modulos_validos = [f"modulo.{m}" for m in ConfiguracaoService.MODULOS]
    if chave not in modulos_validos:
        return jsonify({"erro": "Chave inválida."}), 400

    estado_atual = ConfiguracaoService.is_module_enabled(chave.replace("modulo.", ""))
    novo_estado = not estado_atual

    ConfiguracaoService.set(chave, novo_estado, ator_id=_get_ator_id())

    return jsonify({"ativo": novo_estado, "chave": chave})


# ---------------------------------------------------------------------------
# Nomes de exibição dos módulos (2.12)
# ---------------------------------------------------------------------------

@bp.route("/modulos/nomes", methods=["POST"])
@_login_required
@_super_admin_required
def salvar_nomes_modulos():
    """Salva os nomes de exibição dos módulos (white-label interno, 2.12).

    A chave interna do módulo (permissões, rotas, auditoria) nunca muda —
    apenas o rótulo exibido em cards, títulos e telas.

    Form fields:
        nome_<modulo> (str): Novo rótulo do módulo. Vazio volta ao padrão.
    """
    ator_id = _get_ator_id()
    alterados = 0
    for modulo in ConfiguracaoService.NOMES_MODULOS_PADRAO:
        novo = (request.form.get(f"nome_{modulo}") or "").strip()
        chave = f"modulo.{modulo}.nome"
        atual = (ConfiguracaoService.get(chave) or "").strip()
        if novo == atual:
            continue
        ConfiguracaoService.set(chave, novo or None, ator_id=ator_id)
        alterados += 1

    if alterados:
        flash(f"Nomes de {alterados} módulo(s) atualizados.", "sucesso")
    else:
        flash("Nenhum nome foi alterado.", "info")
    return redirect(url_for("configuracoes.index"))


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

@bp.route("/branding", methods=["POST"])
@_login_required
@_super_admin_required
def salvar_branding():
    """Salva as configurações de identidade visual da empresa.

    Form fields:
        empresa_nome (str): Nome da empresa exibido no portal.
                            Vazio mantém a variável de ambiente.
        empresa_logo (str): URL do logotipo. Vazio usa a variável de ambiente.

    Returns:
        Redirect para o painel de configurações com mensagem de sucesso.
    """
    ator_id = _get_ator_id()
    nome = (request.form.get("empresa_nome") or "").strip() or None
    logo = (request.form.get("empresa_logo") or "").strip() or None

    ConfiguracaoService.set(CHAVE_BRANDING_EMPRESA_NOME, nome, ator_id=ator_id)
    ConfiguracaoService.set(CHAVE_BRANDING_EMPRESA_LOGO, logo, ator_id=ator_id)

    flash("Identidade visual salva com sucesso.", "sucesso")
    return redirect(url_for("configuracoes.index"))


# ---------------------------------------------------------------------------
# Dados da Empresa (para cabeçalho da Folha de Ponto)
# ---------------------------------------------------------------------------

@bp.route("/dados-empresa", methods=["POST"])
@_login_required
@_super_admin_required
def salvar_dados_empresa():
    """Salva CNPJ e endereço da empresa nos system settings.

    Esses dados são exibidos no cabeçalho do PDF da Folha de Ponto.

    Form fields:
        empresa_razao_social (str): Razão Social exibida no PDF da Folha.
        empresa_cnpj (str): CNPJ no formato XX.XXX.XXX/XXXX-XX. Pode ser vazio.
        empresa_endereco (str): Endereço completo. Pode ser vazio.

    Returns:
        Redirect para o painel de configurações com flash de confirmação.
    """
    ator_id = _get_ator_id()
    ConfiguracaoService.set(
        CHAVE_EMPRESA_RAZAO_SOCIAL,
        (request.form.get("empresa_razao_social") or "").strip() or None,
        ator_id=ator_id,
    )
    ConfiguracaoService.set(
        CHAVE_EMPRESA_CNPJ,
        (request.form.get("empresa_cnpj") or "").strip() or None,
        ator_id=ator_id,
    )
    ConfiguracaoService.set(
        CHAVE_EMPRESA_ENDERECO,
        (request.form.get("empresa_endereco") or "").strip() or None,
        ator_id=ator_id,
    )
    flash("Dados da empresa salvos com sucesso.", "sucesso")
    return redirect(url_for("configuracoes.index"))


# ---------------------------------------------------------------------------
# Mercado Pago — credenciais de integração
# ---------------------------------------------------------------------------

@bp.route("/mercadopago", methods=["POST"])
@_login_required
@_super_admin_required
def salvar_mercadopago():
    """Salva as credenciais do Mercado Pago no banco de configurações.

    O Access Token é armazenado no banco de dados e lido pelo MercadoPagoService
    em tempo de execução para processar pagamentos. A Public Key é retornada
    ao frontend para inicializar o MP Bricks.

    Form fields:
        mp_access_token (str): Access Token do MP (APP_USR-... ou TEST-...).
                               Vazio = remove a configuração (usa env var de fallback).
        mp_public_key (str):   Public Key do MP (APP_USR-... ou TEST-...).
                               Vazio = remove a configuração.

    Returns:
        Redirect para o painel de configurações com mensagem de sucesso.
    """
    ator_id = _get_ator_id()
    access_token = (request.form.get("mp_access_token") or "").strip() or None
    public_key = (request.form.get("mp_public_key") or "").strip() or None

    ConfiguracaoService.set(CHAVE_MP_ACCESS_TOKEN, access_token, ator_id=ator_id)
    ConfiguracaoService.set(CHAVE_MP_PUBLIC_KEY, public_key, ator_id=ator_id)

    flash("Credenciais do Mercado Pago salvas com sucesso.", "sucesso")
    return redirect(url_for("configuracoes.index") + "#tab-mercadopago")


# ---------------------------------------------------------------------------
# Fuso Horário do Sistema
# ---------------------------------------------------------------------------

# Fusos comuns para o seletor na UI
FUSOS_DISPONIVEIS = [
    ("America/Sao_Paulo",       "America/Sao_Paulo (Brasília, GMT-3)"),
    ("America/Manaus",          "America/Manaus (AM, GMT-4)"),
    ("America/Belem",           "America/Belem (PA/MA, GMT-3)"),
    ("America/Fortaleza",       "America/Fortaleza (CE/RN, GMT-3)"),
    ("America/Recife",          "America/Recife (PE, GMT-3)"),
    ("America/Bahia",           "America/Bahia (BA, GMT-3)"),
    ("America/Cuiaba",          "America/Cuiaba (MT, GMT-4)"),
    ("America/Porto_Velho",     "America/Porto_Velho (RO, GMT-4)"),
    ("America/Boa_Vista",       "America/Boa_Vista (RR, GMT-4)"),
    ("America/Noronha",         "America/Noronha (Fernando de Noronha, GMT-2)"),
    ("America/Rio_Branco",      "America/Rio_Branco (AC, GMT-5)"),
    ("UTC",                     "UTC (Coordenado Universal)"),
]


@bp.route("/fuso-horario", methods=["POST"])
@_login_required
@_super_admin_required
def salvar_fuso_horario():
    """Salva o fuso horário global do sistema.

    O fuso é aplicado a todos os módulos que exibem ou processam datas/horas:
    Ponto Eletrônico, Reservas, Avisos/Agendador e filtros de template.

    Form fields:
        system_timezone (str): Identificador IANA do fuso (ex: America/Sao_Paulo).

    Returns:
        Redirect para o painel de configurações com flash de confirmação.

    Raises:
        HTTP 400: Se o fuso informado não for válido.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    ator_id = _get_ator_id()
    tz_str = (request.form.get("system_timezone") or "").strip()
    if not tz_str:
        flash("Informe um fuso horário válido.", "erro")
        return redirect(url_for("configuracoes.index"))
    try:
        ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        flash(f"Fuso horário inválido: {tz_str}", "erro")
        return redirect(url_for("configuracoes.index"))

    ConfiguracaoService.set(CHAVE_SYSTEM_TIMEZONE, tz_str, ator_id=ator_id)
    flash(f"Fuso horário atualizado para {tz_str}.", "sucesso")
    return redirect(url_for("configuracoes.index"))


# ---------------------------------------------------------------------------
# Cofre de Segredos / Integrações
# ---------------------------------------------------------------------------

@bp.route("/segredos", methods=["GET"])
@_login_required
@_super_admin_required
def segredos():
    """Exibe o cofre de segredos/integrações com valores apenas mascarados.

    Returns:
        Renderização de admin/configuracoes/segredos.html com cada integração
        do catálogo, indicando se já está configurada e sua máscara (sem expor
        o valor bruto).
    """
    itens = []
    for definicao in SECRETS_CATALOG:
        key = definicao["key"]
        itens.append({
            **definicao,
            "configurado": SecretService.exists(key),
            "mascara": SecretService.masked(key),
        })
    return render_template("admin/configuracoes/segredos.html", segredos=itens)


@bp.route("/segredos/salvar", methods=["POST"])
@_login_required
@_super_admin_required
def segredos_salvar():
    """Grava ou atualiza um segredo do catálogo no cofre criptografado.

    Form fields:
        key (str): Chave do segredo (deve pertencer ao catálogo).
        value (str): Valor bruto a criptografar. Vazio é rejeitado.

    Returns:
        Redirect para a tela de segredos com flash de resultado.
    """
    key = (request.form.get("key") or "").strip()
    value = (request.form.get("value") or "").strip()
    definicao = _SECRETS_POR_KEY.get(key)
    if not definicao:
        flash("Integração inválida.", "erro")
        return redirect(url_for("configuracoes.segredos"))
    if not value:
        flash("Informe um valor para salvar.", "erro")
        return redirect(url_for("configuracoes.segredos"))

    try:
        SecretService.set(
            key,
            value,
            ator_id=_get_ator_id(),
            provider=definicao.get("provider"),
            label=definicao.get("label"),
        )
        flash(f"{definicao['label']} salvo com sucesso.", "sucesso")
    except Exception:
        flash("Não foi possível salvar o segredo. Verifique a chave mestra do cofre.", "erro")

    return redirect(url_for("configuracoes.segredos"))


@bp.route("/segredos/<path:key>/remover", methods=["POST"])
@_login_required
@_super_admin_required
def segredos_remover(key: str):
    """Desativa um segredo do catálogo (idempotente).

    Args:
        key: Chave do segredo a remover (deve pertencer ao catálogo).

    Returns:
        Redirect para a tela de segredos com flash de resultado.
    """
    definicao = _SECRETS_POR_KEY.get((key or "").strip())
    if not definicao:
        flash("Integração inválida.", "erro")
        return redirect(url_for("configuracoes.segredos"))

    SecretService.delete(definicao["key"], ator_id=_get_ator_id())
    flash(f"{definicao['label']} removido.", "aviso")
    return redirect(url_for("configuracoes.segredos"))


# ---------------------------------------------------------------------------
# Roles — listagem
# ---------------------------------------------------------------------------

@bp.route("/roles", methods=["GET"])
@_login_required
def listar_roles():
    """Lista roles administráveis pelo ator.

    Super admin vê todos os roles. Supervisores delegados veem apenas roles com
    ao menos uma permissão editável dentro de seu domínio.

    Returns:
        Renderização de admin/configuracoes/roles.html com a lista
        de roles e flash messages de operações anteriores.
    """
    actor = _get_actor_context()
    roles = RoleService.listar(apenas_ativos=False)
    editable_role_ids: set[int] = set()

    if actor.is_super_admin:
        editable_role_ids = {role.id for role in roles}
    else:
        reserva_sources = _reserva_sources_editaveis(actor)
        roles = [
            role
            for role in roles
            if (
                _permissoes_editaveis_para_role(actor, role)
                or _reservas_acoes_escopadas_editaveis(actor, role, reserva_sources)
            )
        ]
        editable_role_ids = {role.id for role in roles}
        if not roles:
            abort(403)

    return render_template(
        "admin/configuracoes/roles.html",
        roles=roles,
        editable_role_ids=editable_role_ids,
        is_delegated_admin=not actor.is_super_admin,
    )


# ---------------------------------------------------------------------------
# Roles — criação
# ---------------------------------------------------------------------------

@bp.route("/roles/criar", methods=["POST"])
@_login_required
@_super_admin_required
def criar_role():
    """Cria um novo role a partir do formulário inline na listagem.

    Form fields:
        nome (str):     Slug único do role (snake_case).
        descricao (str): Descrição legível.
        is_super_admin (checkbox): Presente se deve ser super_admin.

    Returns:
        Redirect para a listagem de roles com mensagem de resultado.
    """
    nome = (request.form.get("nome") or "").strip()
    descricao = (request.form.get("descricao") or "").strip() or None
    is_super_admin = request.form.get("is_super_admin") == "on"

    try:
        RoleService.criar(
            nome=nome,
            descricao=descricao,
            is_super_admin=is_super_admin,
            ator_id=_get_ator_id(),
        )
        flash(f"Role '{nome}' criado com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("configuracoes.listar_roles"))


# ---------------------------------------------------------------------------
# Roles — editor de permissões
# ---------------------------------------------------------------------------

@bp.route("/roles/<int:role_id>/permissoes", methods=["GET"])
@_login_required
def editar_permissoes(role_id: int):
    """Exibe o editor de permissões de um role específico.

    Args:
        role_id: PK do role a editar.

    Returns:
        Renderização de admin/configuracoes/role_permissoes.html com
        o grid de toggles organizado por módulo.

    Raises:
        HTTP 403: Se o ator não puder administrar nenhuma permissão desse role.
        HTTP 404: Se o role não existir.
    """
    role = RoleService.buscar_por_id(role_id)
    if not role:
        abort(404)

    actor = _get_actor_context()
    permissoes_editaveis = _permissoes_editaveis_para_role(actor, role)
    reserva_sources = _reserva_sources_editaveis(actor)
    reservas_acoes_escopadas = _reservas_acoes_escopadas_editaveis(actor, role, reserva_sources)
    if not permissoes_editaveis and not reservas_acoes_escopadas:
        abort(403)

    # Conjunto de permissões atuais do role
    perms_atuais = {
        (p.modulo, p.acao)
        for p in role.permissoes
        if p.source_id is None
    }
    scoped_perms_atuais = {
        (p.acao, p.source_id)
        for p in role.permissoes
        if p.modulo == "reservas" and p.source_id is not None
    }
    reservas_acoes_escopaveis = [
        acao
        for acao in PERMISSOES_DO_SISTEMA.get("reservas", [])
        if acao in reservas_acoes_escopadas
    ]

    # Nomes de exibição customizados (2.12) sobrepõem os rótulos do catálogo
    labels_modulo = dict(_LABELS_MODULO)
    labels_modulo.update({
        modulo: nome
        for modulo, nome in ConfiguracaoService.get_nomes_modulos().items()
        if modulo in labels_modulo
    })

    return render_template(
        "admin/configuracoes/role_permissoes.html",
        role=role,
        permissoes_sistema=permissoes_editaveis,
        perms_atuais=perms_atuais,
        scoped_perms_atuais=scoped_perms_atuais,
        reserva_sources=reserva_sources,
        reservas_acoes_escopaveis=reservas_acoes_escopaveis,
        reservas_acoes_escopadas=reservas_acoes_escopadas,
        labels_modulo=labels_modulo,
        labels_acao=_LABELS_ACAO,
        is_delegated_editor=not actor.is_super_admin,
    )


@bp.route("/roles/<int:role_id>/permissoes", methods=["POST"])
@_login_required
def salvar_permissoes(role_id: int):
    """Salva permissões de um role respeitando o escopo do ator.

    Para super_admin, opera sobre o catálogo completo. Para supervisores
    delegados, opera apenas no subconjunto de permissões administrável pelo
    ator e preserva permissões fora desse escopo.

    Args:
        role_id: PK do role a editar.

    Returns:
        Redirect para o editor de permissões com mensagem de resultado.

    Raises:
        HTTP 403: Se o ator não puder administrar nenhuma permissão desse role.
        HTTP 404: Se o role não existir.
    """
    role = RoleService.buscar_por_id(role_id)
    if not role:
        abort(404)

    ator_id = _get_ator_id()
    actor = ActorContext.from_funcionario_id(ator_id)
    permissoes_editaveis = _permissoes_editaveis_para_role(actor, role)
    reserva_sources = _reserva_sources_editaveis(actor)
    reservas_acoes_escopadas = _reservas_acoes_escopadas_editaveis(actor, role, reserva_sources)
    if not permissoes_editaveis and not reservas_acoes_escopadas:
        abort(403)

    permissoes_editaveis_set: set[tuple[str, str]] = {
        (modulo, acao)
        for modulo, acoes in permissoes_editaveis.items()
        for acao in acoes
    }

    # Permissões editáveis que devem estar ativas (vindas dos checkboxes).
    perms_desejadas: set[tuple[str, str]] = set()
    for modulo, acoes in permissoes_editaveis.items():
        for acao in acoes:
            campo = f"perm_{modulo}_{acao}"
            if request.form.get(campo) == "on":
                perms_desejadas.add((modulo, acao))

    # Permissões atualmente existentes no role
    perms_atuais: set[tuple[str, str]] = {
        (p.modulo, p.acao)
        for p in role.permissoes
        if p.source_id is None
    }
    perms_atuais_editaveis = perms_atuais & permissoes_editaveis_set

    # Adicionar as que estão faltando
    for modulo, acao in perms_desejadas - perms_atuais_editaveis:
        try:
            RoleService.adicionar_permissao(role_id, modulo, acao, ator_id=ator_id)
        except ValueError:
            pass

    # Remover apenas permissões editáveis que não deveriam mais existir.
    for modulo, acao in perms_atuais_editaveis - perms_desejadas:
        try:
            RoleService.remover_permissao(role_id, modulo, acao, ator_id=ator_id)
        except ValueError:
            pass

    # Reservas também aceita permissões escopadas por venue. Para cada ação,
    # permissão global e permissões por venue são alternativas: se a global
    # estiver marcada, os escopos redundantes daquela ação são removidos.
    reservas_acoes_editaveis = set(reservas_acoes_escopadas)
    if reservas_acoes_editaveis:
        source_ids_editaveis = {
            source_id
            for source_ids in reservas_acoes_escopadas.values()
            for source_id in source_ids
        }
        scoped_desejadas: set[tuple[str, int]] = set()
        for acao in reservas_acoes_editaveis:
            if ("reservas", acao) in perms_desejadas:
                continue
            for source_id in reservas_acoes_escopadas[acao]:
                campo = f"scoped_reservas_{acao}_{source_id}"
                if request.form.get(campo) == "on":
                    scoped_desejadas.add((acao, source_id))

        scoped_atuais: set[tuple[str, int]] = {
            (p.acao, int(p.source_id))
            for p in role.permissoes
            if (
                p.modulo == "reservas"
                and p.source_id is not None
                and p.acao in reservas_acoes_editaveis
                and int(p.source_id) in reservas_acoes_escopadas[p.acao]
            )
        }

        for acao, source_id in scoped_desejadas - scoped_atuais:
            try:
                RoleService.adicionar_permissao(
                    role_id,
                    "reservas",
                    acao,
                    source_id=source_id,
                    ator_id=ator_id,
                )
            except ValueError:
                pass

        for acao, source_id in scoped_atuais - scoped_desejadas:
            try:
                RoleService.remover_permissao(
                    role_id,
                    "reservas",
                    acao,
                    source_id=source_id,
                    ator_id=ator_id,
                )
            except ValueError:
                pass

    flash(f"Permissões do role '{role.nome}' atualizadas.", "sucesso")
    return redirect(url_for("configuracoes.editar_permissoes", role_id=role_id))


# ---------------------------------------------------------------------------
# Roles — desativação
# ---------------------------------------------------------------------------

@bp.route("/roles/<int:role_id>/desativar", methods=["POST"])
@_login_required
@_super_admin_required
def desativar_role(role_id: int):
    """Desativa um role (soft-delete).

    Bloqueado para roles de super_admin para evitar lockout total.

    Args:
        role_id: PK do role a desativar.

    Returns:
        Redirect para a listagem de roles com mensagem de resultado.
    """
    try:
        role = RoleService.desativar(role_id, ator_id=_get_ator_id())
        flash(f"Role '{role.nome}' desativado.", "aviso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("configuracoes.listar_roles"))


# ---------------------------------------------------------------------------
# Geofencing CRUD
# ---------------------------------------------------------------------------

_GEO_FIELDS = ["id", "name", "latitude", "longitude", "radius_meters", "is_active"]


@bp.route("/geofence/criar", methods=["POST"])
@_login_required
@_super_admin_required
def geofence_criar():
    """Cria uma nova zona de geofence e registra no log de auditoria.

    Form fields:
        name (str): Nome descritivo da zona.
        latitude (float): Latitude do centro da zona em graus decimais.
        longitude (float): Longitude do centro da zona em graus decimais.
        radius_meters (int): Raio da zona em metros.

    Returns:
        Redirect para configuracoes.index.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    try:
        name          = request.form["name"].strip()
        latitude      = float(request.form["latitude"])
        longitude     = float(request.form["longitude"])
        radius_meters = int(request.form["radius_meters"])
    except (KeyError, ValueError):
        flash("Dados inválidos. Verifique latitude, longitude e raio.", "erro")
        return redirect(url_for("configuracoes.index"))

    if not name:
        flash("O nome da zona é obrigatório.", "erro")
        return redirect(url_for("configuracoes.index"))

    zona = GeofenceLocation(
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        is_active=True,
    )
    db.session.add(zona)
    db.session.flush()

    snapshot = BaseService._snapshot(zona, _GEO_FIELDS)
    db.session.commit()

    AuditService.log_create(
        module="geofence",
        entity_id=zona.id,
        new_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Zona '{zona.name}' criada com sucesso.", "sucesso")
    return redirect(url_for("configuracoes.index"))


@bp.route("/geofence/<int:loc_id>/editar", methods=["POST"])
@_login_required
@_super_admin_required
def geofence_editar(loc_id: int):
    """Atualiza os dados de uma zona de geofence existente.

    Args:
        loc_id: PK da GeofenceLocation a editar.

    Form fields:
        name (str): Novo nome da zona.
        latitude (float): Nova latitude.
        longitude (float): Nova longitude.
        radius_meters (int): Novo raio em metros.
        is_active (str): '1' para ativa, ausente para inativa.

    Returns:
        Redirect para configuracoes.index.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    zona = db.session.get(GeofenceLocation, loc_id)
    if not zona:
        flash("Zona não encontrada.", "erro")
        return redirect(url_for("configuracoes.index"))

    try:
        name          = request.form["name"].strip()
        latitude      = float(request.form["latitude"])
        longitude     = float(request.form["longitude"])
        radius_meters = int(request.form["radius_meters"])
        is_active     = request.form.get("is_active") == "1"
    except (KeyError, ValueError):
        flash("Dados inválidos. Verifique os campos do formulário.", "erro")
        return redirect(url_for("configuracoes.index"))

    if not name:
        flash("O nome da zona é obrigatório.", "erro")
        return redirect(url_for("configuracoes.index"))

    antes = BaseService._snapshot(zona, _GEO_FIELDS)

    zona.name          = name
    zona.latitude      = latitude
    zona.longitude     = longitude
    zona.radius_meters = radius_meters
    zona.is_active     = is_active
    db.session.commit()

    depois = BaseService._snapshot(zona, _GEO_FIELDS)

    AuditService.log_update(
        module="geofence",
        entity_id=zona.id,
        previous_state=antes,
        new_state=depois,
        actor_id=_get_ator_id(),
    )

    flash(f"Zona '{zona.name}' atualizada.", "sucesso")
    return redirect(url_for("configuracoes.index"))


@bp.route("/geofence/<int:loc_id>/deletar", methods=["POST"])
@_login_required
@_super_admin_required
def geofence_deletar(loc_id: int):
    """Remove permanentemente uma zona de geofence.

    Args:
        loc_id: PK da GeofenceLocation a remover.

    Returns:
        Redirect para configuracoes.index.
    """
    from app.core.audit import AuditService
    from app.services.base import BaseService

    zona = db.session.get(GeofenceLocation, loc_id)
    if not zona:
        flash("Zona não encontrada.", "erro")
        return redirect(url_for("configuracoes.index"))

    snapshot = BaseService._snapshot(zona, _GEO_FIELDS)
    nome = zona.name

    db.session.delete(zona)
    db.session.commit()

    AuditService.log_delete(
        module="geofence",
        entity_id=loc_id,
        previous_state=snapshot,
        actor_id=_get_ator_id(),
    )

    flash(f"Zona '{nome}' removida.", "aviso")
    return redirect(url_for("configuracoes.index"))


# ---------------------------------------------------------------------------
# Calendário Global — CRUD de eventos/feriados
# ---------------------------------------------------------------------------

_TIPO_EVENTO_MAP = {
    "NATIONAL_HOLIDAY": TipoEvento.NATIONAL_HOLIDAY,
    "LOCAL_HOLIDAY": TipoEvento.LOCAL_HOLIDAY,
    "MAINTENANCE": TipoEvento.MAINTENANCE,
    "HIGH_SEASON": TipoEvento.HIGH_SEASON,
}


@bp.route("/calendario/criar", methods=["POST"])
@_login_required
@_super_admin_required
def calendario_criar():
    """Cria um novo evento global no calendário do sistema.

    Form fields:
        titulo (str): Nome descritivo do evento.
        data_evento (str): Data inicial no formato YYYY-MM-DD.
        data_fim (str): Data final no formato YYYY-MM-DD. Opcional — vazio
            significa evento de um dia só.
        tipo (str): Tipo do evento (NATIONAL_HOLIDAY, LOCAL_HOLIDAY, etc.).
        afeta_precificacao (str): '1' se afeta precificação.
        afeta_folha (str): '1' se afeta folha de pagamento.
        recorrente_anual (str): '1' se o evento é recorrente anual.

    Returns:
        Redirect para configuracoes.index.
    """
    from datetime import date as date_type

    from app.services.calendar_service import CalendarService

    try:
        titulo = request.form["titulo"].strip()
        data_str = request.form["data_evento"]
        data_evento = date_type.fromisoformat(data_str)
        data_fim_str = (request.form.get("data_fim") or "").strip()
        data_fim = date_type.fromisoformat(data_fim_str) if data_fim_str else None
        tipo_str = request.form["tipo"]
        tipo = _TIPO_EVENTO_MAP.get(tipo_str)
        if not tipo:
            raise ValueError(f"Tipo inválido: {tipo_str}")
    except (KeyError, ValueError) as exc:
        flash(f"Dados inválidos: {exc}", "erro")
        return redirect(url_for("configuracoes.index"))

    afeta_precificacao = request.form.get("afeta_precificacao") == "1"
    afeta_folha = request.form.get("afeta_folha") == "1"
    recorrente_anual = request.form.get("recorrente_anual") == "1"

    try:
        CalendarService.criar(
            titulo=titulo,
            data_evento=data_evento,
            data_fim=data_fim,
            tipo=tipo,
            afeta_precificacao=afeta_precificacao,
            afeta_folha=afeta_folha,
            recorrente_anual=recorrente_anual,
            ator_id=_get_ator_id(),
        )
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("configuracoes.index"))

    flash(f"Evento '{titulo}' criado com sucesso.", "sucesso")
    return redirect(url_for("configuracoes.index"))


@bp.route("/calendario/<int:event_id>/editar", methods=["POST"])
@_login_required
@_super_admin_required
def calendario_editar(event_id: int):
    """Atualiza um evento existente no calendário do sistema.

    Args:
        event_id: PK do GlobalEvent a editar.

    Form fields:
        titulo (str): Novo nome do evento.
        data_evento (str): Nova data inicial no formato YYYY-MM-DD.
        data_fim (str): Nova data final. Vazio volta o evento a um dia só.
        tipo (str): Novo tipo do evento.
        afeta_precificacao (str): '1' se afeta precificação.
        afeta_folha (str): '1' se afeta folha de pagamento.
        recorrente_anual (str): '1' se o evento é recorrente anual.

    Returns:
        Redirect para configuracoes.index.
    """
    from datetime import date as date_type

    from app.services.calendar_service import CalendarService

    try:
        titulo = request.form["titulo"].strip()
        data_str = request.form["data_evento"]
        data_evento = date_type.fromisoformat(data_str)
        data_fim_str = (request.form.get("data_fim") or "").strip()
        data_fim = date_type.fromisoformat(data_fim_str) if data_fim_str else None
        tipo_str = request.form["tipo"]
        tipo = _TIPO_EVENTO_MAP.get(tipo_str)
        if not tipo:
            raise ValueError(f"Tipo inválido: {tipo_str}")
    except (KeyError, ValueError) as exc:
        flash(f"Dados inválidos: {exc}", "erro")
        return redirect(url_for("configuracoes.index"))

    afeta_precificacao = request.form.get("afeta_precificacao") == "1"
    afeta_folha = request.form.get("afeta_folha") == "1"
    recorrente_anual = request.form.get("recorrente_anual") == "1"

    try:
        CalendarService.atualizar(
            event_id=event_id,
            titulo=titulo,
            data_evento=data_evento,
            data_fim=data_fim,
            limpar_data_fim=data_fim is None,
            tipo=tipo,
            afeta_precificacao=afeta_precificacao,
            afeta_folha=afeta_folha,
            recorrente_anual=recorrente_anual,
            ator_id=_get_ator_id(),
        )
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("configuracoes.index"))

    flash(f"Evento '{titulo}' atualizado.", "sucesso")
    return redirect(url_for("configuracoes.index"))

@bp.route("/calendario/<int:event_id>/deletar", methods=["POST"])
@_login_required
@_super_admin_required
def calendario_deletar(event_id: int):
    """Remove um evento do calendário do sistema.

    Args:
        event_id: PK do GlobalEvent a remover.

    Returns:
        Redirect para configuracoes.index.
    """
    from app.services.calendar_service import CalendarService

    try:
        CalendarService.deletar(event_id=event_id, ator_id=_get_ator_id())
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("configuracoes.index"))

    flash("Evento removido.", "aviso")
    return redirect(url_for("configuracoes.index"))


# ---------------------------------------------------------------------------
# Informativos — Gerenciamento Centralizado de Informações
# GET  /admin/configuracoes/informativos                    → painel CRUD
# POST /admin/configuracoes/informativos/criar              → cria informativo
# POST /admin/configuracoes/informativos/<id>/editar        → atualiza informativo
# POST /admin/configuracoes/informativos/<id>/deletar       → remove informativo
# ---------------------------------------------------------------------------

@bp.route("/informativos", methods=["GET"])
@_login_required
@_super_admin_required
def informativos_index():
    """Exibe o painel de gerenciamento de Informativos.

    Lista todos os snippets de informação com seus flags de sincronização
    com o WordPress.  Oferece interface CRUD completa.

    Returns:
        Renderização de admin/configuracoes/informativos_index.html com a
        lista de todos os Informativos ordenados por título.
    """
    from app.services.informativo_service import InformativoService

    registros = InformativoService.listar_todos()
    return render_template(
        "admin/configuracoes/informativos_index.html",
        registros=registros,
    )


@bp.route("/informativos/criar", methods=["POST"])
@_login_required
@_super_admin_required
def informativos_criar():
    """Cria um novo Informativo a partir do formulário.

    Form fields:
        title (str): Título do snippet.
        slug (str): Slug único (letras minúsculas, dígitos, hífen, underscore).
        content (str): Corpo do conteúdo em texto livre / HTML.
        sync_wordpress (str): Presente se o checkbox estiver marcado.

    Returns:
        Redirect para configuracoes.informativos_index com mensagem de resultado.
    """
    from app.services.informativo_service import InformativoService

    title = (request.form.get("title") or "").strip()
    slug = (request.form.get("slug") or "").strip().lower()
    content = request.form.get("content") or None
    sync_wordpress = "sync_wordpress" in request.form

    try:
        InformativoService.criar(
            title=title,
            slug=slug,
            content=content,
            sync_wordpress=sync_wordpress,
            ator_id=_get_ator_id(),
        )
        flash(f"Informativo '{title}' criado com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("configuracoes.informativos_index"))


@bp.route("/informativos/<int:informativo_id>/editar", methods=["POST"])
@_login_required
@_super_admin_required
def informativos_editar(informativo_id: int):
    """Atualiza um Informativo existente a partir do formulário.

    Args:
        informativo_id: Chave primária do Informativo a atualizar.

    Form fields:
        title (str): Novo título.
        content (str): Novo conteúdo.
        sync_wordpress (str): Presente se o checkbox estiver marcado.

    Returns:
        Redirect para configuracoes.informativos_index com mensagem de resultado.
    """
    from app.services.informativo_service import InformativoService

    title = (request.form.get("title") or "").strip()
    content = request.form.get("content") or None
    sync_wordpress = "sync_wordpress" in request.form

    try:
        InformativoService.atualizar(
            informativo_id=informativo_id,
            title=title,
            content=content,
            sync_wordpress=sync_wordpress,
            ator_id=_get_ator_id(),
        )
        flash(f"Informativo salvo com sucesso.", "sucesso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("configuracoes.informativos_index"))


@bp.route("/informativos/<int:informativo_id>/deletar", methods=["POST"])
@_login_required
@_super_admin_required
def informativos_deletar(informativo_id: int):
    """Remove permanentemente um Informativo.

    Args:
        informativo_id: Chave primária do Informativo a remover.

    Returns:
        Redirect para configuracoes.informativos_index com mensagem de resultado.
    """
    from app.services.informativo_service import InformativoService

    try:
        InformativoService.deletar(informativo_id=informativo_id, ator_id=_get_ator_id())
        flash("Informativo removido.", "aviso")
    except ValueError as exc:
        flash(str(exc), "erro")

    return redirect(url_for("configuracoes.informativos_index"))
