# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Portal do Colaborador — Employee Portal routes.

Routes
------
GET  /portal/login          → login form
POST /portal/login          → CPF + password authentication
GET  /portal/dashboard      → employee dashboard
POST /portal/logout         → end session
GET  /portal/minha-senha    → change-password form
POST /portal/minha-senha    → change-password handler
"""

from __future__ import annotations

import functools

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

from app.core.audit import AuditService
from app.core.permission_catalog import permissoes_dashboard
from app.core.permissions import has_permission
from app.extensions import db
from app.models.funcionario import Funcionario, validar_cpf
from app.services.configuracao_service import ConfiguracaoService
from app.services.funcionario_service import FuncionarioService

bp = Blueprint("portal", __name__)


# ---------------------------------------------------------------------------
# Session guard
# ---------------------------------------------------------------------------

def login_required(fn):
    """Redirect to login if no active session."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("funcionario_id"):
            if (
                request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or request.accept_mimetypes.best == "application/json"
            ):
                return jsonify({"ok": False, "error": "Sessão expirada. Faça login novamente."}), 401
            return redirect(url_for("portal.login"))
        return fn(*args, **kwargs)
    return wrapper


def _get_funcionario_sessao() -> Funcionario:
    """Load the authenticated Funcionario from session or abort 401."""
    funcionario_id = session.get("funcionario_id")
    if not funcionario_id:
        abort(401)
    funcionario = db.session.get(Funcionario, funcionario_id)
    if not funcionario or not funcionario.ativo:
        session.clear()
        abort(401)
    return funcionario


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    """
    GET  → render login.html template
    POST → authenticate via CPF, create session, audit log
    """
    if request.method == "GET":
        # If already authenticated, go straight to dashboard
        if session.get("funcionario_id"):
            return redirect(url_for("portal.dashboard"))
        return render_template("auth/login.html")

    # --- POST: authenticate ---
    from werkzeug.security import check_password_hash

    cpf_raw: str = (request.form.get("cpf") or "").strip()
    senha_raw: str = (request.form.get("senha") or "").strip()

    # 1. Validate CPF format and algorithm
    try:
        cpf_limpo = validar_cpf(cpf_raw)
    except ValueError as exc:
        AuditService.log_login(None, success=False)
        flash(str(exc), "erro")
        return render_template("auth/login.html"), 400

    # 2. Look up active employee — use a constant-time path for both
    #    "not found" and "wrong password" to avoid user-enumeration
    funcionario = (
        db.session.query(Funcionario)
        .filter_by(cpf=cpf_limpo, ativo=True)
        .first()
    )

    _MSG_INVALIDO = "CPF ou senha incorretos."

    if funcionario is None:
        AuditService.log_login(None, success=False)
        flash(_MSG_INVALIDO, "erro")
        return render_template("auth/login.html"), 401

    # 3. Require password — reject accounts with no hash set yet
    if not funcionario.senha_hash:
        AuditService.log_login(funcionario.id, success=False)
        flash(
            "Sua conta ainda não possui senha de acesso. "
            "Solicite ao administrador que defina uma senha em RH > Funcionários > Editar.",
            "erro",
        )
        return render_template("auth/login.html"), 401

    # 4. Verify password hash
    if not check_password_hash(funcionario.senha_hash, senha_raw):
        AuditService.log_login(funcionario.id, success=False)
        flash(_MSG_INVALIDO, "erro")
        return render_template("auth/login.html"), 401

    # 5. Create session
    session.clear()
    session["funcionario_id"] = funcionario.id
    session.permanent = True

    # 6. Audit — Auditoria obrigatória
    AuditService.log_login(funcionario.id, success=True)

    return redirect(url_for("portal.dashboard"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    """
    Painel principal do colaborador.

    Passa ao template um dicionário 'perms' com as permissões do funcionário
    logado, permitindo que o template exiba ou oculte seções dinamicamente.
    Funcionários sem role atribuído recebem perms vazias (somente dashboard).

    Returns:
        Renderização de portal/dashboard.html com o funcionário autenticado
        e seu mapa de permissões por módulo.
    """
    funcionario = _get_funcionario_sessao()
    fid = funcionario.id

    perms = {
        modulo: {
            acao: has_permission(fid, modulo, acao)
            for acao in acoes
        }
        for modulo, acoes in permissoes_dashboard().items()
    }

    modulos_ativos = {
        "ponto":       ConfiguracaoService.is_module_enabled("ponto"),
        "reservas":    ConfiguracaoService.is_module_enabled("reservas"),
    }

    is_super_admin = bool(funcionario.role and funcionario.role.is_super_admin)

    return render_template(
        "portal/dashboard.html",
        funcionario=funcionario,
        perms=perms,
        modulos_ativos=modulos_ativos,
        is_super_admin=is_super_admin,
    )


# ---------------------------------------------------------------------------
# Minha Senha
# ---------------------------------------------------------------------------

@bp.route("/minha-senha", methods=["GET"])
@login_required
def minha_senha_form():
    """Exibe o formulário de alteração de senha do funcionário autenticado.

    Returns:
        Renderização de portal/minha_senha.html.
    """
    funcionario = _get_funcionario_sessao()
    return render_template("portal/minha_senha.html", funcionario=funcionario)


@bp.route("/minha-senha", methods=["POST"])
@login_required
def minha_senha_submit():
    """Processa a alteração de senha do funcionário autenticado.

    Form fields:
        senha_atual (str): Senha atual para confirmação de identidade.
        nova_senha (str): Nova senha (mínimo 6 caracteres).
        confirmar_senha (str): Confirmação da nova senha.

    Returns:
        Redirect para minha_senha_form em caso de sucesso ou erro, com flash.
    """
    from werkzeug.security import check_password_hash

    funcionario = _get_funcionario_sessao()

    senha_atual   = (request.form.get("senha_atual")    or "").strip()
    nova_senha    = (request.form.get("nova_senha")     or "").strip()
    confirmar     = (request.form.get("confirmar_senha") or "").strip()

    # Verifica senha atual (obrigatória quando já existe hash)
    if funcionario.senha_hash:
        if not check_password_hash(funcionario.senha_hash, senha_atual):
            flash("Senha atual incorreta.", "erro")
            return redirect(url_for("portal.minha_senha_form"))

    if len(nova_senha) < 6:
        flash("A nova senha deve ter pelo menos 6 caracteres.", "erro")
        return redirect(url_for("portal.minha_senha_form"))

    if nova_senha != confirmar:
        flash("As senhas não coincidem.", "erro")
        return redirect(url_for("portal.minha_senha_form"))

    try:
        FuncionarioService.definir_senha(funcionario.id, nova_senha, ator_id=funcionario.id)
    except ValueError as exc:
        flash(str(exc), "erro")
        return redirect(url_for("portal.minha_senha_form"))

    flash("Senha alterada com sucesso.", "sucesso")
    return redirect(url_for("portal.minha_senha_form"))


# ---------------------------------------------------------------------------
# Preferência de tema (claro/escuro) — por conta
# ---------------------------------------------------------------------------

@bp.route("/tema", methods=["POST"])
@login_required
def definir_tema():
    """Salva a preferência de tema do colaborador autenticado (JSON).

    A preferência é por conta: isola usuários em terminais compartilhados e
    acompanha o colaborador entre dispositivos.

    JSON body:
        tema (str|null): 'light', 'dark' ou null (seguir padrão do sistema).

    Returns:
        JSON {"ok": true, "tema": <tema>} em caso de sucesso;
        {"ok": false, "error": <msg>} com status 400 se o tema for inválido.
    """
    dados = request.get_json(silent=True) or {}
    tema = dados.get("tema")
    if tema == "":
        tema = None

    try:
        FuncionarioService.definir_tema(session["funcionario_id"], tema)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, "tema": tema})


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@bp.route("/logout", methods=["POST"])
def logout():
    """End the session and audit the logout event."""
    funcionario_id = session.get("funcionario_id")

    if funcionario_id:
        # Auditoria obrigatória — log before clearing
        AuditService.log_logout(funcionario_id)

    session.clear()
    return redirect(url_for("portal.login"))
