# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
PaymentTransaction — Ledger individual de cobranças contra uma Reserva.

Substitui o modelo de pagamento único (campos JSON em Reserva) por um ledger
1:N que suporta pagamentos parciais, estornos e auditoria detalhada.

Fluxo:
  1. processar_pagamento() cria PaymentTransaction(PENDING) antes de chamar o MP.
  2. O `id` (UUID) é enviado como `external_reference` ao Mercado Pago.
  3. Webhook recebe confirmação → identifica a transação pelo external_reference.
  4. Atualiza PaymentTransaction.status → recalcula Reserva.status.

Cross-version compatibility:
  `reserva_group_id` (não `reserva_id`) agrega pagamentos de todas as versões
  da mesma reserva. Edits (nova versão = novo id) não perdem pagamentos anteriores.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from app.extensions import db


class TransactionStatus(str, enum.Enum):
    """Ciclo de vida de uma transação de pagamento individual."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    REFUND_FAILED = "REFUND_FAILED"


class PaymentTransaction(db.Model):
    """Registro individual de cobrança — unidade atômica do ledger de pagamentos.

    Criado com status PENDING imediatamente antes de chamar a API do Mercado Pago.
    O `id` (UUID) é enviado como `external_reference` para que o webhook identifique
    exatamente qual transação foi confirmada, sem depender de joins complexos.

    Atributos de negócio:
        reserva_group_id: UUID do grupo de versões da Reserva — imutável entre edits.
        reserva_id: ID da versão atual da Reserva (FK, SET NULL em cascade delete).
        status: Estado atual (PENDING → APPROVED | REJECTED | CANCELLED | REFUNDED | REFUND_FAILED).
        amount: Valor cobrado nesta transação específica (pode ser parcial).
        payment_method: Método de pagamento ("pix", "credit_card", etc.).

    Campos do Mercado Pago:
        mp_payment_id: ID do pagamento no MP (resposta da API, indexado para webhook).
        mp_status: Status raw do MP ("pending", "approved", "rejected", etc.).
        mp_status_detail: Detalhe do MP (ex.: "pending_waiting_payment").

    PIX:
        qr_code: Código copia-e-cola.
        qr_code_base64: QR Code como imagem base64.
        ticket_url: URL do comprovante no MP.

    Auditoria:
        created_at: Antes da chamada ao MP.
        approved_at: Setado pelo webhook na aprovação.
        mp_response_snapshot: Snapshot do response do MP para disputas.
    """

    __tablename__ = "sistur_payment_transactions"

    id = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    # Cross-version aggregation — usa group_id, não reserva_id
    reserva_group_id = db.Column(db.String(36), nullable=False, index=True)
    reserva_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reservas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status = db.Column(
        db.Enum(TransactionStatus, native_enum=False),
        nullable=False,
        default=TransactionStatus.PENDING,
        index=True,
    )
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_method = db.Column(db.String(50), nullable=True)

    # Mercado Pago tracking
    mp_payment_id = db.Column(db.String(100), nullable=True, index=True)
    mp_status = db.Column(db.String(50), nullable=True)
    mp_status_detail = db.Column(db.String(100), nullable=True)

    # PIX QR data (snapshot permanente — persiste além da expiração do PIX)
    qr_code = db.Column(db.Text, nullable=True)
    qr_code_base64 = db.Column(db.Text, nullable=True)
    ticket_url = db.Column(db.String(512), nullable=True)

    # Timestamps
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Estorno — preenchidos pelo Active Defense quando o slot já está ocupado
    refund_id = db.Column(db.String(100), nullable=True)        # ID do estorno no MP
    refunded_at = db.Column(db.DateTime(timezone=True), nullable=True)  # Data/hora do estorno

    # Snapshot raw do MP para auditoria e resolução de disputas
    mp_response_snapshot = db.Column(db.JSON, nullable=True)

    # Relacionamento com a versão atual da Reserva
    reserva = db.relationship(
        "Reserva",
        foreign_keys=[reserva_id],
        backref=db.backref("payment_transactions", lazy="dynamic"),
    )

    def __repr__(self) -> str:
        """Representação textual para debug."""
        return (
            f"<PaymentTransaction id={self.id[:8]}… "
            f"group={self.reserva_group_id[:8]}… "
            f"amount={self.amount} status={self.status.value}>"
        )
