# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

from flask import Flask, redirect, url_for
from flask_migrate import Migrate
from flask_cors import CORS

from app.config import config
from app.extensions import db, socketio

migrate = Migrate()


def create_app(config_name: str = "default") -> Flask:
    """
    Flask application factory.

    Args:
        config_name: One of 'development', 'production', or 'default'.
    """
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(
        app,
        async_mode=app.config["SOCKETIO_ASYNC_MODE"],
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False,
    )

    # CORS — Allow requests from WordPress at https://cachoeiradogirassol.com.br
    CORS(app, origins=[
        "https://cachoeiradogirassol.com.br",
        "http://localhost:8080",
        "http://cachoeira-wordpress-prod:80",
    ], supports_credentials=True)

    # Branding context processor — makes {{ company_name }} and {{ company_logo }}
    # available in every Jinja2 template without manual passing.
    # Os valores do banco de dados têm prioridade sobre as variáveis de ambiente.
    @app.context_processor
    def inject_branding():
        try:
            from app.services.configuracao_service import ConfiguracaoService
            nome = ConfiguracaoService.get("branding.empresa_nome") or app.config["COMPANY_NAME"]
            logo = ConfiguracaoService.get("branding.empresa_logo") or app.config["COMPANY_LOGO"]
        except Exception:
            # Fallback para env vars se o banco ainda não estiver disponível
            # (ex: durante execução de migrações ou testes sem tabelas criadas)
            nome = app.config["COMPANY_NAME"]
            logo = app.config["COMPANY_LOGO"]
        return {"company_name": nome, "company_logo": logo}

    # Avisos context processor — injeta avisos_nao_lidos em todos os templates
    # para exibir o badge no sino de notificações da topbar.
    @app.context_processor
    def inject_avisos():
        """Injeta contagens de notificações em todos os templates para o badge da sineta.

        Retorna:
            avisos_nao_lidos      — avisos não lidos do colaborador logado.
            ajustes_pendentes_count — aprovações pendentes (ajustes de ponto e/ou
                                       quebras de caixa PDV, conforme permissões).
        """
        try:
            from flask import session
            fid = session.get("funcionario_id")
            if fid:
                from app.services.aviso_service import AvisoService
                from app.core.permissions import has_permission
                nao_lidos = AvisoService.contar_nao_lidos(fid)
                pendentes = 0
                if has_permission(fid, "ajuste_ponto", "aprovar"):
                    from app.services.ajuste_ponto_service import AjustePontoService
                    pendentes += AjustePontoService.contar_pendentes()
                if has_permission(fid, "pdv", "manage_shift"):
                    from app.services.pdv_service import PdvService
                    pendentes += len(PdvService.listar_turnos_pendentes())
                return {
                    "avisos_nao_lidos": nao_lidos,
                    "ajustes_pendentes_count": pendentes,
                }
        except Exception:
            pass
        return {"avisos_nao_lidos": 0, "ajustes_pendentes_count": 0}

    @app.context_processor
    def inject_is_super_admin():
        """Injeta is_super_admin para gatear a topbar.

        Views que já passam is_super_admin explicitamente ao render_template
        têm prioridade — este processor só preenche quando ausente.
        """
        try:
            from flask import session
            fid = session.get("funcionario_id")
            if fid:
                from app.models.funcionario import Funcionario
                f = db.session.get(Funcionario, fid)
                if f and f.ativo:
                    return {"is_super_admin": bool(f.role and f.role.is_super_admin)}
        except Exception:
            pass
        return {"is_super_admin": False}

    @app.context_processor
    def inject_permissions():
        """Injeta has_permission como callable em todos os templates."""
        from app.core.permissions import has_permission
        return {"has_permission": has_permission}

    @app.context_processor
    def inject_nomes_modulos():
        """Injeta os nomes de exibição configuráveis dos módulos (2.12).

        Templates leem `nomes_modulos.get('ponto', 'Ponto Eletrônico')` — o super
        admin pode renomear qualquer módulo em Configurações sem deploy.
        """
        from app.services.configuracao_service import ConfiguracaoService
        try:
            return {"nomes_modulos": ConfiguracaoService.get_nomes_modulos()}
        except Exception:
            return {"nomes_modulos": dict(ConfiguracaoService.NOMES_MODULOS_PADRAO)}

    @app.context_processor
    def inject_tema():
        """Injeta a preferência de tema (claro/escuro) do colaborador logado.

        Por conta: o tema é resolvido no servidor a partir do usuário da sessão,
        de modo que terminais compartilhados não vazem a escolha de um usuário
        para outro. Retorna '' quando não há preferência ou ninguém logado —
        nesse caso o front segue o prefers-color-scheme do dispositivo.
        """
        try:
            from flask import session
            fid = session.get("funcionario_id")
            if fid:
                from app.models.funcionario import Funcionario
                f = db.session.get(Funcionario, fid)
                if f and f.tema_preferido:
                    return {"tema_preferido": f.tema_preferido}
        except Exception:
            pass
        return {"tema_preferido": ""}

    def _get_system_tz():
        """Retorna o ZoneInfo do fuso configurado em system.timezone, ou America/Sao_Paulo."""
        from zoneinfo import ZoneInfo
        try:
            from app.services.configuracao_service import ConfiguracaoService
            tz_str = ConfiguracaoService.get("system.timezone", "America/Sao_Paulo") or "America/Sao_Paulo"
            return ZoneInfo(tz_str)
        except Exception:
            return ZoneInfo("America/Sao_Paulo")

    # Documento pessoal — exibição parcial para conferência sem revelar o número
    @app.template_filter("documento_parcial")
    def documento_parcial(valor):
        """Mascara um documento para exibição, preservando os três primeiros dígitos.

        Serve à conferência de balcão: o atendente confirma o começo do número
        que o cliente apresenta sem que a tela exponha o documento inteiro.

        Não use em ``value=`` de input — o formulário gravaria os asteriscos por
        cima do documento real. Somente em exibição.

        Args:
            valor: Documento vindo do modelo, ou None.

        Returns:
            Ex.: ``057.***.***-**``. Vazio vira string vazia.
        """
        from app.core.dados_sensiveis import mascarar_documento
        return mascarar_documento(valor) or ""

    # Timezone filter — converte datetime UTC para o fuso configurado nos templates
    @app.template_filter("to_brasilia_time")
    def to_brasilia_time(dt_value):
        """Converte datetime UTC para o fuso horário do sistema para exibição no template.

        Args:
            dt_value: datetime object (esperado em UTC).

        Returns:
            String formatada no fuso local (HH:MM:SS) ou string original se inválida.
        """
        from datetime import datetime, timezone
        if not dt_value:
            return ""
        try:
            if isinstance(dt_value, str):
                dt_value = datetime.fromisoformat(dt_value)
            if dt_value.tzinfo is None:
                dt_value = dt_value.replace(tzinfo=timezone.utc)
            return dt_value.astimezone(_get_system_tz()).strftime("%H:%M:%S")
        except Exception:
            return str(dt_value)

    @app.template_filter("to_brasilia_datetime")
    def to_brasilia_datetime(dt_value):
        """Converte datetime UTC para o fuso horário do sistema com data e hora.

        Args:
            dt_value: datetime object (esperado em UTC).

        Returns:
            String formatada no fuso local (DD/MM/YYYY HH:MM:SS) ou string original se inválida.
        """
        from datetime import datetime, timezone
        if not dt_value:
            return ""
        try:
            if isinstance(dt_value, str):
                dt_value = datetime.fromisoformat(dt_value)
            if dt_value.tzinfo is None:
                dt_value = dt_value.replace(tzinfo=timezone.utc)
            return dt_value.astimezone(_get_system_tz()).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            return str(dt_value)

    @app.template_filter("to_datetime_local_input")
    def to_datetime_local_input(dt_value):
        """Formata datetime UTC para inputs datetime-local no fuso do sistema."""
        try:
            from app.services.timezone_service import TimezoneService
            return TimezoneService.datetime_local_input(dt_value)
        except Exception:
            return ""

    # Root route — serve the login page at /
    @app.route("/")
    def index():
        return redirect(url_for("portal.login"))

    # Import models so SQLAlchemy registers all tables before create_all()
    with app.app_context():
        from app.core import models  # noqa: F401
        from app.models import funcionario  # noqa: F401
        from app.models import role  # noqa: F401
        from app.models import ponto  # noqa: F401
        from app.models import configuracoes  # noqa: F401
        from app.models import geofence  # noqa: F401
        from app.models import calendario  # noqa: F401
        from app.models import avisos  # noqa: F401  — sistur_avisos, sistur_ausencias_justificadas
        from app.models import ajuste_ponto  # noqa: F401  — sistur_ajuste_ponto_requests
        from app.models import reservas  # noqa: F401  — sistur_reserva_sources, categories, items, reservas, item_links, templates
        from app.models import payment  # noqa: F401  — sistur_payment_transactions
        from app.models import integration_log  # noqa: F401  — sistur_integration_logs
        from app.models import informativo  # noqa: F401  — sistur_informativos
        from app.models import secret  # noqa: F401  — sistur_secrets
        from app.models import organizacao  # noqa: F401  — sistur_funcoes, sistur_equipes, sistur_equipe_membros
        from app.models import escala  # noqa: F401  — sistur_escala_entradas
        from app.models import pdv  # noqa: F401  — sistur_pdv_shifts, sales, held_carts, offline_sync_log

    # Seed padrões de status de reserva se a tabela existir mas estiver vazia.
    # Necessário porque o evento after_create do SQLAlchemy não dispara via Alembic.
    with app.app_context():
        try:
            from app.models.reservas import ReservaStatusConfig, _STATUS_CONFIG_DEFAULTS
            if db.session.query(ReservaStatusConfig).count() == 0:
                db.session.bulk_insert_mappings(ReservaStatusConfig, _STATUS_CONFIG_DEFAULTS)
                db.session.commit()
            else:
                # Corrige flags de negócio que mudaram após o seed inicial.
                deposit = db.session.query(ReservaStatusConfig).filter_by(
                    internal_name="DEPOSIT"
                ).first()
                if deposit and not deposit.blocks_resource:
                    deposit.blocks_resource = True
                    db.session.commit()
        except Exception:
            db.session.rollback()

    # Blueprints
    from app.blueprints.portal.routes import bp as portal_bp
    app.register_blueprint(portal_bp, url_prefix="/portal")

    from app.blueprints.rh.routes import bp as rh_bp
    app.register_blueprint(rh_bp, url_prefix="/rh")

    from app.blueprints.ponto.routes import bp as ponto_bp
    app.register_blueprint(ponto_bp, url_prefix="/ponto")

    from app.blueprints.admin.routes import bp as admin_bp
    app.register_blueprint(admin_bp, url_prefix="/admin")

    from app.blueprints.configuracoes.routes import bp as configuracoes_bp
    app.register_blueprint(configuracoes_bp, url_prefix="/admin/configuracoes")

    from app.blueprints.api_internal.routes import bp as api_internal_bp
    app.register_blueprint(api_internal_bp, url_prefix="/api/internal")

    from app.blueprints.organizacao.routes import bp as organizacao_bp
    app.register_blueprint(organizacao_bp, url_prefix="/rh/organizacao")

    from app.blueprints.escala.routes import bp as escala_bp
    app.register_blueprint(escala_bp, url_prefix="/rh/escala")

    from app.blueprints.relatorios.routes import bp as relatorios_bp
    app.register_blueprint(relatorios_bp, url_prefix="/rh/relatorios")

    from app.blueprints.api_public import bp as api_public_bp
    app.register_blueprint(api_public_bp, url_prefix="/api/public")

    from app.blueprints.avisos.routes import bp as avisos_bp
    app.register_blueprint(avisos_bp, url_prefix="/avisos")

    from app.blueprints.reservas.routes import bp as reservas_bp
    app.register_blueprint(reservas_bp, url_prefix="/reservas")

    from app.blueprints.pagamentos import bp as pagamentos_bp
    app.register_blueprint(pagamentos_bp, url_prefix="/api/v1/payments")

    from app.blueprints.logs import bp as logs_bp
    app.register_blueprint(logs_bp, url_prefix="/api/logs")

    from app.blueprints.pdv import bp as pdv_bp
    app.register_blueprint(pdv_bp, url_prefix="/api/v1/pdv")

    # CLI commands
    from app.cli import register_commands
    register_commands(app)

    # APScheduler — monitoramento de presença em background.
    # Não inicializado em modo de teste para evitar jobs disparando durante pytest.
    if config_name != "testing":
        try:
            from flask_apscheduler import APScheduler
        except ImportError:
            APScheduler = None  # type: ignore[assignment,misc]

        if APScheduler is not None:
            scheduler = APScheduler()
            scheduler.init_app(app)

            @scheduler.task("interval", id="verificar_atrasos", minutes=5, misfire_grace_time=60)
            def job_verificar_atrasos():
                """Job periódico: detecta colaboradores com atraso e aplica débito provisório."""
                from app.services.aviso_service import AvisoService
                with scheduler.app.app_context():
                    AvisoService.verificar_atrasos()

            @scheduler.task("cron", id="finalizar_ausencias", hour=23, minute=0, misfire_grace_time=300)
            def job_finalizar_ausencias():
                """Job diário às 23h: confirma faltas do dia e notifica supervisores."""
                from app.services.aviso_service import AvisoService
                with scheduler.app.app_context():
                    AvisoService.finalizar_ausencias()

            @scheduler.task("cron", id="integridade_banco_horas", hour=3, minute=30, misfire_grace_time=1800)
            def job_integridade_banco_horas():
                """Job diário às 3h30: detecta drift no banco de horas e avisa quem corrige (2.9c)."""
                from app.services.aviso_service import AvisoService
                with scheduler.app.app_context():
                    report = AvisoService.verificar_integridade_banco_horas()
                    if report["divergencias"] or report["erros"]:
                        scheduler.app.logger.warning(
                            "integridade_banco_horas: divergencias=%s avisos=%s erros=%s",
                            report["divergencias"],
                            report["avisos_criados"],
                            report["erros"],
                        )

            @scheduler.task("interval", id="reservas_reconciliar_pendentes", hours=1, misfire_grace_time=300)
            def job_reservas_reconciliar_pendentes():
                """Job periódico: reconcilia pagamento de reservas WEB/BALCÃO e cancela vencidas.

                Generalização do job acima para fora do fluxo do bot. Roda em duas
                etapas na mesma ordem sempre — reconciliar antes de cancelar — para
                que uma reserva paga cujo webhook falhou nunca seja cancelada por a
                rotina de expiração ter rodado primeiro.
                """
                from app.services.reserva_service import ReservaService
                with scheduler.app.app_context():
                    pagamentos = ReservaService.reconciliar_pagamentos_pendentes(min_age_hours=2)
                    if pagamentos["verificadas"] or pagamentos["erros"]:
                        scheduler.app.logger.info(
                            "reservas_reconciliar_pendentes: verificadas=%s confirmadas=%s erros=%s",
                            pagamentos["verificadas"],
                            pagamentos["confirmadas"],
                            pagamentos["erros"],
                        )

                    cancelamentos = ReservaService.cancelar_pendentes_vencidas()
                    if cancelamentos["canceladas"]:
                        scheduler.app.logger.info(
                            "reservas_reconciliar_pendentes: avaliadas=%s canceladas=%s",
                            cancelamentos["avaliadas"],
                            cancelamentos["canceladas"],
                        )

            scheduler.start()

    return app
