# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
AvisoService — lógica de negócio do módulo de Notificações (Avisos).

Responsabilidades:
    - CRUD de avisos internos (criar, marcar lido, deletar).
    - Jobs de monitoramento de presença executados pelo scheduler:
        verificar_atrasos()   — roda a cada 5 minutos, detecta atrasos.
        finalizar_ausencias() — roda às 23h, confirma faltas do dia.

Auditoria obrigatória: toda mutação chama AuditService.
Arquitetura em camadas: sem imports do Flask — pure Python, Cython-compatível.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.audit import AuditService
from app.extensions import db
from app.services.base import BaseService


class AvisoService(BaseService):
    """
    Serviço de notificações internas (Avisos) e monitoramento automático de presença.

    Todos os métodos são estáticos — sem estado de instância — garantindo
    compatibilidade com compilação via Cython e importabilidade sem efeitos colaterais.
    """

    # -----------------------------------------------------------------------
    # CRUD de avisos
    # -----------------------------------------------------------------------

    @staticmethod
    def criar(
        destinatario_id: int,
        titulo: str,
        mensagem: str,
        tipo: str,
        remetente_id: int | None = None,
        ator_id: int | None = None,
    ) -> "Aviso":  # type: ignore[name-defined]  # noqa: F821
        """Cria um novo aviso interno e registra a criação no log de auditoria.

        Args:
            destinatario_id: ID do funcionário que receberá o aviso.
            titulo:          Título curto do aviso (max 200 caracteres).
            mensagem:        Corpo do aviso (texto livre, opcional).
            tipo:            Classificação do aviso ('ATRASO', 'FALTA' ou 'SISTEMA').
            remetente_id:    ID do funcionário remetente. None = aviso do sistema.
            ator_id:         ID do usuário que executa a ação (para auditoria).

        Returns:
            Instância de Aviso persistida no banco de dados.

        Raises:
            ValueError: Se o destinatário não for encontrado ou estiver inativo.
        """
        from app.models.avisos import Aviso, TipoAviso
        from app.models.funcionario import Funcionario

        destinatario = db.session.get(Funcionario, destinatario_id)
        if not destinatario or not destinatario.ativo:
            raise ValueError(f"Destinatário {destinatario_id} não encontrado ou inativo.")

        aviso = Aviso(
            destinatario_id=destinatario_id,
            remetente_id=remetente_id,
            titulo=titulo.strip(),
            mensagem=mensagem.strip() if mensagem else None,
            tipo=TipoAviso(tipo),
        )
        db.session.add(aviso)
        db.session.flush()

        AuditService.log_create(
            "avisos",
            entity_id=aviso.id,
            new_state={
                "destinatario_id": destinatario_id,
                "remetente_id": remetente_id,
                "titulo": titulo,
                "tipo": tipo,
            },
            actor_id=ator_id,
        )
        db.session.commit()
        return aviso

    @staticmethod
    def marcar_lido(aviso_id: int, ator_id: int) -> "Aviso":  # type: ignore[name-defined]  # noqa: F821
        """Marca um aviso como lido e registra a ação no log de auditoria.

        Args:
            aviso_id: PK do aviso a marcar como lido.
            ator_id:  ID do funcionário que está marcando (deve ser o destinatário).

        Returns:
            Instância atualizada de Aviso.

        Raises:
            ValueError: Se o aviso não for encontrado.
        """
        from app.models.avisos import Aviso

        aviso = db.session.get(Aviso, aviso_id)
        if not aviso:
            raise ValueError(f"Aviso {aviso_id} não encontrado.")

        if aviso.is_lido:
            return aviso  # idempotente — já estava lido

        aviso.is_lido = True
        db.session.flush()

        AuditService.log_update(
            "avisos",
            entity_id=aviso_id,
            previous_state={"is_lido": False},
            new_state={"is_lido": True},
            actor_id=ator_id,
        )
        db.session.commit()
        return aviso

    @staticmethod
    def marcar_todos_lidos(funcionario_id: int, ator_id: int) -> int:
        """Marca todos os avisos não lidos de um funcionário como lidos.

        Args:
            funcionario_id: ID do funcionário cujos avisos serão marcados.
            ator_id:        ID do usuário executando a ação (para auditoria).

        Returns:
            Número de avisos marcados como lidos.
        """
        from app.models.avisos import Aviso

        resultado = (
            db.session.query(Aviso)
            .filter_by(destinatario_id=funcionario_id, is_lido=False)
            .update({"is_lido": True}, synchronize_session="fetch")
        )
        db.session.flush()

        if resultado > 0:
            AuditService.log_update(
                "avisos",
                entity_id=funcionario_id,
                previous_state={"action": "marcar_todos_lidos", "count_antes": resultado},
                new_state={"count_marcados": resultado},
                actor_id=ator_id,
            )

        db.session.commit()
        return resultado

    @staticmethod
    def encerrar_por_referencia(
        referencia_tipo: str,
        referencia_id: int,
        ator_id: int | None = None,
        desfecho_titulo: str | None = None,
        desfecho_mensagem: str | None = None,
        excluir_destinatarios: set[int] | None = None,
    ) -> int:
        """Encerra os avisos-irmãos de uma solicitação resolvida (2.11).

        Quando uma solicitação enviada a vários aprovadores é resolvida por um
        deles, os avisos pendentes dos demais são marcados como lidos — e,
        opcionalmente, cada um recebe um aviso de desfecho ("aprovada por
        Fulano") para saber o resultado sem procurar.

        Args:
            referencia_tipo:  Tipo da solicitação de origem (ex.: 'ajuste_ponto').
            referencia_id:    PK da solicitação de origem.
            ator_id:          Quem resolveu (auditoria; excluído do desfecho).
            desfecho_titulo:  Título do aviso de desfecho. None = sem desfecho.
            desfecho_mensagem: Corpo do aviso de desfecho.
            excluir_destinatarios: IDs que não devem receber o desfecho
                (ex.: o solicitante, que recebe o aviso pessoal de resultado).

        Returns:
            Número de avisos encerrados (marcados como lidos).
        """
        from app.models.avisos import Aviso, TipoAviso

        pendentes = (
            db.session.query(Aviso)
            .filter_by(
                referencia_tipo=referencia_tipo,
                referencia_id=referencia_id,
                is_lido=False,
            )
            .all()
        )
        if not pendentes:
            return 0

        excluidos = excluir_destinatarios or set()
        if ator_id is not None:
            excluidos = excluidos | {ator_id}

        destinatarios_desfecho = []
        for aviso in pendentes:
            aviso.is_lido = True
            if aviso.destinatario_id not in excluidos:
                destinatarios_desfecho.append(aviso.destinatario_id)
        db.session.flush()

        AuditService.log_update(
            "avisos",
            entity_id=referencia_id,
            previous_state={
                "action": "encerrar_por_referencia",
                "referencia_tipo": referencia_tipo,
                "pendentes_antes": len(pendentes),
            },
            new_state={"encerrados": len(pendentes)},
            actor_id=ator_id,
        )

        if desfecho_titulo and desfecho_mensagem:
            for dest_id in set(destinatarios_desfecho):
                db.session.add(Aviso(
                    destinatario_id=dest_id,
                    remetente_id=ator_id,
                    titulo=desfecho_titulo,
                    mensagem=desfecho_mensagem,
                    tipo=TipoAviso.SISTEMA,
                ))
            db.session.flush()

        db.session.commit()
        return len(pendentes)

    @staticmethod
    def deletar(aviso_id: int, ator_id: int) -> None:
        """Remove um aviso e registra a exclusão no log de auditoria.

        Args:
            aviso_id: PK do aviso a remover.
            ator_id:  ID do usuário que executa a exclusão.

        Raises:
            ValueError: Se o aviso não for encontrado.
        """
        from app.models.avisos import Aviso

        aviso = db.session.get(Aviso, aviso_id)
        if not aviso:
            raise ValueError(f"Aviso {aviso_id} não encontrado.")

        snapshot = {
            "destinatario_id": aviso.destinatario_id,
            "titulo": aviso.titulo,
            "tipo": aviso.tipo.value,
            "is_lido": aviso.is_lido,
        }

        AuditService.log_delete(
            "avisos",
            entity_id=aviso_id,
            previous_state=snapshot,
            actor_id=ator_id,
        )
        db.session.delete(aviso)
        db.session.commit()

    @staticmethod
    def contar_nao_lidos(funcionario_id: int) -> int:
        """Retorna a contagem de avisos não lidos do funcionário.

        Utilizado pelo context processor inject_avisos() em cada requisição.
        Não gera log de auditoria (operação de leitura).

        Args:
            funcionario_id: ID do funcionário.

        Returns:
            Número inteiro de avisos com is_lido=False.
        """
        from app.models.avisos import Aviso

        return (
            db.session.query(Aviso)
            .filter_by(destinatario_id=funcionario_id, is_lido=False)
            .count()
        )

    @staticmethod
    def listar(funcionario_id: int, page: int = 1, per_page: int = 20) -> Any:
        """Retorna a listagem paginada de avisos de um funcionário, do mais recente ao mais antigo.

        Args:
            funcionario_id: ID do funcionário.
            page:           Página atual (base 1).
            per_page:       Itens por página.

        Returns:
            Objeto Pagination do SQLAlchemy.
        """
        from app.models.avisos import Aviso

        return (
            db.session.query(Aviso)
            .filter_by(destinatario_id=funcionario_id)
            .order_by(Aviso.criado_em.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )

    # -----------------------------------------------------------------------
    # Jobs de monitoramento de presença — chamados pelo APScheduler
    # -----------------------------------------------------------------------

    @staticmethod
    def verificar_atrasos() -> dict[str, Any]:
        """Verifica funcionários que não registraram ponto após o horário previsto + tolerância.

        Executado pelo APScheduler a cada 5 minutos. Para cada colaborador ativo com
        horario_entrada_padrao configurado:
            1. Ignora feriados (GlobalEvent.afeta_folha=True).
            2. Ignora dias inativos na jornada_semanal.
            3. Ignora quando ainda está dentro da janela de tolerância de 20 minutos.
            4. Ignora ausências justificadas aprovadas para hoje.
            5. Ignora quem já registrou ponto hoje.
            6. Ignora se o débito provisório já foi aplicado neste ciclo.
            7. Aplica débito provisório no TimeDay (saldo_final = -expected_minutes).
            8. Notifica o funcionário e seus supervisores de área via Aviso(ATRASO).

        Returns:
            Dicionário com 'processados' (int) e 'erros' (list[str]).
        """
        from zoneinfo import ZoneInfo

        from sqlalchemy import extract, func

        from app.models.avisos import AusenciaJustificada, TipoAviso
        from app.models.calendario import GlobalEvent
        from app.models.funcionario import Funcionario
        from app.models.ponto import TimeDay, TimeEntry
        from app.models.role import Role, RolePermission
        from app.services.configuracao_service import ConfiguracaoService
        from app.services.ponto_service import _get_expected_minutes

        # Converter UTC para hora local do negócio
        tz_str = ConfiguracaoService.get("system.timezone", "America/Sao_Paulo")
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = ZoneInfo("America/Sao_Paulo")

        agora_local = datetime.now(tz)
        today = agora_local.date()
        agora_time = agora_local.time()

        # Tolerância de 20 minutos além do horário de entrada
        TOLERANCIA_AVISO_MINUTOS = 20

        # Verificar se hoje é feriado com afeta_folha=True
        is_holiday = db.session.query(GlobalEvent).filter(
            GlobalEvent.afeta_folha == True,  # noqa: E712
            db.or_(
                GlobalEvent.data_evento == today,
                db.and_(
                    GlobalEvent.recorrente_anual == True,  # noqa: E712
                    extract("month", GlobalEvent.data_evento) == today.month,
                    extract("day", GlobalEvent.data_evento) == today.day,
                ),
            ),
        ).first() is not None

        if is_holiday:
            return {"processados": 0, "erros": [], "motivo": "feriado"}

        # Mapeia weekday() → chave de jornada_semanal
        dias_semana = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
        dia_key = dias_semana[today.weekday()]

        # Buscar todos os funcionários ativos com horário de entrada configurado.
        # Diaristas são excluídos: não têm jornada recorrente nem banco de horas,
        # então não geram débito/aviso de atraso automático (2.2).
        funcionarios = (
            db.session.query(Funcionario)
            .filter(
                Funcionario.ativo == True,  # noqa: E712
                Funcionario.horario_entrada_padrao.isnot(None),
                Funcionario.tipo_contrato != "DIARISTA",
            )
            .all()
        )

        processados = 0
        erros: list[str] = []

        for func in funcionarios:
            try:
                # 1. Dia inativo na jornada semanal?
                if func.jornada_semanal:
                    config_dia = func.jornada_semanal.get(dia_key, {})
                    if not config_dia.get("ativo", True):
                        continue

                # 2. Ainda dentro da janela de tolerância?
                entrada = func.horario_entrada_padrao
                limiar_dt = (
                    datetime.combine(today, entrada, tzinfo=tz)
                    + timedelta(minutes=TOLERANCIA_AVISO_MINUTOS)
                )
                if agora_local < limiar_dt:
                    continue

                # 3. Ausência justificada aprovada para hoje?
                tem_ausencia = (
                    db.session.query(AusenciaJustificada)
                    .filter_by(funcionario_id=func.id, data=today, aprovado=True)
                    .first()
                ) is not None
                if tem_ausencia:
                    continue

                # 4. Já registrou alguma batida hoje?
                tem_batida = (
                    db.session.query(TimeEntry)
                    .filter_by(funcionario_id=func.id, shift_date=today)
                    .first()
                ) is not None
                if tem_batida:
                    continue

                # 5. Débito provisório já aplicado neste ciclo? (guarda contra múltiplos workers)
                time_day = (
                    db.session.query(TimeDay)
                    .filter_by(funcionario_id=func.id, shift_date=today)
                    .first()
                )
                if time_day and time_day.auto_debit_aplicado:
                    continue

                # 6. O dia espera jornada? Folga de escala, falta justificada e
                # folga compensatória zeram a expectativa — sem isso o colaborador
                # e os supervisores recebiam "Atraso detectado" num dia de folga.
                expected = _get_expected_minutes(func, today)
                if expected <= 0:
                    continue

                # ── Aplicar débito provisório ─────────────────────────────
                old_saldo = time_day.saldo_final_minutos if time_day else 0

                if not time_day:
                    time_day = TimeDay(
                        funcionario_id=func.id,
                        shift_date=today,
                        expected_minutes_snapshot=expected,
                        tolerance_snapshot=10,
                    )
                    db.session.add(time_day)
                    db.session.flush()

                time_day.saldo_final_minutos = -expected
                time_day.saldo_calculado_minutos = -expected
                time_day.auto_debit_aplicado = True
                time_day.needs_review = False

                delta = time_day.saldo_final_minutos - old_saldo
                func.banco_horas_acumulado += delta

                AuditService.log_update(
                    "ponto",
                    func.id,
                    previous_state={"action": "auto_debit_ausencia", "saldo_anterior": old_saldo},
                    new_state={
                        "saldo_novo": time_day.saldo_final_minutos,
                        "auto_debit_aplicado": True,
                        "shift_date": today.isoformat(),
                    },
                    actor_id=None,
                )

                # ── Notificar funcionário e supervisores ─────────────────
                msg_func = (
                    f"Seu ponto não foi registrado até "
                    f"{limiar_dt.strftime('%H:%M')} de {today.strftime('%d/%m/%Y')}. "
                    "Um débito provisório foi lançado no seu banco de horas."
                )
                _criar_aviso_sem_commit(
                    func.id, "Atraso detectado", msg_func, TipoAviso.ATRASO
                )

                # Supervisores: funcionários da mesma área com permissão avisos.receber_alertas
                # ou com role is_super_admin=True
                if func.area_id:
                    supervisores = _buscar_supervisores(func.area_id, func.id)
                    msg_sup = (
                        f"{func.nome} não registrou ponto até "
                        f"{limiar_dt.strftime('%H:%M')} de {today.strftime('%d/%m/%Y')}."
                    )
                    for sup_id in supervisores:
                        _criar_aviso_sem_commit(
                            sup_id, f"Atraso: {func.nome}", msg_sup, TipoAviso.ATRASO
                        )

                db.session.commit()
                processados += 1

            except Exception as exc:
                db.session.rollback()
                erros.append(f"func_id={func.id}: {exc}")

        return {"processados": processados, "erros": erros}

    @staticmethod
    def finalizar_ausencias() -> dict[str, Any]:
        """Confirma as faltas não justificadas ao final do dia e notifica supervisores.

        Executado pelo APScheduler diariamente às 23h. Busca todos os TimeDay com
        auto_debit_aplicado=True para hoje. Se ainda não houver batidas reais,
        o débito provisório se torna definitivo e são criados Avisos de FALTA.

        Returns:
            Dicionário com 'confirmadas' (int) e 'erros' (list[str]).
        """
        from zoneinfo import ZoneInfo

        from app.models.avisos import TipoAviso
        from app.models.ponto import TimeDay, TimeEntry
        from app.services.configuracao_service import ConfiguracaoService

        tz_str = ConfiguracaoService.get("system.timezone", "America/Sao_Paulo")
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = ZoneInfo("America/Sao_Paulo")

        today = datetime.now(tz).date()

        dias_pendentes = (
            db.session.query(TimeDay)
            .filter_by(shift_date=today, auto_debit_aplicado=True)
            .all()
        )

        confirmadas = 0
        erros: list[str] = []

        for time_day in dias_pendentes:
            try:
                # Se o funcionário bateu ponto em algum momento, processar_dia()
                # já deveria ter resetado auto_debit_aplicado — confirmar aqui
                tem_batida = (
                    db.session.query(TimeEntry)
                    .filter_by(
                        funcionario_id=time_day.funcionario_id,
                        shift_date=today,
                    )
                    .first()
                ) is not None

                if tem_batida:
                    continue  # processar_dia() já tratou este caso

                func_id = time_day.funcionario_id

                # Notificar o próprio funcionário
                msg_func = (
                    f"Falta não justificada registrada em {today.strftime('%d/%m/%Y')}. "
                    "Entre em contato com o RH se houver engano."
                )
                _criar_aviso_sem_commit(
                    func_id, "Falta registrada", msg_func, TipoAviso.FALTA
                )

                # Notificar supervisores da área
                from app.models.funcionario import Funcionario
                func = db.session.get(Funcionario, func_id)
                if func and func.area_id:
                    supervisores = _buscar_supervisores(func.area_id, func_id)
                    msg_sup = (
                        f"{func.nome} teve falta não justificada em {today.strftime('%d/%m/%Y')}."
                    )
                    for sup_id in supervisores:
                        _criar_aviso_sem_commit(
                            sup_id, f"Falta: {func.nome}", msg_sup, TipoAviso.FALTA
                        )

                db.session.commit()
                confirmadas += 1

            except Exception as exc:
                db.session.rollback()
                erros.append(f"time_day_id={time_day.id}: {exc}")

        return {"confirmadas": confirmadas, "erros": erros}

    @staticmethod
    def verificar_integridade_banco_horas() -> dict[str, Any]:
        """Detecta drift no `banco_horas_acumulado` e avisa quem pode corrigir (2.9c).

        Fecha o ciclo detectar → corrigir: `RHService.divergencias_banco_horas`
        aponta o problema e `PontoService.recalcular_banco_global` conserta, mas
        sem este job ninguém descobre que precisa rodar o corretor — o cache é
        mantido por delta e diverge em silêncio.

        Executado pelo APScheduler diariamente. Notifica os supervisores da área
        do funcionário afetado (mesma regra de `avisos.receber_alertas` usada nos
        alertas de atraso) e os super admins, que podem agir em qualquer área.

        **Anti-spam:** se o destinatário já tem um aviso **não lido** sobre o
        mesmo funcionário, não cria outro — um alerta pendente já sinaliza a
        ação. Some sozinho quando a divergência é corrigida.

        Returns:
            Dicionário com 'divergencias' (int), 'avisos_criados' (int) e
            'erros' (list[str]).
        """
        from app.models.avisos import Aviso, TipoAviso
        from app.services.rh_service import RHService

        divergencias = RHService.divergencias_banco_horas()
        if not divergencias:
            return {"divergencias": 0, "avisos_criados": 0, "erros": []}

        super_admins = _buscar_super_admins()
        avisos_criados = 0
        erros: list[str] = []

        for div in divergencias:
            func = div["funcionario"]
            try:
                destinatarios = set(super_admins)
                if func.area_id:
                    destinatarios.update(_buscar_supervisores(func.area_id, func.id))
                destinatarios.discard(func.id)  # o próprio afetado não é quem corrige
                if not destinatarios:
                    continue

                titulo = f"Banco de horas divergente: {func.nome}"
                horas = div["diferenca"] / 60
                mensagem = (
                    f"O saldo em cache de {func.nome} está em {div['acumulado']} min, "
                    f"mas a soma real dos dias menos os abatimentos dá {div['esperado']} min "
                    f"(diferença de {div['diferenca']:+d} min / {horas:+.1f} h). "
                    f"Rode o recálculo do banco de horas deste colaborador para corrigir."
                )

                for dest_id in sorted(destinatarios):
                    ja_avisado = (
                        db.session.query(Aviso)
                        .filter_by(
                            destinatario_id=dest_id,
                            titulo=titulo,
                            is_lido=False,
                        )
                        .first()
                    ) is not None
                    if ja_avisado:
                        continue
                    _criar_aviso_sem_commit(dest_id, titulo, mensagem, TipoAviso.SISTEMA)
                    avisos_criados += 1

                db.session.commit()

            except Exception as exc:
                db.session.rollback()
                erros.append(f"func_id={func.id}: {exc}")

        return {
            "divergencias": len(divergencias),
            "avisos_criados": avisos_criados,
            "erros": erros,
        }


# ---------------------------------------------------------------------------
# Helpers de módulo (privados)
# ---------------------------------------------------------------------------


def _criar_aviso_sem_commit(
    destinatario_id: int,
    titulo: str,
    mensagem: str,
    tipo: "TipoAviso",  # type: ignore[name-defined]  # noqa: F821
) -> None:
    """Adiciona um Aviso à sessão sem fazer commit — o caller é responsável pelo commit.

    Args:
        destinatario_id: ID do funcionário destinatário.
        titulo:          Título do aviso.
        mensagem:        Corpo do aviso.
        tipo:            Enum TipoAviso.
    """
    from app.models.avisos import Aviso

    aviso = Aviso(
        destinatario_id=destinatario_id,
        titulo=titulo.strip(),
        mensagem=mensagem.strip(),
        tipo=tipo,
    )
    db.session.add(aviso)
    db.session.flush()

    AuditService.log_create(
        "avisos",
        entity_id=aviso.id,
        new_state={
            "destinatario_id": destinatario_id,
            "tipo": tipo.value,
            "titulo": titulo,
            "fonte": "scheduler",
        },
        actor_id=None,
    )


def _buscar_supervisores(area_id: int, excluir_id: int) -> list[int]:
    """Retorna IDs de funcionários ativos na área com permissão avisos.receber_alertas ou super_admin.

    Args:
        area_id:    ID da área para filtrar supervisores.
        excluir_id: ID do funcionário que originou o evento (não deve receber alerta duplicado).

    Returns:
        Lista de IDs de funcionários que devem receber o alerta.
    """
    from app.models.funcionario import Funcionario
    from app.models.role import Role, RolePermission

    # Busca funcionários ativos na mesma área com role ativo
    candidatos = (
        db.session.query(Funcionario)
        .join(Role, Funcionario.role_id == Role.id)
        .filter(
            Funcionario.area_id == area_id,
            Funcionario.ativo == True,  # noqa: E712
            Funcionario.id != excluir_id,
            Role.ativo == True,  # noqa: E712
        )
        .all()
    )

    supervisores: list[int] = []
    for candidato in candidatos:
        if not candidato.role:
            continue
        if candidato.role.is_super_admin:
            supervisores.append(candidato.id)
            continue
        tem_permissao = (
            db.session.query(RolePermission)
            .filter_by(role_id=candidato.role_id, modulo="avisos", acao="receber_alertas")
            .first()
        ) is not None
        if tem_permissao:
            supervisores.append(candidato.id)

    return supervisores


def _buscar_super_admins() -> list[int]:
    """Retorna IDs de funcionários ativos com role super admin (qualquer área).

    Usado por alertas que não são de uma área específica — integridade de dados
    financeiros, por exemplo — onde `_buscar_supervisores` (escopado por área)
    deixaria de fora quem de fato pode agir.

    Returns:
        Lista de IDs de funcionários super admin ativos.
    """
    from app.models.funcionario import Funcionario
    from app.models.role import Role

    return [
        f.id
        for f in db.session.query(Funcionario)
        .join(Role, Funcionario.role_id == Role.id)
        .filter(
            Funcionario.ativo == True,  # noqa: E712
            Role.ativo == True,  # noqa: E712
            Role.is_super_admin == True,  # noqa: E712
        )
        .all()
    ]
