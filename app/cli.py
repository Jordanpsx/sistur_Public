# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Flask CLI commands for one-time setup and maintenance tasks.

Usage
-----
    flask setup                      # interactive — prompts for nome and CPF; creates super_admin role and assigns it
    flask setup --nome "Jordan Machado" --cpf "529.982.247-25"
    flask seed-roles                 # cria os roles iniciais (super_admin, funcionario)
    flask assign-role                # atribui um role a um funcionário existente
    flask assign-role --cpf "52998224725" --role "super_admin"
"""


import click

from app.core.permission_catalog import PERMISSOES_DO_SISTEMA
from app.extensions import db
from app.models.funcionario import Funcionario, validar_cpf
from app.models.role import Role, RolePermission


def register_commands(app) -> None:
    @app.cli.command("setup")
    @click.option(
        "--nome",
        prompt="Nome completo",
        help="Nome do primeiro funcionário.",
    )
    @click.option(
        "--cpf",
        prompt="CPF (somente números ou formatado)",
        help="CPF do primeiro funcionário.",
    )
    @click.option(
        "--cargo",
        default="Administrador",
        show_default=True,
        help="Cargo do primeiro funcionário.",
    )
    def setup_command(nome: str, cpf: str, cargo: str) -> None:
        """Cria as tabelas e o primeiro funcionário se o sistema estiver vazio."""
        # 1. Create / verify all tables
        db.create_all()
        click.echo("[ok] Tabelas criadas/verificadas.")

        # 2. Guard — abort if employees already exist
        if db.session.query(Funcionario).count() > 0:
            click.echo(
                "[aviso] O sistema ja possui funcionarios cadastrados. "
                "Nenhuma alteracao foi feita."
            )
            return

        # 3. Validate CPF before inserting
        try:
            cpf_limpo = validar_cpf(cpf)
        except ValueError as exc:
            click.echo(f"[erro] CPF invalido: {exc}", err=True)
            raise SystemExit(1)

        # 4. Create or get the super_admin role
        super_admin = db.session.query(Role).filter_by(nome="super_admin").first()
        if not super_admin:
            super_admin = Role(
                nome="super_admin",
                descricao="Acesso irrestrito ao sistema. Bypassa todas as verificações de permissão.",
                is_super_admin=True,
                ativo=True,
            )
            db.session.add(super_admin)
            db.session.flush()
            click.echo("[ok] Role 'super_admin' criado automaticamente.")

        # 5. Create the first employee with super_admin role
        funcionario = Funcionario(
            nome=nome.strip(),
            cpf=cpf_limpo,
            cargo=cargo,
            ativo=True,
            role_id=super_admin.id,
        )
        db.session.add(funcionario)
        db.session.commit()

        click.echo(f"[ok] Funcionario criado: {funcionario.nome} — CPF: {funcionario.cpf_formatado()}")
        click.echo(f"     Role: super_admin (acesso irrestrito)")
        click.echo(f"     Acesse /portal/login e informe o CPF: {funcionario.cpf_formatado()}")

    @app.cli.command("seed-roles")
    def seed_roles_command() -> None:
        """Cria os roles iniciais (super_admin, funcionario) e suas permissões.

        Idempotente: roles e permissões já existentes são ignorados.
        Execute após 'flask setup' para configurar o sistema de permissões.

        Roles criados:
            super_admin — acesso irrestrito (is_super_admin=True)
            funcionario — acesso básico ao dashboard do portal
        """
        db.create_all()

        # -----------------------------------------------------------------
        # Role: super_admin
        # -----------------------------------------------------------------
        super_admin = db.session.query(Role).filter_by(nome="super_admin").first()
        if not super_admin:
            super_admin = Role(
                nome="super_admin",
                descricao="Acesso irrestrito ao sistema. Bypassa todas as verificações de permissão.",
                is_super_admin=True,
                ativo=True,
            )
            db.session.add(super_admin)
            db.session.flush()
            click.echo("[ok] Role 'super_admin' criado.")
        else:
            click.echo("[aviso] Role 'super_admin' ja existe — ignorado.")

        # -----------------------------------------------------------------
        # Role: funcionario
        # -----------------------------------------------------------------
        funcionario_role = db.session.query(Role).filter_by(nome="funcionario").first()
        if not funcionario_role:
            funcionario_role = Role(
                nome="funcionario",
                descricao="Acesso básico ao portal do colaborador (somente dashboard).",
                is_super_admin=False,
                ativo=True,
            )
            db.session.add(funcionario_role)
            db.session.flush()
            click.echo("[ok] Role 'funcionario' criado.")
        else:
            click.echo("[aviso] Role 'funcionario' ja existe — ignorado.")

        # -----------------------------------------------------------------
        # Permissões do role funcionario: apenas dashboard.view
        # -----------------------------------------------------------------
        _ensure_permission(funcionario_role, "dashboard", "view")

        db.session.commit()

        # -----------------------------------------------------------------
        # Resumo de todas as permissões disponíveis (para referência)
        # -----------------------------------------------------------------
        click.echo("\nPermissoes registradas no sistema:")
        for modulo, acoes in PERMISSOES_DO_SISTEMA.items():
            for acao in acoes:
                click.echo(f"  {modulo}.{acao}")

        click.echo(
            "\n[ok] Seed concluido. Use 'flask seed-roles' novamente para atualizar."
        )


    @app.cli.command("assign-role")
    @click.option(
        "--cpf",
        prompt="CPF do funcionário (somente números ou formatado)",
        help="CPF do funcionário que receberá o role.",
    )
    @click.option(
        "--role",
        type=click.Choice(["super_admin", "funcionario"], case_sensitive=False),
        prompt="Role a atribuir",
        help="Nome do role a atribuir.",
    )
    def assign_role_command(cpf: str, role: str) -> None:
        """Atribui um role a um funcionário existente."""
        db.create_all()

        # Validate CPF
        try:
            cpf_limpo = validar_cpf(cpf)
        except ValueError as exc:
            click.echo(f"[erro] CPF invalido: {exc}", err=True)
            raise SystemExit(1)

        # Find employee
        funcionario = db.session.query(Funcionario).filter_by(cpf=cpf_limpo).first()
        if not funcionario:
            click.echo(f"[erro] Funcionario com CPF {cpf} nao encontrado.", err=True)
            raise SystemExit(1)

        # Find or create the role
        role_lower = role.lower()
        role_obj = db.session.query(Role).filter_by(nome=role_lower).first()
        if not role_obj:
            click.echo(f"[erro] Role '{role}' nao existe. Execute 'flask seed-roles' primeiro.", err=True)
            raise SystemExit(1)

        # Update employee
        funcionario.role_id = role_obj.id
        db.session.commit()

        click.echo(f"[ok] Funcionario '{funcionario.nome}' agora possui o role '{role_obj.nome}'.")
        click.echo(f"     Super Admin: {role_obj.is_super_admin}")


def _ensure_permission(role: Role, modulo: str, acao: str) -> None:
    """Adiciona a permissão ao role somente se ainda não existir."""
    exists = (
        db.session.query(RolePermission)
        .filter_by(role_id=role.id, modulo=modulo, acao=acao)
        .first()
    )
    if not exists:
        db.session.add(RolePermission(role_id=role.id, modulo=modulo, acao=acao))

