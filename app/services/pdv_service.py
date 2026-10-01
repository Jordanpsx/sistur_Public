# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
PdvService — lógica de negócio do módulo PDV (Ponto de Venda de balcão).

Regras de arquitetura (ver PDV_PROJECT_PLAN.md):
- NUNCA toca ReservaResource/ReservaItemLink diretamente. Toda mutação de
  estoque passa por ReservaService.criar()/atualizar(), herdando o lock
  pessimista (SELECT ... FOR UPDATE) de _verificar_estoque().
- Idempotência via client_sale_uuid: reenvio da fila offline é no-op.
- CASH/EXTERNAL_CARD_TERMINAL/EXTERNAL_PIX → Reserva nasce PAID (dinheiro/pix/
  cartão já recebido pelo operador fora do sistema — não há o que aguardar);
  MP_PIX/MP_CARD → PENDING (o sistema gera a cobrança e aguarda o webhook
  Mercado Pago existente);
  COMANDA → itens anexados à reserva do hóspede, pagos no check-out.
- Zero imports de Flask (Cython-ready).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.core.audit import AuditService
from app.core.dados_sensiveis import mascarar_documento
from app.core.models import AuditAction
from app.core.permissions import has_permission
from app.extensions import db
from app.models.funcionario import Funcionario, validar_cpf
from app.models.pdv import (
    PdvChangeSource,
    PdvHeldCart,
    PdvMovimentacaoCaixa,
    PdvOfflineSyncLog,
    PdvPaymentMethod,
    PdvSale,
    PdvSaleType,
    PdvShift,
    PdvShiftStatus,
    PdvSyncOutcome,
    TipoMovimentacaoCaixa,
)
from app.models.reservas import (
    Reserva,
    ReservaSource,
    ReservaStatus,
    ReservaStatusConfig,
)
from app.services.base import BaseService
from app.services.reserva_pricing_service import ReservaPricingService
from app.services.reserva_service import OverbookingError, ReservaService

_MODULE = "pdv"

_SHIFT_SNAPSHOT_FIELDS = [
    "id", "source_id", "operador_id",
    "opening_float_amount", "opening_float_pix",
    "closing_counted_amount", "closing_expected_amount",
    "closing_counted_pix", "closing_expected_pix",
    "status", "discrepancy_reason", "approved_by_id", "approved_at",
    "opened_at", "closed_at",
]


class QuebraDeCaixaError(ValueError):
    """Fechamento com contado ≠ esperado e sem justificativa do operador.

    Carrega os valores das duas fontes para a UI exibir a divergência e
    coletar o motivo obrigatório antes de reenviar.
    """

    def __init__(self, message: str, detalhe: dict) -> None:
        super().__init__(message)
        self.detalhe = detalhe

_SALE_SNAPSHOT_FIELDS = [
    "id", "shift_id", "reserva_id", "client_sale_uuid", "sale_type",
    "payment_method", "amount_total", "amount_tendered", "amount_change",
    "change_source", "synced_offline",
]


class PdvService(BaseService):
    """Serviço de turnos de caixa e vendas de balcão do PDV."""

    # ------------------------------------------------------------------
    # Turnos (caixa)
    # ------------------------------------------------------------------

    @classmethod
    def abrir_turno(
        cls,
        source_id: int,
        operador_id: int,
        opening_float_amount: float = 0.0,
        opening_float_pix: float = 0.0,
        *,
        ator_id: int | None = None,
    ) -> PdvShift:
        """Abre um turno de caixa declarando as DUAS fontes de troco.

        Args:
            source_id: ID da ReservaSource (venue) onde o caixa opera.
            operador_id: ID do Funcionario operador do caixa.
            opening_float_amount: Fundo de troco físico inicial na gaveta (R$).
            opening_float_pix: Fundo de troco digital na conta PIX do caixa (R$).
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            PdvShift persistido com status OPEN.

        Raises:
            ValueError: Se o venue não existir, o valor de fundo for negativo
                ou já houver turno OPEN para este operador neste venue.
        """
        source = db.session.query(ReservaSource).filter(
            ReservaSource.id == source_id,
            ReservaSource.deleted_at.is_(None),
        ).first()
        if not source:
            raise ValueError(f"Venue {source_id} não encontrado.")

        if opening_float_amount is None or float(opening_float_amount) < 0:
            raise ValueError("Fundo de troco físico não pode ser negativo.")
        if opening_float_pix is None or float(opening_float_pix) < 0:
            raise ValueError("Fundo de troco PIX não pode ser negativo.")

        aberto = db.session.query(PdvShift).filter(
            PdvShift.source_id == source_id,
            PdvShift.operador_id == operador_id,
            PdvShift.status.in_([PdvShiftStatus.OPEN, PdvShiftStatus.PENDING_APPROVAL]),
            PdvShift.deleted_at.is_(None),
        ).first()
        if aberto:
            estado = "aberto" if aberto.status == PdvShiftStatus.OPEN else "aguardando aprovação"
            raise ValueError(
                f"Operador já possui turno {estado} (#{aberto.id}) neste venue. "
                "Resolva o turno atual antes de abrir outro."
            )

        shift = PdvShift(
            source_id=source_id,
            operador_id=operador_id,
            opening_float_amount=Decimal(str(opening_float_amount)),
            opening_float_pix=Decimal(str(opening_float_pix)),
        )
        db.session.add(shift)
        db.session.flush()

        AuditService.log(
            AuditAction.create,
            _MODULE,
            entity_id=shift.id,
            new_state=cls._snapshot(shift, _SHIFT_SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return shift

    @classmethod
    def _esperados_fechamento(cls, shift: PdvShift) -> tuple[Decimal, Decimal]:
        """Calcula o esperado nas duas fontes de troco ao fechar o turno.

        Física (gaveta):
          fundo físico
          + total das vendas CASH cujo troco saiu da gaveta (entra tendered,
            sai o troco → líquido = amount_total)
          + tendered integral das vendas CASH cujo troco saiu via PIX
            (a gaveta recebe tudo e não devolve nada).

        Digital (fundo PIX de troco):
          fundo PIX
          − troco devolvido via PIX nas vendas CASH com change_source=PIX.

        **Fundo estritamente decrescente, de propósito** — não é "a conta onde
        o pix do estabelecimento entra", é a reserva fixa usada só para dar
        troco em pix. Nunca cresce durante o turno; só existe pra diminuir.
        Vendas EXTERNAL_PIX (pix recebido direto do cliente) **não entram
        aqui** — o dinheiro vai para a conta bancária/pix do estabelecimento,
        fora do que este método concilia. Elas ficam visíveis no relatório do
        turno (`relatorio_turno`, agrupado por `payment_method`), não numa
        conferência de "contagem física" que não existe pra pix recebido.

        Vendas MP_PIX/MP_CARD liquidam na conta Mercado Pago — não passam
        por nenhuma das duas fontes de troco.

        Movimentações manuais de caixa (`PdvMovimentacaoCaixa` — sangria e
        suprimento) entram só na gaveta física, nunca no fundo PIX:
        SUPRIMENTO soma, TRANSFERENCIA_INTERNA/RECOLHIMENTO_COFRE/
        PAGAMENTO_DESPESA subtraem. Reaproveitado por
        `MovimentacaoCaixaService.calcular_saldo_esperado_gaveta`, que delega
        inteiramente aqui — uma fórmula usada nos dois lugares (fechamento de
        turno e conferência avulsa) não tem como divergir de si mesma.
        """
        esperado_cash = Decimal(shift.opening_float_amount or 0)
        esperado_pix = Decimal(shift.opening_float_pix or 0)

        vendas_cash = db.session.query(PdvSale).filter(
            PdvSale.shift_id == shift.id,
            PdvSale.payment_method == PdvPaymentMethod.CASH,
            PdvSale.deleted_at.is_(None),
        ).all()
        for venda in vendas_cash:
            troco = Decimal(venda.amount_change or 0)
            if venda.change_source == PdvChangeSource.PIX and troco > 0:
                esperado_cash += Decimal(venda.amount_tendered or venda.amount_total)
                esperado_pix -= troco
            else:
                esperado_cash += Decimal(venda.amount_total or 0)

        movimentacoes = db.session.query(PdvMovimentacaoCaixa).filter(
            PdvMovimentacaoCaixa.caixa_turno_id == shift.id,
            PdvMovimentacaoCaixa.deleted_at.is_(None),
        ).all()
        for mov in movimentacoes:
            valor = Decimal(mov.valor)
            if mov.tipo_movimentacao == TipoMovimentacaoCaixa.SUPRIMENTO:
                esperado_cash += valor
            else:
                esperado_cash -= valor

        return esperado_cash, esperado_pix

    @classmethod
    def fechar_turno(
        cls,
        shift_id: int,
        closing_counted_amount: float,
        closing_counted_pix: float = 0.0,
        *,
        discrepancy_reason: str | None = None,
        ator_id: int | None = None,
    ) -> PdvShift:
        """Fecha um turno conferindo as DUAS fontes de troco (física + PIX).

        Fluxo:
        - Contado == esperado nas duas fontes → status CLOSED direto.
        - Divergência em qualquer fonte (quebra de caixa):
          * Sem motivo → levanta QuebraDeCaixaError com o detalhe das duas
            fontes (a UI exibe e coleta o motivo obrigatório).
          * Com motivo (≥10 chars) → status PENDING_APPROVAL, notifica os
            supervisores (Avisos) e aguarda aprovar_fechamento().

        Args:
            shift_id: ID do turno a fechar.
            closing_counted_amount: Dinheiro físico contado na gaveta.
            closing_counted_pix: Saldo contado na conta PIX de troco.
            discrepancy_reason: Relato do operador sobre a quebra (obrigatório
                quando houver divergência).
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            PdvShift atualizado (CLOSED ou PENDING_APPROVAL).

        Raises:
            ValueError: Turno não encontrado / já fechado / aguardando aprovação.
            QuebraDeCaixaError: Divergência sem motivo informado.
        """
        shift = db.session.query(PdvShift).filter(
            PdvShift.id == shift_id,
            PdvShift.deleted_at.is_(None),
        ).first()
        if not shift:
            raise ValueError("Turno não encontrado.")
        if shift.status == PdvShiftStatus.CLOSED:
            raise ValueError("Turno já está fechado.")
        if shift.status == PdvShiftStatus.PENDING_APPROVAL:
            raise ValueError(
                "Turno já aguarda aprovação de supervisor para fechar."
            )

        previous = cls._snapshot(shift, _SHIFT_SNAPSHOT_FIELDS)

        esperado_cash, esperado_pix = cls._esperados_fechamento(shift)
        contado_cash = Decimal(str(closing_counted_amount)).quantize(Decimal("0.01"))
        contado_pix = Decimal(str(closing_counted_pix or 0)).quantize(Decimal("0.01"))
        if contado_cash < 0:
            raise ValueError("Dinheiro contado na gaveta não pode ser negativo.")
        if contado_pix < 0:
            raise ValueError("Saldo PIX contado não pode ser negativo.")
        diff_cash = contado_cash - esperado_cash
        diff_pix = contado_pix - esperado_pix

        shift.closing_expected_amount = esperado_cash
        shift.closing_counted_amount = contado_cash
        shift.closing_expected_pix = esperado_pix
        shift.closing_counted_pix = contado_pix

        coerente = diff_cash == 0 and diff_pix == 0
        detalhe = {
            "expected_cash": float(esperado_cash),
            "counted_cash": float(contado_cash),
            "diff_cash": float(diff_cash),
            "expected_pix": float(esperado_pix),
            "counted_pix": float(contado_pix),
            "diff_pix": float(diff_pix),
        }

        if coerente:
            shift.status = PdvShiftStatus.CLOSED
            shift.closed_at = datetime.now(timezone.utc)
        else:
            motivo = (discrepancy_reason or "").strip()
            if len(motivo) < 10:
                db.session.rollback()
                raise QuebraDeCaixaError(
                    "Quebra de caixa: contado difere do esperado. "
                    "Informe o motivo (mínimo 10 caracteres) para enviar à aprovação.",
                    detalhe,
                )
            shift.status = PdvShiftStatus.PENDING_APPROVAL
            shift.discrepancy_reason = motivo
            cls._notificar_supervisores_quebra(shift, detalhe, motivo)

        AuditService.log(
            AuditAction.update,
            _MODULE,
            entity_id=shift.id,
            previous_state=previous,
            new_state=cls._snapshot(shift, _SHIFT_SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return shift

    @classmethod
    def aprovar_fechamento(
        cls,
        shift_id: int,
        aprovador_id: int,
        *,
        ator_id: int | None = None,
    ) -> PdvShift:
        """Aprova o fechamento de um turno com quebra de caixa.

        Regras:
        - Turno deve estar PENDING_APPROVAL.
        - O aprovador não pode ser o próprio operador do turno (segregação
          de funções — a permissão pdv.manage_shift é checada na rota).

        Args:
            shift_id: ID do turno aguardando aprovação.
            aprovador_id: ID do Funcionario supervisor que aprova.
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            PdvShift fechado (CLOSED, approved_by/approved_at preenchidos).

        Raises:
            ValueError: Turno não encontrado, não pendente, ou auto-aprovação.
        """
        shift = db.session.query(PdvShift).filter(
            PdvShift.id == shift_id,
            PdvShift.deleted_at.is_(None),
        ).first()
        if not shift:
            raise ValueError("Turno não encontrado.")
        if shift.status != PdvShiftStatus.PENDING_APPROVAL:
            raise ValueError("Turno não está aguardando aprovação.")
        if aprovador_id == shift.operador_id:
            raise ValueError(
                "O operador do turno não pode aprovar a própria quebra de caixa."
            )

        previous = cls._snapshot(shift, _SHIFT_SNAPSHOT_FIELDS)
        shift.status = PdvShiftStatus.CLOSED
        shift.approved_by_id = aprovador_id
        shift.approved_at = datetime.now(timezone.utc)
        shift.closed_at = shift.approved_at

        AuditService.log(
            AuditAction.update,
            _MODULE,
            entity_id=shift.id,
            previous_state=previous,
            new_state=cls._snapshot(shift, _SHIFT_SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return shift

    @classmethod
    def listar_turnos_pendentes(cls) -> list[dict]:
        """Lista turnos em PENDING_APPROVAL para a tela de reconciliação."""
        shifts = db.session.query(PdvShift).filter(
            PdvShift.status == PdvShiftStatus.PENDING_APPROVAL,
            PdvShift.deleted_at.is_(None),
        ).order_by(PdvShift.opened_at.asc()).all()
        out = []
        for s in shifts:
            snap = cls._snapshot(s, _SHIFT_SNAPSHOT_FIELDS)
            snap["operador_nome"] = s.operador.nome if s.operador else None
            snap["source_name"] = s.source.name if s.source else None
            snap["diff_cash"] = float(
                Decimal(s.closing_counted_amount or 0) - Decimal(s.closing_expected_amount or 0)
            )
            snap["diff_pix"] = float(
                Decimal(s.closing_counted_pix or 0) - Decimal(s.closing_expected_pix or 0)
            )
            out.append(snap)
        return out

    @classmethod
    def _notificar_supervisores_quebra(
        cls, shift: PdvShift, detalhe: dict, motivo: str
    ) -> None:
        """Cria Avisos para supervisores pedindo aprovação da quebra de caixa."""
        from app.models.avisos import TipoAviso
        from app.models.funcionario import Funcionario as _Func
        from app.models.role import Role, RolePermission
        from app.services.aviso_service import AvisoService

        roles_gerentes = {
            rp.role_id
            for rp in db.session.query(RolePermission).filter_by(
                modulo="pdv", acao="manage_shift",
            )
        }
        super_roles = {
            r.id
            for r in db.session.query(Role).filter_by(is_super_admin=True, ativo=True)
        }
        destinatarios = db.session.query(_Func).filter(
            _Func.ativo.is_(True),
            _Func.role_id.in_(roles_gerentes | super_roles) if (roles_gerentes | super_roles) else False,
            _Func.id != shift.operador_id,
        ).all()

        operador_nome = shift.operador.nome if shift.operador else f"#{shift.operador_id}"
        titulo = f"Quebra de caixa — turno #{shift.id} aguarda aprovação"
        mensagem = (
            f"O turno #{shift.id} ({operador_nome}) fechou com divergência: "
            f"físico {detalhe['diff_cash']:+.2f} (contado {detalhe['counted_cash']:.2f} / "
            f"esperado {detalhe['expected_cash']:.2f}); "
            f"PIX {detalhe['diff_pix']:+.2f} (contado {detalhe['counted_pix']:.2f} / "
            f"esperado {detalhe['expected_pix']:.2f}). "
            f"Motivo do operador: \"{motivo}\". "
            "Aprove ou trate em Reservas → Reconciliação PDV."
        )
        for destinatario in destinatarios:
            try:
                AvisoService.criar(
                    destinatario_id=destinatario.id,
                    titulo=titulo,
                    mensagem=mensagem,
                    tipo=TipoAviso.SISTEMA,
                    remetente_id=shift.operador_id,
                    ator_id=shift.operador_id,
                )
            except Exception:  # noqa: BLE001 — aviso é best-effort, não pode travar o fechamento
                continue

    @classmethod
    def turno_atual(cls, operador_id: int) -> dict | None:
        """Retorna o turno OPEN mais recente do operador, ou None.

        Usado pela SPA para restaurar a sessão de caixa após F5/crash sem
        exigir que o operador reabra o turno.

        Args:
            operador_id: ID do Funcionario operador.

        Returns:
            Dict {shift_id, source_id, source_name, opening_float_amount,
            opened_at} ou None se não houver turno aberto.
        """
        shift = db.session.query(PdvShift).filter(
            PdvShift.operador_id == operador_id,
            PdvShift.status == PdvShiftStatus.OPEN,
            PdvShift.deleted_at.is_(None),
        ).order_by(PdvShift.opened_at.desc()).first()
        if not shift:
            return None
        return {
            "shift_id": shift.id,
            "source_id": shift.source_id,
            "source_name": shift.source.name if shift.source else None,
            "opening_float_amount": float(shift.opening_float_amount or 0),
            "opened_at": shift.opened_at.isoformat(),
        }

    @staticmethod
    def listar_venues() -> list[dict]:
        """Lista venues ativos para o seletor de abertura de turno.

        Returns:
            Lista de dicts {id, name} de ReservaSource ativas, por nome.
        """
        sources = db.session.query(ReservaSource).filter(
            ReservaSource.deleted_at.is_(None),
            ReservaSource.is_active == True,  # noqa: E712
        ).order_by(ReservaSource.name).all()
        return [{"id": s.id, "name": s.name} for s in sources]

    @classmethod
    def relatorio_turno(cls, shift_id: int) -> dict:
        """Relatório de vendas do turno agrupado por método de pagamento.

        Args:
            shift_id: ID do turno.

        Returns:
            Dict com shift (dados básicos), totals_by_method (lista de
            {method, count, total}) e grand_total.

        Raises:
            ValueError: Se o turno não for encontrado.
        """
        shift = db.session.query(PdvShift).filter(
            PdvShift.id == shift_id,
            PdvShift.deleted_at.is_(None),
        ).first()
        if not shift:
            raise ValueError("Turno não encontrado.")

        rows = db.session.query(
            PdvSale.payment_method,
            db.func.count(PdvSale.id),
            db.func.coalesce(db.func.sum(PdvSale.amount_total), 0),
        ).filter(
            PdvSale.shift_id == shift.id,
            PdvSale.deleted_at.is_(None),
        ).group_by(PdvSale.payment_method).all()

        totals = [
            {
                "method": method.value if hasattr(method, "value") else str(method),
                "count": int(count),
                "total": float(total),
            }
            for method, count, total in rows
        ]

        return {
            "shift": cls._snapshot(shift, _SHIFT_SNAPSHOT_FIELDS),
            "totals_by_method": totals,
            "grand_total": float(sum(t["total"] for t in totals)),
            "sales_count": int(sum(t["count"] for t in totals)),
        }

    @classmethod
    def listar_sync_logs(
        cls,
        *,
        outcome: str | PdvSyncOutcome | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Lista tentativas de sincronização offline para reconciliação gerencial.

        O retorno evita expor o payload bruto; a tela gerencial precisa de
        status, identificadores e um resumo operacional da venda.
        """
        try:
            safe_limit = max(1, min(int(limit or 100), 200))
        except (TypeError, ValueError):
            safe_limit = 100

        query = db.session.query(PdvOfflineSyncLog)
        if outcome:
            try:
                parsed_outcome = (
                    outcome if isinstance(outcome, PdvSyncOutcome)
                    else PdvSyncOutcome(str(outcome))
                )
            except ValueError as exc:
                raise ValueError("Outcome de sync inválido.") from exc
            query = query.filter(PdvOfflineSyncLog.outcome == parsed_outcome)

        logs = query.order_by(
            PdvOfflineSyncLog.processed_at.desc(),
            PdvOfflineSyncLog.id.desc(),
        ).limit(safe_limit).all()
        return [cls._snapshot_sync_log(log) for log in logs]

    @classmethod
    def autorizar_manager_override(
        cls,
        *,
        cpf: str,
        senha: str,
        reason: str,
    ) -> dict:
        """Valida credenciais de gerente para um override pontual do PDV."""
        from werkzeug.security import check_password_hash

        motivo = (reason or "").strip()
        if len(motivo) < 10:
            raise ValueError("Informe um motivo com pelo menos 10 caracteres.")
        if not senha:
            raise ValueError("Senha do gerente é obrigatória.")

        cpf_limpo = validar_cpf(cpf or "")
        gerente = db.session.query(Funcionario).filter(
            Funcionario.cpf == cpf_limpo,
            Funcionario.ativo.is_(True),
        ).first()
        if not gerente or not gerente.senha_hash:
            raise ValueError("Credenciais de gerente inválidas.")
        if not check_password_hash(gerente.senha_hash, senha):
            raise ValueError("Credenciais de gerente inválidas.")
        if not has_permission(gerente.id, "pdv", "manage_shift"):
            raise ValueError("Funcionário sem permissão gerencial para override PDV.")

        return {
            "manager_id": gerente.id,
            "manager_name": gerente.nome,
            "reason": motivo,
        }

    # ------------------------------------------------------------------
    # Vendas
    # ------------------------------------------------------------------

    @classmethod
    def registrar_venda(
        cls,
        *,
        client_sale_uuid: str,
        shift_id: int,
        source_name: str,
        category_name: str,
        items: list[dict],
        payment_method: str | PdvPaymentMethod,
        customer_name: str | None = None,
        customer_document: str | None = None,
        amount_tendered: float | None = None,
        change_source: str | PdvChangeSource = PdvChangeSource.CASH,
        manual_discount_percent: float = 0.0,
        force_overbooking: bool = False,
        manager_override: dict | None = None,
        offline_override_note: dict | None = None,
        synced_offline: bool = False,
        device_id: str | None = None,
        pdv_default_status: str | None = None,
        ator_id: int | None = None,
    ) -> dict:
        """Registra uma venda avulsa de balcão (cria Reserva + PdvSale).

        Fluxo:
        1. Idempotência: se já existe PdvSale com este client_sale_uuid,
           retorna o resultado original sem criar nada (reenvio offline).
        2. Preço autoritativo via ReservaPricingService.calculate() —
           o total do cliente é apenas preview, nunca é confiado.
        3. ReservaService.criar(origin=BALCAO) — herda o lock pessimista
           de estoque; nenhum bypass.
        4. CASH/EXTERNAL_CARD_TERMINAL/EXTERNAL_PIX → status PAID imediato
           (dinheiro/pix/cartão já recebido pelo operador fora do sistema).
           MP_PIX/MP_CARD → permanece PENDING: aí sim o sistema gera a
           cobrança e aguarda o fluxo webhook Mercado Pago existente.
        5. PdvSale gravada com troco calculado; tentativa registrada em
           PdvOfflineSyncLog quando device_id é fornecido.

        Args:
            client_sale_uuid: UUID v4 gerado no cliente — chave de idempotência.
            shift_id: ID do turno OPEN do operador.
            source_name: Nome do venue (ReservaSource).
            category_name: Categoria primária da venda (categoria do 1º item).
            items: Lista de dicts {"item_id", "quantity", "resource_id"?}.
            payment_method: CASH | EXTERNAL_CARD_TERMINAL | EXTERNAL_PIX |
                MP_PIX | MP_CARD.
            customer_name: Nome do cliente (opcional — walk-in gera "Balcão N").
            customer_document: CPF/CNPJ (opcional).
            amount_tendered: Valor entregue em dinheiro (para cálculo de troco).
            change_source: Fonte do troco em vendas CASH ("CASH" = gaveta
                física, "PIX" = fundo digital do caixa). Afeta o esperado
                das duas fontes na conferência de fechamento.
            manual_discount_percent: Desconto manual do operador (0-100).
            force_overbooking: Override gerencial de conflito de estoque.
            manager_override: Dict retornado por autorizar_manager_override().
            offline_override_note: Nota de autorização coletada OFFLINE para
                venda de item de estoque limitado ({"cpf", "reason"}). É uma
                declaração não verificada criptograficamente (sem rede não há
                como validar senha) — NÃO seta force_overbooking: o lock de
                estoque continua decidindo no sync. Fica em metadata_info,
                na trilha PdvOfflineSyncLog e em AuditLog dedicado.
            synced_offline: True se a venda veio da fila offline.
            device_id: Identificador estável do terminal (obrigatório se
                synced_offline=True, para a trilha PdvOfflineSyncLog).
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            Dict {"sale", "reserva_id", "pricing", "duplicate": bool}.

        Raises:
            ValueError: Validação, turno fechado, ou item não encontrado.
            OverbookingError: Conflito de estoque (exceto force_overbooking).
        """
        cls._require(client_sale_uuid, "client_sale_uuid")
        cls._require(source_name, "source_name")
        cls._require(category_name, "category_name")
        if not items:
            raise ValueError("A venda deve conter ao menos um item.")

        method = cls._parse_payment_method(payment_method)
        if method == PdvPaymentMethod.COMANDA:
            raise ValueError(
                "Método COMANDA deve usar anexar_comanda(), não registrar_venda()."
            )
        override_details = cls._validar_manager_override(
            manager_override,
            required=force_overbooking,
        )
        offline_note = cls._validar_offline_override_note(offline_override_note)
        sync_payload = {
            "shift_id": shift_id,
            "source_name": source_name,
            "payment_method": method.value,
            "items": items,
        }
        if offline_note:
            sync_payload["offline_override_note"] = offline_note

        # 1. Idempotência — reenvio da fila offline é no-op
        existente = db.session.query(PdvSale).filter(
            PdvSale.client_sale_uuid == client_sale_uuid,
        ).first()
        if existente:
            if device_id:
                cls._log_sync(
                    client_sale_uuid, device_id, sync_payload,
                    PdvSyncOutcome.REJECTED_DUPLICATE,
                    "Venda já registrada — retorno idempotente.",
                )
                db.session.commit()
            return {
                "sale": cls._snapshot(existente, _SALE_SNAPSHOT_FIELDS),
                "reserva_id": existente.reserva_id,
                "pricing": None,
                "duplicate": True,
            }

        shift = cls._shift_aberto(shift_id)

        # 2. Preço autoritativo server-side
        source = db.session.query(ReservaSource).filter(
            db.func.lower(ReservaSource.name) == source_name.lower(),
            ReservaSource.deleted_at.is_(None),
        ).first()
        if not source:
            raise ValueError(f"Venue '{source_name}' não encontrado.")

        agora = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            pricing = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=agora,
                check_out_date=agora,
                items=items,
                manual_discount_percent=manual_discount_percent,
            )
        except ValueError as exc:
            if device_id:
                db.session.rollback()
                cls._log_sync(
                    client_sale_uuid, device_id, sync_payload,
                    PdvSyncOutcome.REJECTED_VALIDATION, str(exc),
                )
                db.session.commit()
            raise

        items_payload = cls._distribuir_price_override(pricing, items)

        # Walk-in sem nome → sequencial "Balcão N" (mesmo padrão do form balcão)
        nome_efetivo = (customer_name or "").strip() or ReservaService._proximo_nome_balcao()

        metadata_info = {
            "origem_pdv": True,
            "pdv_shift_id": shift.id,
            "pdv_payment_method": method.value,
            "synced_offline": synced_offline,
        }
        if offline_note:
            metadata_info["pdv_offline_override_note"] = offline_note

        # 3. Criar reserva pelo caminho único (lock de estoque incluído)
        try:
            reserva = ReservaService.criar(
                source_name=source.name,
                category_name=category_name,
                customer_name=nome_efetivo,
                customer_document=customer_document,
                origin="BALCAO",
                check_in_date=agora,
                check_out_date=None,
                items_payload=items_payload,
                metadata_info=metadata_info,
                manual_discount_percent=manual_discount_percent,
                force_overbooking=force_overbooking,
                ator_id=ator_id,
            )
        except OverbookingError as exc:
            if device_id:
                db.session.rollback()
                cls._log_sync(
                    client_sale_uuid, device_id, sync_payload,
                    PdvSyncOutcome.REJECTED_STOCK, str(exc),
                )
                db.session.commit()
            raise
        except ValueError as exc:
            if device_id:
                db.session.rollback()
                cls._log_sync(
                    client_sale_uuid, device_id, sync_payload,
                    PdvSyncOutcome.REJECTED_VALIDATION, str(exc),
                )
                db.session.commit()
            raise

        # 4. Status inicial derivado do método de pagamento
        if method in (
            PdvPaymentMethod.CASH,
            PdvPaymentMethod.EXTERNAL_CARD_TERMINAL,
            PdvPaymentMethod.EXTERNAL_PIX,
        ):
            reserva.status = cls._status_pdv_imediato(pdv_default_status)
        # MP_PIX/MP_CARD: permanece PENDING — o sistema gerou a cobrança e
        # aguarda o webhook MP confirmar. EXTERNAL_PIX não espera nada — o
        # pix já caiu na conta do operador antes de ele nem abrir o sistema.

        # 5. Troco + registro da venda
        total = Decimal(str(pricing["total"]))
        tendered = Decimal(str(amount_tendered)) if amount_tendered is not None else None
        change = None
        troco_source = None
        if method == PdvPaymentMethod.CASH and tendered is not None:
            if tendered < total:
                raise ValueError(
                    f"Valor entregue (R$ {tendered}) menor que o total (R$ {total})."
                )
            change = tendered - total
            if change > 0:
                if isinstance(change_source, PdvChangeSource):
                    troco_source = change_source
                else:
                    try:
                        troco_source = PdvChangeSource[str(change_source or "CASH").upper()]
                    except KeyError:
                        raise ValueError(
                            f"Fonte de troco inválida: '{change_source}'. Use CASH ou PIX."
                        )

        sale = PdvSale(
            shift_id=shift.id,
            reserva_id=reserva.id,
            client_sale_uuid=client_sale_uuid,
            sale_type=PdvSaleType.STANDALONE,
            payment_method=method,
            amount_total=total,
            amount_tendered=tendered,
            amount_change=change,
            change_source=troco_source,
            synced_offline=synced_offline,
        )
        db.session.add(sale)
        db.session.flush()

        AuditService.log(
            AuditAction.create,
            _MODULE,
            entity_id=sale.id,
            new_state=cls._snapshot(sale, _SALE_SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        if override_details:
            AuditService.log(
                AuditAction.update,
                _MODULE,
                entity_id=sale.id,
                previous_state=None,
                new_state={
                    "action": "manager_override",
                    "client_sale_uuid": client_sale_uuid,
                    "manager_id": override_details["manager_id"],
                    "reason": override_details["reason"],
                    "force_overbooking": force_overbooking,
                    "synced_offline": synced_offline,
                },
                actor_id=ator_id,
            )
        if offline_note:
            AuditService.log(
                AuditAction.update,
                _MODULE,
                entity_id=sale.id,
                previous_state=None,
                new_state={
                    "action": "offline_override_note",
                    "client_sale_uuid": client_sale_uuid,
                    "manager_cpf": offline_note["cpf"],
                    "reason": offline_note["reason"],
                    "synced_offline": synced_offline,
                },
                actor_id=ator_id,
            )
        if device_id:
            cls._log_sync(
                client_sale_uuid, device_id, sync_payload,
                PdvSyncOutcome.ACCEPTED, None,
            )
        db.session.commit()

        return {
            "sale": cls._snapshot(sale, _SALE_SNAPSHOT_FIELDS),
            "reserva_id": reserva.id,
            "pricing": pricing,
            "duplicate": False,
        }

    @classmethod
    def anexar_comanda(
        cls,
        *,
        client_sale_uuid: str,
        shift_id: int,
        reserva_id: int,
        items: list[dict],
        ator_id: int | None = None,
    ) -> dict:
        """Anexa itens à comanda de uma reserva ativa (pagos no check-out).

        Cria uma nova versão da Reserva via ReservaService.atualizar() com os
        itens existentes + novos. Se a reserva estava PAID e o total mudou, o
        próprio atualizar() rebaixa para PENDING (hóspede deve a diferença) —
        semântica correta de comanda.

        Args:
            client_sale_uuid: UUID v4 de idempotência gerado no cliente.
            shift_id: ID do turno OPEN do operador.
            reserva_id: ID da versão atual da reserva do hóspede.
            items: Novos itens {"item_id", "quantity"} a anexar.
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            Dict {"sale", "reserva_id" (nova versão), "duplicate": bool}.

        Raises:
            ValueError: Reserva não encontrada/arquivada, turno fechado.
        """
        cls._require(client_sale_uuid, "client_sale_uuid")
        if not items:
            raise ValueError("A comanda deve conter ao menos um item.")

        existente = db.session.query(PdvSale).filter(
            PdvSale.client_sale_uuid == client_sale_uuid,
        ).first()
        if existente:
            return {
                "sale": cls._snapshot(existente, _SALE_SNAPSHOT_FIELDS),
                "reserva_id": existente.reserva_id,
                "duplicate": True,
            }

        shift = cls._shift_aberto(shift_id)

        reserva = db.session.query(Reserva).filter(
            Reserva.id == reserva_id,
            Reserva.deleted_at.is_(None),
        ).first()
        if not reserva:
            raise ValueError("Reserva não encontrada.")
        if reserva.status == ReservaStatus.ARCHIVED_VERSION:
            raise ValueError("Reserva é uma versão arquivada — use a versão atual.")

        # Combinar itens existentes (ativos) com os novos preservando resource_id.
        # O mesmo item pode aparecer em linhas diferentes quando cada unidade
        # consome um recurso físico distinto.
        combinado: dict[tuple[int, int | None], int] = {}
        total_anterior = Decimal(str(reserva.total_amount or 0))
        for link in reserva.items:
            if link.deleted_at is None and link.item_id is not None:
                key = (link.item_id, link.resource_id)
                combinado[key] = combinado.get(key, 0) + link.quantity
        for entry in items:
            item_id = entry.get("item_id")
            if not item_id:
                raise ValueError("Cada item deve ter 'item_id'.")
            resource_id = entry.get("resource_id")
            resource_id = int(resource_id) if resource_id else None
            key = (int(item_id), resource_id)
            combinado[key] = combinado.get(key, 0) + int(entry.get("quantity", 1))

        combinado_items = [
            {
                "item_id": item_id,
                "quantity": quantity,
                **({"resource_id": resource_id} if resource_id else {}),
            }
            for (item_id, resource_id), quantity in combinado.items()
        ]

        # Recalcular preço da reserva completa para distribuir price_override
        pricing = ReservaPricingService.calculate(
            source_id=reserva.source_id,
            check_in_date=reserva.check_in_date,
            check_out_date=reserva.check_out_date or reserva.check_in_date,
            items=combinado_items,
            category_id=reserva.category_id,
            manual_discount_percent=float(reserva.desconto_percentual_manual or 0),
        )
        items_payload = cls._distribuir_price_override(pricing, combinado_items)

        nova_versao = ReservaService.atualizar(
            reserva.id,
            items_payload=items_payload,
            ator_id=ator_id,
        )

        valor_anexado = Decimal(str(pricing["total"])) - total_anterior

        sale = PdvSale(
            shift_id=shift.id,
            reserva_id=nova_versao.id,
            client_sale_uuid=client_sale_uuid,
            sale_type=PdvSaleType.COMANDA_APPEND,
            payment_method=PdvPaymentMethod.COMANDA,
            amount_total=valor_anexado,
        )
        db.session.add(sale)
        db.session.flush()

        AuditService.log(
            AuditAction.create,
            _MODULE,
            entity_id=sale.id,
            new_state=cls._snapshot(sale, _SALE_SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()

        return {
            "sale": cls._snapshot(sale, _SALE_SNAPSHOT_FIELDS),
            "reserva_id": nova_versao.id,
            "duplicate": False,
        }

    # ------------------------------------------------------------------
    # Carrinhos suspensos (espelho servidor)
    # ------------------------------------------------------------------

    @classmethod
    def suspender_carrinho(
        cls,
        shift_id: int,
        cart_json: dict,
        label: str | None = None,
        *,
        ator_id: int | None = None,
    ) -> PdvHeldCart:
        """Salva espelho servidor de um carrinho suspenso (best-effort).

        Args:
            shift_id: ID do turno OPEN.
            cart_json: Snapshot completo do carrinho client-side.
            label: Rótulo curto (nome do cliente, "Mesa 3", etc.).
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            PdvHeldCart persistido.

        Raises:
            ValueError: Turno não encontrado/fechado ou carrinho vazio.
        """
        shift = cls._shift_aberto(shift_id)
        if not cart_json:
            raise ValueError("Carrinho vazio não pode ser suspenso.")

        held = PdvHeldCart(shift_id=shift.id, cart_json=cart_json, label=label)
        db.session.add(held)
        db.session.flush()

        AuditService.log(
            AuditAction.create,
            _MODULE,
            entity_id=held.id,
            new_state={"shift_id": shift.id, "label": label},
            actor_id=ator_id,
        )
        db.session.commit()
        return held

    @classmethod
    def listar_carrinhos_suspensos(cls, shift_id: int) -> list[PdvHeldCart]:
        """Lista carrinhos suspensos não resolvidos do turno.

        Args:
            shift_id: ID do turno.

        Returns:
            Lista de PdvHeldCart pendentes, mais antigos primeiro.
        """
        return db.session.query(PdvHeldCart).filter(
            PdvHeldCart.shift_id == shift_id,
            PdvHeldCart.resolved_at.is_(None),
        ).order_by(PdvHeldCart.held_at.asc()).all()

    @classmethod
    def resolver_carrinho_suspenso(
        cls, held_cart_id: int, *, ator_id: int | None = None
    ) -> PdvHeldCart:
        """Marca um carrinho suspenso como resolvido (retomado ou descartado).

        Args:
            held_cart_id: ID do PdvHeldCart.
            ator_id: ID do usuário executando a ação (auditoria).

        Returns:
            PdvHeldCart atualizado.

        Raises:
            ValueError: Se o carrinho não for encontrado.
        """
        held = db.session.get(PdvHeldCart, held_cart_id)
        if not held:
            raise ValueError("Carrinho suspenso não encontrado.")
        held.resolved_at = datetime.now(timezone.utc)

        AuditService.log(
            AuditAction.update,
            _MODULE,
            entity_id=held.id,
            previous_state={"resolved_at": None},
            new_state={"resolved_at": held.resolved_at.isoformat()},
            actor_id=ator_id,
        )
        db.session.commit()
        return held

    # ------------------------------------------------------------------
    # Consulta de cliente / comanda
    # ------------------------------------------------------------------

    @classmethod
    def buscar_cliente(cls, query: str, source_id: int | None = None) -> list[dict]:
        """Busca reservas ativas por CPF/CNPJ ou fragmento de nome.

        Usada pelo lookup de comanda (F7): retorna reservas não arquivadas,
        não deletadas e não canceladas para vincular a venda ao hóspede.

        Args:
            query: CPF/CNPJ (somente dígitos) ou fragmento do nome (≥ 3 chars).
            source_id: Filtro opcional por venue.

        Returns:
            Lista de dicts {reserva_id, group_id, customer_name,
            customer_document, status, check_in_date, total_amount}.

        Raises:
            ValueError: Se a query tiver menos de 3 caracteres.
        """
        q = (query or "").strip()
        if len(q) < 3:
            raise ValueError("Busca requer ao menos 3 caracteres.")

        base = db.session.query(Reserva).filter(
            Reserva.deleted_at.is_(None),
            Reserva.status.notin_([
                ReservaStatus.ARCHIVED_VERSION,
                ReservaStatus.CANCELED,
            ]),
        )
        if source_id:
            base = base.filter(Reserva.source_id == source_id)

        somente_digitos = "".join(ch for ch in q if ch.isdigit())
        if somente_digitos and len(somente_digitos) >= 3:
            base = base.filter(Reserva.customer_document.contains(somente_digitos))
        else:
            base = base.filter(Reserva.customer_name.ilike(f"%{q}%"))

        reservas = base.order_by(Reserva.criado_em.desc()).limit(20).all()
        return [
            {
                "reserva_id": r.id,
                "group_id": r.group_id,
                "customer_name": r.customer_name,
                # Parcial: o operador confere o início do número que o cliente
                # apresenta sem que a busca vire uma listagem de CPFs.
                "customer_document": mascarar_documento(r.customer_document),
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "check_in_date": r.check_in_date.isoformat() if r.check_in_date else None,
                "total_amount": float(r.total_amount) if r.total_amount is not None else None,
            }
            for r in reservas
        ]

    # ------------------------------------------------------------------
    # Catálogo PDV (com flags offline)
    # ------------------------------------------------------------------

    @classmethod
    def catalogo_pdv(cls, source_id: int, favorite_item_ids: list[int] | None = None) -> dict:
        """Catálogo do venue com flags de venda offline por item.

        Estende ReservaService.catalogo() com `pdv_offline_allowed`:
          - True (🟢): item sem grupo de recursos físicos, ou cujo grupo tem
            ao menos um recurso ilimitado (stock_quantity IS NULL) — consumível
            de estoque amplo (gastronomia, lenha, bebidas).
          - False (🔴): item cujo grupo só tem recursos de estoque finito
            (Day Use, churrasqueiras, camping) — bloqueado offline para evitar
            overbooking com vendas concorrentes do site/bot.

        Args:
            source_id: ID da ReservaSource (venue).

        Returns:
            Dict {"source", "generated_at"} onde source segue o formato de
            ReservaService.catalogo() com o campo extra por item.

        Raises:
            ValueError: Se o venue não existir ou estiver inativo.
        """
        from app.models.reservas import ReservaCategory, ReservaItem, ReservaResource

        catalogo = ReservaService.catalogo(source_id=source_id)
        if not catalogo:
            raise ValueError(f"Venue {source_id} não encontrado ou inativo.")
        source_out = catalogo[0]
        favorite_item_ids = favorite_item_ids or []
        favorite_rank = {int(item_id): idx + 1 for idx, item_id in enumerate(favorite_item_ids)}

        # Grupos com ao menos um recurso ilimitado → offline permitido
        grupos_ilimitados = {
            gid
            for (gid,) in db.session.query(ReservaResource.group_id).filter(
                ReservaResource.source_id == source_id,
                ReservaResource.deleted_at.is_(None),
                ReservaResource.stock_quantity.is_(None),
                ReservaResource.group_id.isnot(None),
            ).distinct()
        }
        hoje = datetime.now(timezone.utc).date()
        recursos_por_grupo: dict[int, list[dict]] = {}
        for recurso in ReservaService.verificar_disponibilidade_recursos(
            source_id=source_id,
            check_in=hoje,
            check_out=hoje,
        ):
            group_id = recurso.get("group_id")
            if group_id is None:
                continue
            recursos_por_grupo.setdefault(group_id, []).append({
                "resource_id": recurso["resource_id"],
                "name": recurso["name"],
                "stock_quantity": recurso["stock_quantity"],
                "available": recurso["available"],
                "is_available": recurso["is_available"],
                "group_id": group_id,
                "group_path": recurso.get("group_path"),
            })

        # Preço do dia (dia útil / fim de semana / feriado). ReservaService.catalogo()
        # devolve sempre item.price (base); sem esta correção a tela exibiria o
        # preço base enquanto registrar_venda() cobraria o preço do dia.
        # A referência de data é a MESMA de registrar_venda() — exibir e cobrar
        # precisam concordar mesmo na virada do dia.
        itens_do_venue = (
            db.session.query(ReservaItem)
            .join(ReservaItem.category)
            .filter(
                ReservaCategory.source_id == source_id,
                ReservaItem.deleted_at.is_(None),
                ReservaCategory.deleted_at.is_(None),
            )
            .all()
        )
        precos_hoje = ReservaPricingService.precos_do_dia(itens_do_venue, hoje)

        for category in source_out.get("categories", []):
            for item in category.get("items", []):
                group_id = item.get("group_id")
                requires_resource = bool(item.get("requires_resource"))
                item["pdv_offline_allowed"] = (
                    not requires_resource
                    and (group_id is None or group_id in grupos_ilimitados)
                )
                item["pdv_resources"] = recursos_por_grupo.get(group_id, []) if requires_resource else []
                item_id = int(item["id"])
                item["pdv_is_favorite"] = item_id in favorite_rank
                item["pdv_favorite_rank"] = favorite_rank.get(item_id)
                if item_id in precos_hoje:
                    item["price"] = float(precos_hoje[item_id])

        return {
            "source": source_out,
            # Data usada para precificar. A SPA guarda o catálogo em IndexedDB
            # para o modo offline; comparar com a data corrente permite detectar
            # um catálogo do dia anterior (turno que atravessa a meia-noite).
            "pricing_date": hoje.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_payment_method(value: str | PdvPaymentMethod) -> PdvPaymentMethod:
        """Converte string em PdvPaymentMethod, com erro pt-BR amigável."""
        if isinstance(value, PdvPaymentMethod):
            return value
        try:
            return PdvPaymentMethod[str(value).upper()]
        except KeyError:
            validos = ", ".join(m.name for m in PdvPaymentMethod)
            raise ValueError(
                f"Método de pagamento inválido: '{value}'. Válidos: {validos}."
            )

    @staticmethod
    def _shift_aberto(shift_id: int) -> PdvShift:
        """Retorna o turno se existir e estiver OPEN; senão ValueError."""
        shift = db.session.query(PdvShift).filter(
            PdvShift.id == shift_id,
            PdvShift.deleted_at.is_(None),
        ).first()
        if not shift:
            raise ValueError("Turno não encontrado.")
        if shift.status != PdvShiftStatus.OPEN:
            raise ValueError("Turno está fechado — abra um novo turno para vender.")
        return shift

    @staticmethod
    def _status_pago() -> str:
        """Resolve o status 'pago' configurado (ReservaStatusConfig) ou PAID."""
        config = db.session.query(ReservaStatusConfig).filter_by(
            internal_name="PAID",
        ).first()
        return config.internal_name if config else ReservaStatus.PAID

    @staticmethod
    def _status_pdv_imediato(preferred_status: str | None) -> str:
        """Resolve o status inicial para vendas PDV com pagamento imediato."""
        if preferred_status:
            config = db.session.query(ReservaStatusConfig).filter_by(
                internal_name=preferred_status,
            ).first()
            if config and config.internal_name != "ARCHIVED_VERSION":
                return config.internal_name
        return PdvService._status_pago()

    @staticmethod
    def _distribuir_price_override(pricing: dict, items: list[dict]) -> list[dict]:
        """Distribui price_override por item a partir do pricing autoritativo.

        Distribui o total final proporcionalmente ao item_total de cada item
        (mesmo algoritmo do formulário de balcão), corrigindo arredondamento
        no item de maior valor para que sum(price_override) == total exato —
        a validação anti-fraude de ReservaService.criar() passa com zero
        divergência.
        """
        sim_subtotal = pricing["subtotal"]
        sim_total = pricing["total"]
        breakdown_rows = pricing.get("items_breakdown") or []
        breakdown_by_item = {
            e["item_id"]: e["item_total"]
            for e in breakdown_rows
        }

        payload = []
        for idx, entry in enumerate(items):
            if idx < len(breakdown_rows) and breakdown_rows[idx].get("item_id") == entry["item_id"]:
                item_total = breakdown_rows[idx].get("item_total", 0)
            else:
                item_total = breakdown_by_item.get(entry["item_id"], 0)
            override = (
                round(item_total / sim_subtotal * sim_total, 2)
                if sim_subtotal > 0
                else 0
            )
            payload.append({
                "item_id": entry["item_id"],
                "quantity": entry.get("quantity", 1),
                "resource_id": entry.get("resource_id"),
                "price_override": override,
            })

        soma = sum(p["price_override"] for p in payload)
        diff = round(sim_total - soma, 2)
        if abs(diff) >= 0.01 and payload:
            maior = max(range(len(payload)), key=lambda i: payload[i]["price_override"])
            payload[maior]["price_override"] = round(
                payload[maior]["price_override"] + diff, 2
            )
        return payload

    @staticmethod
    def _log_sync(
        client_sale_uuid: str,
        device_id: str,
        payload: dict,
        outcome: PdvSyncOutcome,
        error_detail: str | None,
    ) -> None:
        """Adiciona (sem commit) uma linha na trilha de sync offline."""
        db.session.add(
            PdvOfflineSyncLog(
                client_sale_uuid=client_sale_uuid,
                device_id=device_id,
                payload_snapshot=payload,
                outcome=outcome,
                error_detail=error_detail,
            )
        )

    @staticmethod
    def _validar_manager_override(
        manager_override: dict | None,
        *,
        required: bool,
    ) -> dict | None:
        """Valida e normaliza um override gerencial já autorizado."""
        if not manager_override:
            if required:
                raise ValueError("Override gerencial obrigatório para forçar overbooking.")
            return None

        try:
            manager_id = int(manager_override.get("manager_id") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Gerente do override inválido.") from exc
        reason = (manager_override.get("reason") or "").strip()
        if not manager_id:
            raise ValueError("Gerente do override inválido.")
        if len(reason) < 10:
            raise ValueError("Informe um motivo com pelo menos 10 caracteres.")
        if not has_permission(manager_id, "pdv", "manage_shift"):
            raise ValueError("Funcionário sem permissão gerencial para override PDV.")

        return {"manager_id": manager_id, "reason": reason}

    @staticmethod
    def _validar_offline_override_note(note: dict | None) -> dict | None:
        """Valida e normaliza a nota de autorização offline de item 🔴.

        Diferente de _validar_manager_override, esta nota NUNCA pôde ser
        verificada contra senha (não havia rede) — é registro declaratório
        para reconciliação. Por isso ela não habilita force_overbooking.
        """
        if not note:
            return None

        cpf_digits = "".join(ch for ch in str(note.get("cpf") or "") if ch.isdigit())
        reason = (note.get("reason") or "").strip()
        if len(cpf_digits) != 11:
            raise ValueError("CPF do responsável pelo override offline inválido.")
        if len(reason) < 10:
            raise ValueError("Informe um motivo com pelo menos 10 caracteres.")
        return {"cpf": cpf_digits, "reason": reason}

    @staticmethod
    def _snapshot_sync_log(log: PdvOfflineSyncLog) -> dict:
        """Snapshot compacto e não sensível do log de sync offline."""
        payload = log.payload_snapshot or {}
        items = payload.get("items") or []
        total_quantity = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                total_quantity += int(item.get("quantity") or 0)
            except (TypeError, ValueError):
                continue

        return {
            "id": log.id,
            "client_sale_uuid": log.client_sale_uuid,
            "device_id": log.device_id,
            "outcome": log.outcome.value if hasattr(log.outcome, "value") else str(log.outcome),
            "error_detail": log.error_detail,
            "processed_at": log.processed_at.isoformat() if log.processed_at else None,
            "payload_summary": {
                "shift_id": payload.get("shift_id"),
                "source_name": payload.get("source_name"),
                "payment_method": payload.get("payment_method"),
                "item_count": len(items),
                "total_quantity": total_quantity,
            },
        }
