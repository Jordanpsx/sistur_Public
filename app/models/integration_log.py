# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Modelo de log de erros de integração entre frontends externos e o SISTUR."""

from datetime import datetime, timezone

from app.extensions import db


class IntegrationLog(db.Model):
    """Registra falhas originadas em frontends externos (WordPress/JS, MP Bricks).

    Diferente do AuditLog — que cobre ações de usuários autenticados — este
    modelo captura erros que ocorrem fora do contexto de sessão do SISTUR,
    dando visibilidade sobre falhas no fluxo de pagamento do cliente final.

    Não possui FK para reserva_id intencionalmente: um dos principais cenários
    de erro é exatamente quando o vínculo reserva→sistur está quebrado, portanto
    a restrição impediria o log do erro que mais importa registrar.

    Campos de contexto obrigatórios para facilitar diagnóstico:
      - reserva_id + customer_name: identificam univocamente a reserva afetada
        mesmo que o registro da reserva tenha sido deletado.
      - error_source: classifica a origem (MP_BRICKS, WP_INTERNAL, SISTUR_SYNC,
        SISTUR_QR).
    """

    __tablename__ = "sistur_integration_logs"

    id = db.Column(db.Integer, primary_key=True)

    # Contexto da reserva — sem FK para aceitar logs mesmo quando a reserva é inválida
    reserva_id = db.Column(db.Integer, nullable=True, index=True)

    # Nome do cliente da reserva para facilitar diagnóstico sem precisar de JOIN
    customer_name = db.Column(db.String(255), nullable=True)

    # Classificação do erro
    error_source = db.Column(db.String(50), nullable=False, index=True)
    # Valores esperados: 'MP_BRICKS' | 'WP_INTERNAL' | 'SISTUR_SYNC' | 'SISTUR_QR'

    error_code = db.Column(db.String(100), nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    # Classificação semântica do erro para filtragem rápida na UI
    # Valores: 'system' | 'client_bank' | 'invalid_data' | None (registros legados)
    error_category = db.Column(db.String(20), nullable=True, index=True)

    # status_detail retornado pelo MP (ex: cc_rejected_insufficient_amount)
    mp_status_detail = db.Column(db.String(100), nullable=True)

    # Payload bruto do objeto de erro retornado pelo MP ou servidor
    raw_payload = db.Column(db.JSON, nullable=True)

    # Contexto da requisição HTTP. Sem IP de propósito: quem chama é sempre um
    # servidor (WordPress, site, Mercado Pago), então o IP gravado era o do
    # servidor e não servia para nada; e guardar o IP do visitante acabaria com
    # a visita anônima ao site.
    user_agent = db.Column(db.String(255), nullable=True)

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    @staticmethod
    def classify(error_source: str, error_code: str) -> str:
        """Classifica um erro em 'system', 'client_bank' ou 'invalid_data'.

        'system'       → falha de infra, config ou código (precisa de ação técnica).
        'client_bank'  → cartão recusado pelo banco/MP (sem ação técnica necessária).
        'invalid_data' → contexto inválido vindo do cliente ou do proxy WP: CPF errado,
                         nonce expirado (página em cache), reserva já expirada ou cliente
                         bloqueado. Não indica bug — indica atendimento a fazer.

        Nota: MP_BRICKS não é mais classificado em bloco como 'client_bank'. Erros do
        Brick incluem falhas de carregamento do SDK e configuração ausente (public key),
        que são falhas de sistema e precisam aparecer no filtro 'system'.
        """
        src  = (error_source or "").upper()
        code = (error_code   or "").upper()

        if (code.startswith("CC_REJECTED")
                or code.startswith("CARD_MP_")
                or code == "PAYMENT_REJECTED_OR_FAILED"):
            return "client_bank"

        # Falhas do Brick que são claramente de infra/config do nosso lado.
        if code in {"BRICK_CREATE_FAILED", "MP_SDK_LOAD_FAILED", "MP_PUBLIC_KEY_MISSING"}:
            return "system"

        if ("VALIDATION_ERROR" in code
                or "INVALID_USER" in code
                or "INVALID_INPUT" in code
                or "NONCE" in code
                or "CLIENT_BANNED" in code
                or "EXPIRED" in code
                or code in {"CARD_SISTUR_400", "PIX_SISTUR_400", "INGEST_SISTUR_400"}):
            return "invalid_data"

        if src == "MP_BRICKS":
            # Demais erros do Brick continuam sendo recusa/limitação do meio de pagamento.
            return "client_bank"

        return "system"

    def __repr__(self) -> str:
        """Representação legível para logs e depuração."""
        return (
            f"<IntegrationLog id={self.id} source={self.error_source!r}"
            f" reserva_id={self.reserva_id} customer={self.customer_name!r}>"
        )
