# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
AjustePontoService — lógica de negócio do sistema de ajuste self-service de ponto.

Auditoria obrigatória: toda mutação chama AuditService.
Arquitetura em camadas: sem imports do Flask — pure Python, Cython-compatível.

Fluxo de negócio
-----------------
1. Colaborador chama criar_solicitacao() → salva AjustePontoRequest(PENDENTE)
   + envia Aviso a todos os funcionários com permissão ajuste_ponto.aprovar.
2. Supervisor chama aprovar() → status → APROVADO, TimeEntry criada/editada,
   PontoService.processar_dia() recalcula o saldo, Aviso enviado ao colaborador.
3. Supervisor chama rejeitar() → status → REJEITADO, motivo salvo,
   Aviso enviado ao colaborador.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.audit import AuditService
from app.extensions import db
from app.services.base import BaseService


class AjustePontoService(BaseService):
    """
    Gerencia o ciclo de vida das solicitações de ajuste de ponto.

    Todos os métodos são estáticos — sem estado de instância — garantindo
    compatibilidade com compilação via Cython.
    """

    # -----------------------------------------------------------------------
    # Criar solicitação
    # -----------------------------------------------------------------------

    @staticmethod
    def criar_solicitacao(
        funcionario_id: int,
        data_solicitada,
        horario_solicitado: datetime,
        tipo_ponto: str,
        motivo: str,
        time_entry_id: int | None = None,
    ):
        """Cria nova solicitação de ajuste de ponto e notifica supervisores.

        O colaborador informa o horário e tipo de batida desejados.
        Se time_entry_id for fornecido, é uma solicitação de CORREÇÃO de batida
        existente. Caso contrário, é um pedido de INSERÇÃO de nova batida.

        Após salvar, envia Aviso do tipo AJUSTE_PONTO a todos os funcionários
        que possuem a permissão ajuste_ponto.aprovar.

        Args:
            funcionario_id:     ID do colaborador solicitante.
            data_solicitada:    Data do turno (date) que precisa de ajuste.
            horario_solicitado: Datetime UTC com o horário pedido pelo colaborador.
            tipo_ponto:         String com o tipo de batida (ex: 'clock_in').
            motivo:             Justificativa obrigatória (mínimo 10 caracteres).
            time_entry_id:      PK da TimeEntry a corrigir. None para nova batida.

        Returns:
            Instância persistida de AjustePontoRequest.

        Raises:
            ValueError: Se funcionario_id não for encontrado ou estiver inativo.
            ValueError: Se motivo for vazio ou muito curto (< 10 caracteres).
            ValueError: Se tipo_ponto for inválido.
        """
        from app.models.ajuste_ponto import AjustePontoRequest
        from app.models.avisos import Aviso, TipoAviso
        from app.models.funcionario import Funcionario
        from app.models.ponto import PunchType
        from app.models.role import RolePermission

        # Validações
        BaseService._require(motivo, "motivo")
        if len(motivo.strip()) < 10:
            raise ValueError("O motivo deve ter pelo menos 10 caracteres.")

        tipo_valido = [p.value for p in PunchType]
        if tipo_ponto not in tipo_valido:
            raise ValueError(f"Tipo de ponto inválido: {tipo_ponto}. Use um de: {tipo_valido}")

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario or not funcionario.ativo:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado ou inativo.")

        # Garante timezone-awareness
        if horario_solicitado.tzinfo is None:
            horario_solicitado = horario_solicitado.replace(tzinfo=timezone.utc)

        # Cria a solicitação
        req = AjustePontoRequest(
            funcionario_id=funcionario_id,
            time_entry_id=time_entry_id,
            data_solicitada=data_solicitada,
            horario_solicitado=horario_solicitado,
            tipo_ponto=tipo_ponto,
            motivo=motivo.strip(),
        )
        db.session.add(req)
        db.session.flush()  # obtém o ID antes dos avisos

        # Auditoria obrigatória — auditoria da criação
        AuditService.log_create(
            "ajuste_ponto",
            entity_id=req.id,
            new_state={
                "funcionario_id": funcionario_id,
                "data_solicitada": data_solicitada.isoformat(),
                "horario_solicitado": horario_solicitado.isoformat(),
                "tipo_ponto": tipo_ponto,
                "motivo": motivo.strip(),
                "time_entry_id": time_entry_id,
            },
            actor_id=funcionario_id,
        )
        db.session.commit()

        # Notifica todos com permissão ajuste_ponto.aprovar
        AjustePontoService._notificar_supervisores(req, funcionario)

        return req

    # -----------------------------------------------------------------------
    # Listar pendentes (para supervisores)
    # -----------------------------------------------------------------------

    @staticmethod
    def listar_pendentes() -> list:
        """Retorna todas as solicitações com status PENDENTE ordenadas por criacao.

        Usado para popular a tab 'Aprovações' no módulo Avisos.

        Returns:
            Lista de AjustePontoRequest com status PENDENTE,
            ordem crescente por criado_em (mais antigas primeiro).
        """
        from app.models.ajuste_ponto import AjustePontoRequest, StatusAjuste

        return (
            db.session.query(AjustePontoRequest)
            .filter_by(status=StatusAjuste.PENDENTE)
            .order_by(AjustePontoRequest.criado_em)
            .all()
        )

    # -----------------------------------------------------------------------
    # Listar por funcionário (para "Minhas Solicitações")
    # -----------------------------------------------------------------------

    @staticmethod
    def listar_por_funcionario(funcionario_id: int, limit: int = 30) -> list:
        """Retorna o histórico de solicitações do colaborador.

        Ordem decrescente por criado_em (mais recentes primeiro).

        Args:
            funcionario_id: PK do colaborador.
            limit:          Número máximo de registros a retornar. Padrão: 30.

        Returns:
            Lista de AjustePontoRequest do colaborador.
        """
        from app.models.ajuste_ponto import AjustePontoRequest

        return (
            db.session.query(AjustePontoRequest)
            .filter_by(funcionario_id=funcionario_id)
            .order_by(AjustePontoRequest.criado_em.desc())
            .limit(limit)
            .all()
        )

    # -----------------------------------------------------------------------
    # Contar pendentes (para o badge da sineta)
    # -----------------------------------------------------------------------

    @staticmethod
    def contar_pendentes() -> int:
        """Retorna o número total de solicitações com status PENDENTE.

        Usado pelo context processor para exibir badge no sino dos supervisores.

        Returns:
            Contagem de AjustePontoRequest PENDENTE.
        """
        from app.models.ajuste_ponto import AjustePontoRequest, StatusAjuste

        return (
            db.session.query(AjustePontoRequest)
            .filter_by(status=StatusAjuste.PENDENTE)
            .count()
        )

    # -----------------------------------------------------------------------
    # Aprovar
    # -----------------------------------------------------------------------

    @staticmethod
    def aprovar(ajuste_id: int, supervisor_id: int):
        """Aprova uma solicitação de ajuste e aplica a batida de ponto correspondente.

        Ao aprovar:
        - Altera status para APROVADO e registra supervisor_id e resolvido_em.
        - Se time_entry_id preenchido: edita a TimeEntry existente via PontoService.editar_batida_admin().
        - Se time_entry_id é None: cria nova TimeEntry via PontoService.registrar_batida() com source='admin'.
        - Em ambos os casos, PontoService.processar_dia() recalcula o TimeDay.
        - Envia Aviso de aprovação ao colaborador.

        Args:
            ajuste_id:    PK da AjustePontoRequest a aprovar.
            supervisor_id: ID do funcionário que está aprovando (deve ter ajuste_ponto.aprovar).

        Returns:
            Instância atualizada de AjustePontoRequest.

        Raises:
            ValueError: Se a solicitação não for encontrada.
            ValueError: Se a solicitação já estiver resolvida (não PENDENTE).
            ValueError: Se o supervisor não existir.
        """
        from app.models.ajuste_ponto import AjustePontoRequest, StatusAjuste
        from app.models.avisos import Aviso, TipoAviso
        from app.models.funcionario import Funcionario
        from app.services.ponto_service import PontoService

        req = db.session.get(AjustePontoRequest, ajuste_id)
        if not req:
            raise ValueError(f"Solicitação de ajuste {ajuste_id} não encontrada.")

        if req.status != StatusAjuste.PENDENTE:
            raise ValueError(AjustePontoService._msg_ja_resolvida(req))

        supervisor = db.session.get(Funcionario, supervisor_id)
        if not supervisor or not supervisor.ativo:
            raise ValueError(f"Supervisor {supervisor_id} não encontrado ou inativo.")

        snapshot_antes = {
            "status": req.status.value,
            "supervisor_id": None,
            "resolvido_em": None,
        }

        # Aplica a batida de ponto
        if req.time_entry_id:
            # Correção de batida existente
            PontoService.editar_batida_admin(
                time_entry_id=req.time_entry_id,
                novo_horario=req.horario_solicitado,
                motivo=f"Ajuste aprovado — solicitação #{req.id}: {req.motivo}",
                ator_id=supervisor_id,
            )
        else:
            # Inserção de nova batida
            from app.models.ponto import PunchType
            PontoService.registrar_batida(
                funcionario_id=req.funcionario_id,
                punch_time=req.horario_solicitado,
                source="admin",
                ator_id=supervisor_id,
            )

        # Atualiza a solicitação
        req.status = StatusAjuste.APROVADO
        req.supervisor_id = supervisor_id
        req.resolvido_em = datetime.now(timezone.utc)
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "ajuste_ponto",
            entity_id=req.id,
            previous_state=snapshot_antes,
            new_state={
                "status": StatusAjuste.APROVADO.value,
                "supervisor_id": supervisor_id,
                "resolvido_em": req.resolvido_em.isoformat(),
            },
            actor_id=supervisor_id,
        )
        db.session.commit()

        # 2.11 — Encerra o aviso pendente dos demais aprovadores e informa o desfecho.
        colaborador = db.session.get(Funcionario, req.funcionario_id)
        from app.services.aviso_service import AvisoService
        AvisoService.encerrar_por_referencia(
            "ajuste_ponto", req.id,
            ator_id=supervisor_id,
            desfecho_titulo="Pedido de ajuste resolvido",
            desfecho_mensagem=(
                f"A solicitação de ajuste de {colaborador.nome} "
                f"({req.data_solicitada.strftime('%d/%m/%Y')}) foi aprovada por {supervisor.nome}."
            ),
            excluir_destinatarios={req.funcionario_id},
        )

        # Notifica o colaborador
        aviso = Aviso(
            destinatario_id=req.funcionario_id,
            remetente_id=supervisor_id,
            titulo="✅ Ajuste de ponto aprovado",
            mensagem=(
                f"Sua solicitação de ajuste para {req.data_solicitada.strftime('%d/%m/%Y')} "
                f"({req.tipo_ponto.replace('_', ' ').title()} — "
                f"{req.horario_solicitado.strftime('%H:%M')}) foi aprovada por {supervisor.nome}."
            ),
            tipo=TipoAviso.AJUSTE_PONTO,
            ajuste_ponto_id=req.id,
        )
        db.session.add(aviso)
        db.session.commit()

        return req

    # -----------------------------------------------------------------------
    # Rejeitar
    # -----------------------------------------------------------------------

    @staticmethod
    def rejeitar(ajuste_id: int, supervisor_id: int, motivo_rejeicao: str):
        """Rejeita uma solicitação de ajuste com motivo obrigatório.

        Altera status para REJEITADO, salva motivo_rejeicao e envia Aviso ao colaborador.

        Args:
            ajuste_id:        PK da AjustePontoRequest a rejeitar.
            supervisor_id:    ID do funcionário que está rejeitando.
            motivo_rejeicao:  Motivo da rejeição — obrigatório e não vazio.

        Returns:
            Instância atualizada de AjustePontoRequest.

        Raises:
            ValueError: Se a solicitação não for encontrada.
            ValueError: Se a solicitação já estiver resolvida.
            ValueError: Se motivo_rejeicao for vazio.
        """
        from app.models.ajuste_ponto import AjustePontoRequest, StatusAjuste
        from app.models.avisos import Aviso, TipoAviso
        from app.models.funcionario import Funcionario

        BaseService._require(motivo_rejeicao, "motivo_rejeicao")

        req = db.session.get(AjustePontoRequest, ajuste_id)
        if not req:
            raise ValueError(f"Solicitação de ajuste {ajuste_id} não encontrada.")

        if req.status != StatusAjuste.PENDENTE:
            raise ValueError(AjustePontoService._msg_ja_resolvida(req))

        supervisor = db.session.get(Funcionario, supervisor_id)
        if not supervisor or not supervisor.ativo:
            raise ValueError(f"Supervisor {supervisor_id} não encontrado ou inativo.")

        snapshot_antes = {"status": req.status.value}

        req.status = StatusAjuste.REJEITADO
        req.supervisor_id = supervisor_id
        req.motivo_rejeicao = motivo_rejeicao.strip()
        req.resolvido_em = datetime.now(timezone.utc)
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "ajuste_ponto",
            entity_id=req.id,
            previous_state=snapshot_antes,
            new_state={
                "status": StatusAjuste.REJEITADO.value,
                "supervisor_id": supervisor_id,
                "motivo_rejeicao": motivo_rejeicao.strip(),
            },
            actor_id=supervisor_id,
        )
        db.session.commit()

        # 2.11 — Encerra o aviso pendente dos demais aprovadores e informa o desfecho.
        colaborador = db.session.get(Funcionario, req.funcionario_id)
        from app.services.aviso_service import AvisoService
        AvisoService.encerrar_por_referencia(
            "ajuste_ponto", req.id,
            ator_id=supervisor_id,
            desfecho_titulo="Pedido de ajuste resolvido",
            desfecho_mensagem=(
                f"A solicitação de ajuste de {colaborador.nome} "
                f"({req.data_solicitada.strftime('%d/%m/%Y')}) foi rejeitada por {supervisor.nome}."
            ),
            excluir_destinatarios={req.funcionario_id},
        )

        # Notifica o colaborador
        aviso = Aviso(
            destinatario_id=req.funcionario_id,
            remetente_id=supervisor_id,
            titulo="❌ Ajuste de ponto não aprovado",
            mensagem=(
                f"Sua solicitação de ajuste para {req.data_solicitada.strftime('%d/%m/%Y')} "
                f"foi rejeitada por {supervisor.nome}. "
                f"Motivo: {motivo_rejeicao.strip()}"
            ),
            tipo=TipoAviso.AJUSTE_PONTO,
            ajuste_ponto_id=req.id,
        )
        db.session.add(aviso)
        db.session.commit()

        return req

    # -----------------------------------------------------------------------
    # Helpers privados
    # -----------------------------------------------------------------------

    @staticmethod
    def _msg_ja_resolvida(req) -> str:
        """Mensagem amigável de concorrência: quem resolveu primeiro vence (2.11)."""
        from app.models.funcionario import Funcionario

        resolvedor = db.session.get(Funcionario, req.supervisor_id) if req.supervisor_id else None
        quem = f" por {resolvedor.nome}" if resolvedor else ""
        acao = "aprovada" if req.status.value == "APROVADO" else "rejeitada"
        return f"Esta solicitação já foi {acao}{quem}."

    @staticmethod
    def _notificar_supervisores(req, funcionario_solicitante):
        """Envia Aviso do tipo AJUSTE_PONTO a todos com permissão ajuste_ponto.aprovar.

        Itera sobre todos os funcionários que possuem a RolePermission
        modulo='ajuste_ponto', acao='aprovar' e cria um Aviso para cada um.

        Args:
            req:                    Instância de AjustePontoRequest recém-criada.
            funcionario_solicitante: Instância de Funcionario do colaborador solicitante.
        """
        from app.models.avisos import Aviso, TipoAviso
        from app.models.funcionario import Funcionario
        from app.models.role import RolePermission

        # Encontra todos os roles com a permissão
        roles_com_permissao = (
            db.session.query(RolePermission.role_id)
            .filter_by(modulo="ajuste_ponto", acao="aprovar")
            .subquery()
        )

        # Encontra todos os funcionários ativos com esses roles
        supervisores = (
            db.session.query(Funcionario)
            .filter(
                Funcionario.ativo == True,
                Funcionario.role_id.in_(roles_com_permissao),
                Funcionario.id != req.funcionario_id,  # não notifica o próprio solicitante
            )
            .all()
        )

        # Também inclui super admins (que têm acesso irrestrito)
        from app.models.role import Role
        super_admins = (
            db.session.query(Funcionario)
            .join(Role, Funcionario.role_id == Role.id)
            .filter(
                Funcionario.ativo == True,
                Role.is_super_admin == True,
                Funcionario.id != req.funcionario_id,
            )
            .all()
        )

        destinatarios_ids = {f.id for f in supervisores} | {f.id for f in super_admins}

        tipo_label = {
            "clock_in": "Entrada",
            "lunch_start": "Saída Almoço",
            "lunch_end": "Retorno Almoço",
            "clock_out": "Saída",
            "extra": "Extra",
        }.get(req.tipo_ponto, req.tipo_ponto)

        for dest_id in destinatarios_ids:
            aviso = Aviso(
                destinatario_id=dest_id,
                remetente_id=req.funcionario_id,
                titulo=f"📋 Pedido de ajuste — {funcionario_solicitante.nome.split()[0]}",
                mensagem=(
                    f"{funcionario_solicitante.nome} solicitou ajuste de ponto:\n"
                    f"Data: {req.data_solicitada.strftime('%d/%m/%Y')} | "
                    f"Tipo: {tipo_label} | "
                    f"Horário: {req.horario_solicitado.strftime('%H:%M')}\n"
                    f"Motivo: {req.motivo}"
                ),
                tipo=TipoAviso.AJUSTE_PONTO,
                ajuste_ponto_id=req.id,
                # Vínculo genérico (2.11): permite encerrar estes avisos em
                # todos os aprovadores quando um deles resolver.
                referencia_tipo="ajuste_ponto",
                referencia_id=req.id,
            )
            db.session.add(aviso)

        db.session.commit()
