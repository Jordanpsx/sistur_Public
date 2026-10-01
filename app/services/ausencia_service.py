# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
AusenciaService — lógica de negócio do módulo de Ausências Justificadas.

Gerencia folgas, atestados e férias que suprimem alertas automáticos de atraso/falta.
Quando uma ausência é aprovada e já existe um débito provisório no banco de horas,
o serviço o reverte automaticamente.

Auditoria obrigatória: toda mutação chama AuditService.
Arquitetura em camadas: sem imports do Flask — pure Python, Cython-compatível.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.audit import AuditService
from app.extensions import db
from app.services.base import BaseService


class AusenciaService(BaseService):
    """
    Serviço de CRUD para ausências justificadas (AusenciaJustificada).

    Todos os métodos são estáticos — sem estado de instância — garantindo
    compatibilidade com compilação via Cython e importabilidade sem efeitos colaterais.
    """

    @staticmethod
    def criar(
        funcionario_id: int,
        data: date,
        tipo: str,
        criado_por_id: int,
        observacao: str | None = None,
        aprovado: bool = False,
        ator_id: int | None = None,
    ) -> "AusenciaJustificada":  # type: ignore[name-defined]  # noqa: F821
        """Registra uma ausência justificada para um colaborador em uma data específica.

        Args:
            funcionario_id: ID do funcionário ausente.
            data:           Data da ausência (não pode estar duplicada para o mesmo funcionário).
            tipo:           Tipo da ausência ('FOLGA', 'ATESTADO' ou 'FERIAS').
            criado_por_id:  ID do admin/RH que cria o registro.
            observacao:     Observação opcional (ex: número do atestado).
            aprovado:       Se True, já entra aprovada (admin com permissão).
            ator_id:        ID do usuário para o log de auditoria.

        Returns:
            Instância de AusenciaJustificada persistida.

        Raises:
            ValueError: Se o funcionário não for encontrado ou já existir ausência na data.
        """
        from app.models.avisos import AusenciaJustificada, TipoAusencia
        from app.models.funcionario import Funcionario

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario or not funcionario.ativo:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado ou inativo.")

        # Unicidade: (funcionario_id, data)
        existente = (
            db.session.query(AusenciaJustificada)
            .filter_by(funcionario_id=funcionario_id, data=data)
            .first()
        )
        if existente:
            raise ValueError(
                f"Já existe uma ausência registrada para {funcionario.nome} "
                f"em {data.strftime('%d/%m/%Y')}."
            )

        ausencia = AusenciaJustificada(
            funcionario_id=funcionario_id,
            data=data,
            tipo=TipoAusencia(tipo),
            aprovado=aprovado,
            criado_por_id=criado_por_id,
            observacao=observacao.strip() if observacao else None,
        )
        db.session.add(ausencia)
        db.session.flush()

        AuditService.log_create(
            "ausencias_justificadas",
            entity_id=ausencia.id,
            new_state={
                "funcionario_id": funcionario_id,
                "data": data.isoformat(),
                "tipo": tipo,
                "aprovado": aprovado,
            },
            actor_id=ator_id,
        )
        db.session.commit()
        return ausencia

    @staticmethod
    def aprovar(ausencia_id: int, ator_id: int) -> "AusenciaJustificada":  # type: ignore[name-defined]  # noqa: F821
        """Aprova uma ausência justificada e reverte o débito provisório se existir.

        Se já existir um TimeDay com auto_debit_aplicado=True para o funcionário
        na data da ausência, o débito é revertido: saldo_final e saldo_calculado voltam
        a 0 e banco_horas_acumulado é ajustado pelo delta.

        Args:
            ausencia_id: PK da ausência a aprovar.
            ator_id:     ID do admin que aprova.

        Returns:
            Instância atualizada de AusenciaJustificada.

        Raises:
            ValueError: Se a ausência não for encontrada ou já estiver aprovada.
        """
        from app.models.avisos import AusenciaJustificada
        from app.models.funcionario import Funcionario
        from app.models.ponto import TimeDay

        ausencia = db.session.get(AusenciaJustificada, ausencia_id)
        if not ausencia:
            raise ValueError(f"Ausência {ausencia_id} não encontrada.")
        if ausencia.aprovado:
            return ausencia  # idempotente

        snapshot_antes = {
            "aprovado": False,
            "funcionario_id": ausencia.funcionario_id,
            "data": ausencia.data.isoformat(),
        }

        ausencia.aprovado = True
        db.session.flush()

        # Reverter débito provisório se existir
        time_day = (
            db.session.query(TimeDay)
            .filter_by(
                funcionario_id=ausencia.funcionario_id,
                shift_date=ausencia.data,
                auto_debit_aplicado=True,
            )
            .first()
        )
        if time_day:
            old_saldo = time_day.saldo_final_minutos
            time_day.saldo_final_minutos = 0
            time_day.saldo_calculado_minutos = 0
            time_day.minutos_trabalhados = 0
            time_day.auto_debit_aplicado = False

            delta = time_day.saldo_final_minutos - old_saldo  # (+) pois old era negativo
            func = db.session.get(Funcionario, ausencia.funcionario_id)
            if func:
                func.banco_horas_acumulado += delta

            AuditService.log_update(
                "ponto",
                ausencia.funcionario_id,
                previous_state={"action": "reverter_auto_debit", "saldo_anterior": old_saldo},
                new_state={"saldo_novo": 0, "auto_debit_aplicado": False, "motivo": "ausencia_aprovada"},
                actor_id=ator_id,
            )

        AuditService.log_update(
            "ausencias_justificadas",
            entity_id=ausencia_id,
            previous_state=snapshot_antes,
            new_state={"aprovado": True},
            actor_id=ator_id,
        )
        db.session.commit()
        return ausencia

    @staticmethod
    def deletar(ausencia_id: int, ator_id: int) -> None:
        """Remove uma ausência justificada (somente se não aprovada).

        Args:
            ausencia_id: PK da ausência a remover.
            ator_id:     ID do admin que executa a exclusão.

        Raises:
            ValueError: Se a ausência não for encontrada ou já estiver aprovada.
        """
        from app.models.avisos import AusenciaJustificada

        ausencia = db.session.get(AusenciaJustificada, ausencia_id)
        if not ausencia:
            raise ValueError(f"Ausência {ausencia_id} não encontrada.")
        if ausencia.aprovado:
            raise ValueError(
                "Não é possível excluir uma ausência já aprovada. "
                "Contate um administrador para reverter manualmente."
            )

        snapshot = {
            "funcionario_id": ausencia.funcionario_id,
            "data": ausencia.data.isoformat(),
            "tipo": ausencia.tipo.value,
            "aprovado": ausencia.aprovado,
        }
        AuditService.log_delete(
            "ausencias_justificadas",
            entity_id=ausencia_id,
            previous_state=snapshot,
            actor_id=ator_id,
        )
        db.session.delete(ausencia)
        db.session.commit()

    @staticmethod
    def listar(funcionario_id: int, page: int = 1, per_page: int = 20) -> Any:
        """Retorna a listagem paginada de ausências de um funcionário, da mais recente à mais antiga.

        Args:
            funcionario_id: ID do funcionário.
            page:           Página atual (base 1).
            per_page:       Itens por página.

        Returns:
            Objeto Pagination do SQLAlchemy.
        """
        from app.models.avisos import AusenciaJustificada

        return (
            db.session.query(AusenciaJustificada)
            .filter_by(funcionario_id=funcionario_id)
            .order_by(AusenciaJustificada.data.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )
