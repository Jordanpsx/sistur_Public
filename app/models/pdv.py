# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Módulo PDV (Ponto de Venda) — venda rápida de balcão integrada a Reservas.

Modelos deste módulo NÃO duplicam o controle de estoque: toda venda concluída
resolve para uma `Reserva` criada via `ReservaService.criar()` (mesmo caminho
usado por WordPress, bot WhatsApp e formulário interno), herdando o lock
pessimista de `_verificar_estoque()`. As tabelas aqui registram apenas:

  - PdvShift:          turno de caixa (abertura/fechamento, conferência de dinheiro)
  - PdvSale:           como a venda foi paga + chave de idempotência offline
  - PdvHeldCart:       espelho servidor (best-effort) de carrinhos em espera
  - PdvOfflineSyncLog: trilha de auditoria de cada tentativa de sync offline

Ver PDV_PROJECT_PLAN.md e docs/pdv/README.md para a arquitetura completa.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from app.extensions import db


def _utcnow() -> datetime:
    """Timestamp UTC timezone-aware para defaults de coluna."""
    return datetime.now(timezone.utc)


class PdvShiftStatus(str, enum.Enum):
    """Estado do turno de caixa.

    PENDING_APPROVAL: fechamento com quebra de caixa (contado ≠ esperado em
    qualquer das duas fontes de troco) aguardando aprovação de supervisor.
    """

    OPEN = "OPEN"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    CLOSED = "CLOSED"


class PdvChangeSource(str, enum.Enum):
    """Fonte usada para dar o troco de uma venda em dinheiro.

    CASH: troco saiu da gaveta física.
    PIX:  operador devolveu o troco via PIX do fundo digital do caixa.
    """

    CASH = "CASH"
    PIX = "PIX"


class PdvSaleType(str, enum.Enum):
    """Natureza da venda de balcão.

    STANDALONE:     venda avulsa — cria uma nova Reserva (origin=BALCAO).
    COMANDA_APPEND: itens anexados à comanda de uma Reserva ativa existente,
                    pagos no check-out (nova versão da Reserva via atualizar()).
    """

    STANDALONE = "STANDALONE"
    COMANDA_APPEND = "COMANDA_APPEND"


class PdvPaymentMethod(str, enum.Enum):
    """Método de pagamento coletado no balcão.

    A distinção que importa não é "qual meio" (pix/cartão), é **quem
    processou o pagamento**:

    - CASH / EXTERNAL_CARD_TERMINAL / EXTERNAL_PIX: dinheiro/pix/cartão já
        recebido pelo operador **fora do sistema** (na mão, na maquininha
        própria do estabelecimento, ou pix direto na conta) — o sistema só
        está registrando algo que já aconteceu. Reserva nasce PAID
        imediatamente; não há nada para o sistema aguardar.
    - MP_PIX / MP_CARD: o **sistema** gera a cobrança (QR code ou link) e
        processa o pagamento via Mercado Pago — aí sim existe uma espera
        real. Reserva nasce PENDING e segue o fluxo webhook + Active
        Defense existente.
    - COMANDA: sem pagamento no ato — itens vão para a conta da reserva do
        hóspede e são cobrados no check-out.

    EXTERNAL_PIX **não** entra na conferência de fechamento de caixa
    (`PdvService._esperados_fechamento`). O fundo PIX do turno é
    exclusivamente a reserva de troco — fixo, só decresce quando dá troco em
    pix — e não é "a conta onde o pix do estabelecimento cai". Pix recebido
    direto vai para fora do sistema (banco/conta pix do estabelecimento);
    fica visível no relatório do turno por método de pagamento
    (`PdvService.relatorio_turno`), sem exigir uma "contagem física" que não
    existe para dinheiro que nunca passou pela mão do operador.
    """

    CASH = "CASH"
    EXTERNAL_CARD_TERMINAL = "EXTERNAL_CARD_TERMINAL"
    EXTERNAL_PIX = "EXTERNAL_PIX"
    MP_PIX = "MP_PIX"
    MP_CARD = "MP_CARD"
    COMANDA = "COMANDA"


class TipoMovimentacaoCaixa(str, enum.Enum):
    """Natureza de uma movimentação manual de caixa (fora do fluxo de vendas).

    A distinção que importa aqui é a mesma do PdvPaymentMethod: **fluxo de
    caixa físico é sempre separado de P&L**. Retirar dinheiro da gaveta para
    pagar uma despesa administrativa tira dinheiro físico do caixa, mas essa
    despesa pertence ao centro de custo que a originou (Administrativo,
    Manutenção…) — nunca ao PDV de onde o dinheiro fisicamente saiu. O PDV é
    só o "banco" físico; PAGAMENTO_DESPESA é a prova de que aquele dinheiro
    saiu da gaveta e foi carimbado para outro centro de custo no mesmo
    lançamento, sem passar pela margem do PDV.

    TRANSFERENCIA_INTERNA: dinheiro sai desta gaveta para outra gaveta física
        ou para uma conta virtual (ex.: sistema externo). Exige conta_destino_id.
    RECOLHIMENTO_COFRE: dinheiro sai da gaveta e vai para o cofre do
        estabelecimento — reduz o físico sem gerar despesa nem transferência
        rastreável a outra gaveta.
    PAGAMENTO_DESPESA: dinheiro sai da gaveta para pagar algo que pertence a
        outro centro de custo. Exige centro_de_custo_id.
    SUPRIMENTO: dinheiro entra na gaveta (reforço de troco vindo do cofre,
        de outra gaveta etc.) — o único tipo que soma no saldo esperado.
    """

    TRANSFERENCIA_INTERNA = "TRANSFERENCIA_INTERNA"
    RECOLHIMENTO_COFRE = "RECOLHIMENTO_COFRE"
    PAGAMENTO_DESPESA = "PAGAMENTO_DESPESA"
    SUPRIMENTO = "SUPRIMENTO"


class PdvSyncOutcome(str, enum.Enum):
    """Resultado do processamento de uma venda vinda da fila offline."""

    ACCEPTED = "ACCEPTED"
    REJECTED_STOCK = "REJECTED_STOCK"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"


class PdvShift(db.Model):
    """Turno de caixa de um operador em um venue.

    Um operador abre o turno com o fundo de troco (`opening_float_amount`) e
    fecha informando o dinheiro contado na gaveta. O valor esperado é calculado
    a partir das vendas CASH do turno para conferência.
    """

    __tablename__ = "sistur_pdv_shifts"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operador_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Fonte física (gaveta) e fonte digital (conta PIX de troco do caixa)
    opening_float_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    opening_float_pix = db.Column(db.Numeric(10, 2), nullable=False, default=0, server_default="0")
    closing_counted_amount = db.Column(db.Numeric(10, 2), nullable=True)
    closing_expected_amount = db.Column(db.Numeric(10, 2), nullable=True)
    closing_counted_pix = db.Column(db.Numeric(10, 2), nullable=True)
    closing_expected_pix = db.Column(db.Numeric(10, 2), nullable=True)
    status = db.Column(
        db.Enum(PdvShiftStatus, native_enum=False, length=20),
        nullable=False,
        default=PdvShiftStatus.OPEN,
        index=True,
    )
    # Quebra de caixa: justificativa do operador + aprovação do supervisor
    discrepancy_reason = db.Column(db.Text, nullable=True)
    approved_by_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    opened_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    source = db.relationship("ReservaSource", foreign_keys=[source_id])
    operador = db.relationship("Funcionario", foreign_keys=[operador_id])
    approved_by = db.relationship("Funcionario", foreign_keys=[approved_by_id])
    sales = db.relationship("PdvSale", back_populates="shift", lazy="dynamic")

    def __repr__(self) -> str:
        """Representação textual para debug."""
        return (
            f"<PdvShift id={self.id} source={self.source_id} "
            f"operador={self.operador_id} status={self.status.value}>"
        )


class PdvSale(db.Model):
    """Registro de venda do PDV — como a venda foi paga.

    Wrapper fino: a mutação de estoque vive na `Reserva` associada. Esta linha
    é criada na mesma transação da Reserva (venda avulsa) ou da nova versão
    (comanda) e carrega a chave de idempotência `client_sale_uuid`, gerada no
    cliente no fechamento do carrinho — reenvios da fila offline são no-ops.
    """

    __tablename__ = "sistur_pdv_sales"

    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_pdv_shifts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reserva_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reservas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    client_sale_uuid = db.Column(db.String(36), nullable=False, unique=True)
    sale_type = db.Column(
        db.Enum(PdvSaleType, native_enum=False),
        nullable=False,
        default=PdvSaleType.STANDALONE,
    )
    payment_method = db.Column(
        db.Enum(PdvPaymentMethod, native_enum=False),
        nullable=False,
        index=True,
    )
    amount_total = db.Column(db.Numeric(10, 2), nullable=False)
    amount_tendered = db.Column(db.Numeric(10, 2), nullable=True)
    amount_change = db.Column(db.Numeric(10, 2), nullable=True)
    # De onde saiu o troco (venda CASH): gaveta física ou fundo PIX do caixa
    change_source = db.Column(
        db.Enum(PdvChangeSource, native_enum=False, length=10),
        nullable=True,
    )
    synced_offline = db.Column(db.Boolean, nullable=False, default=False)
    criado_em = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    shift = db.relationship("PdvShift", back_populates="sales")
    reserva = db.relationship("Reserva", foreign_keys=[reserva_id])

    def __repr__(self) -> str:
        """Representação textual para debug."""
        return (
            f"<PdvSale id={self.id} uuid={self.client_sale_uuid[:8]}… "
            f"method={self.payment_method.value} total={self.amount_total}>"
        )


class PdvHeldCart(db.Model):
    """Espelho servidor de um carrinho em espera ("venda suspensa").

    O mecanismo primário de hold é client-side (localStorage) para funcionar
    offline; esta tabela é um backstop best-effort para sobreviver a crash
    total do terminal ou troca de máquina pelo operador.
    """

    __tablename__ = "sistur_pdv_held_carts"

    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_pdv_shifts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = db.Column(db.String(100), nullable=True)
    cart_json = db.Column(db.JSON, nullable=False)
    held_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)

    shift = db.relationship("PdvShift", foreign_keys=[shift_id])

    def __repr__(self) -> str:
        """Representação textual para debug."""
        return f"<PdvHeldCart id={self.id} shift={self.shift_id} label={self.label!r}>"


class PdvOfflineSyncLog(db.Model):
    """Trilha de auditoria de cada tentativa de sync de venda offline.

    Uma linha por tentativa processada (aceita ou rejeitada). Rejeições de
    estoque (REJECTED_STOCK) representam dinheiro já coletado no mundo real —
    viram tarefa de reconciliação gerencial, nunca são descartadas em silêncio.
    """

    __tablename__ = "sistur_pdv_offline_sync_log"

    id = db.Column(db.Integer, primary_key=True)
    client_sale_uuid = db.Column(db.String(36), nullable=False, index=True)
    device_id = db.Column(db.String(100), nullable=False)
    payload_snapshot = db.Column(db.JSON, nullable=False)
    outcome = db.Column(
        db.Enum(PdvSyncOutcome, native_enum=False),
        nullable=False,
        index=True,
    )
    error_detail = db.Column(db.Text, nullable=True)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        """Representação textual para debug."""
        return (
            f"<PdvOfflineSyncLog id={self.id} uuid={self.client_sale_uuid[:8]}… "
            f"outcome={self.outcome.value}>"
        )


class PdvMovimentacaoCaixa(db.Model):
    """Movimentação manual de caixa — sangria, suprimento, ou lançamento
    equivalente que não nasce de uma venda (PdvSale).

    Separação estrita entre fluxo físico e P&L: esta tabela só registra o
    que aconteceu com o *dinheiro físico* de um turno. O impacto contábil
    (a quem a despesa pertence) mora em `centro_de_custo_id`, nunca no
    `caixa_turno_id` de onde o dinheiro saiu — retirar dinheiro da gaveta da
    "Portaria" para pagar uma conta de manutenção deduz o físico da
    Portaria, mas a despesa é lançada no centro de custo de Manutenção, não
    reduz a margem da Portaria.

    `conta_destino_id` é deliberadamente uma referência solta (sem FK rígida
    no banco), não `sistur_pdv_shifts.id`: quando `tipo_movimentacao` é
    TRANSFERENCIA_INTERNA, o destino pode ser outra gaveta física (aí sim um
    PdvShift.id) OU uma conta virtual de um sistema externo, que não tem
    linha nesta base. Uma FK rígida quebraria esse segundo caso.
    """

    __tablename__ = "sistur_pdv_movimentacoes_caixa"

    id = db.Column(db.Integer, primary_key=True)
    caixa_turno_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_pdv_shifts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tipo_movimentacao = db.Column(
        db.Enum(TipoMovimentacaoCaixa, native_enum=False),
        nullable=False,
        index=True,
    )
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    # Referência solta — ver docstring da classe. Nunca populado para
    # RECOLHIMENTO_COFRE/PAGAMENTO_DESPESA/SUPRIMENTO, só TRANSFERENCIA_INTERNA.
    conta_destino_id = db.Column(db.Integer, nullable=True)
    centro_de_custo_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_areas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # RESTRICT (não CASCADE) de propósito: é um registro financeiro de
    # auditoria — desligar/remover um funcionário não pode apagar em
    # cascata o rastro de para onde o dinheiro foi.
    autorizado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    descricao = db.Column(db.Text, nullable=False)
    data_hora = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    caixa_turno = db.relationship("PdvShift", foreign_keys=[caixa_turno_id])
    centro_de_custo = db.relationship("Area", foreign_keys=[centro_de_custo_id])
    funcionario = db.relationship("Funcionario", foreign_keys=[funcionario_id])
    autorizado_por = db.relationship("Funcionario", foreign_keys=[autorizado_por_id])

    def __repr__(self) -> str:
        """Representação textual para debug."""
        return (
            f"<PdvMovimentacaoCaixa id={self.id} turno={self.caixa_turno_id} "
            f"tipo={self.tipo_movimentacao.value} valor={self.valor}>"
        )
