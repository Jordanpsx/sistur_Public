# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Serviço de Pagamentos — integração com Mercado Pago (modelo multi-transação).

Fluxo de pagamento:
  1. processar_pagamento() cria um PaymentTransaction(PENDING) antes de chamar o MP.
  2. O UUID da transação é enviado como `external_reference` ao MP.
  3. Webhook do MP → processar_webhook() identifica a transação pelo external_reference.
  4. Atualiza PaymentTransaction.status → _recalcular_saldo_reserva() → Reserva.status.

Compatibilidade retroativa (legacy path):
  Pagamentos gerados antes desta refatoração não possuem `external_reference` UUID.
  processar_webhook() detecta isso (external_reference ausente ou em formato antigo)
  e cai no caminho legado: busca Reserva por Reserva.payment_id — comportamento
  idêntico ao anterior.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from app.services.integration_log_service import IntegrationLogService

logger = logging.getLogger(__name__)

# Regex para detectar external_reference no formato UUID (novo) vs. legado (ausente/string)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _verificar_overbooking_ao_pagar(reserva_id: int) -> list[dict]:
    """Verifica se confirmar pagamento de uma reserva causa overbooking.

    Retorna lista de conflitos ou lista vazia se não há conflito.
    Chamado antes de mudar status → PAID em confirmações automáticas do MP.
    """
    from app.extensions import db
    from app.models.reservas import Reserva, ReservaItemLink, ReservaResource
    from app.services.reserva_service import OverbookingError, ReservaService

    reserva = db.session.get(Reserva, reserva_id)
    if reserva is None:
        return []

    links = (
        db.session.query(ReservaItemLink)
        .filter(
            ReservaItemLink.reserva_id == reserva_id,
            ReservaItemLink.resource_id.isnot(None),
            ReservaItemLink.deleted_at.is_(None),
        )
        .all()
    )

    todos_conflitos: list[dict] = []
    for link in links:
        rec = db.session.get(ReservaResource, link.resource_id)
        if rec is None or rec.stock_quantity is None:
            continue
        try:
            ReservaService._verificar_estoque(
                rec,
                reserva.check_in_date,
                reserva.check_out_date,
                link.quantity,
                exclude_reserva_id=reserva_id,
            )
        except OverbookingError as exc:
            todos_conflitos.extend(exc.conflitos)

    return todos_conflitos


def _super_admins_ativos():
    """Retorna lista de Funcionario com is_super_admin=True e ativo=True."""
    from app.extensions import db
    from app.models.funcionario import Funcionario
    from app.models.role import Role

    return (
        db.session.query(Funcionario)
        .join(Role, Funcionario.role_id == Role.id)
        .filter(Role.is_super_admin.is_(True), Funcionario.ativo.is_(True))
        .all()
    )


def _notificar_admins(titulo: str, mensagem: str, log_tag: str) -> None:
    """Envia Aviso tipo SISTEMA para todos os super-admins ativos."""
    from app.services.aviso_service import AvisoService

    admins = _super_admins_ativos()
    if not admins:
        logger.warning("%s: nenhum super-admin ativo encontrado.", log_tag)
        return
    for admin in admins:
        try:
            AvisoService.criar(
                destinatario_id=admin.id,
                titulo=titulo,
                mensagem=mensagem,
                tipo="SISTEMA",
                ator_id=None,
            )
        except Exception as exc:
            logger.warning("%s: falha ao notificar admin %d: %s", log_tag, admin.id, exc)


def _criar_aviso_pago_overbooking(reserva_id: int, conflitos: list[dict]) -> None:
    """Aviso #1 — reserva confirmada como PAID mas há overbooking detectado.

    Criado imediatamente após o pagamento ser registrado como PAID, antes do
    extorno ser processado. Alerta os operadores e inclui link para a reserva.
    """
    from app.extensions import db
    from app.models.reservas import Reserva

    reserva = db.session.get(Reserva, reserva_id)
    cliente = reserva.customer_name if reserva else f"ID {reserva_id}"

    linhas = [
        f"A reserva #{reserva_id} ({cliente}) foi paga, porém o slot já está ocupado.",
        f"Ver reserva: /reservas/{reserva_id}",
        "",
        "O sistema está processando o extorno automático ao cliente.",
        "",
        "Reservas conflitantes:",
    ]
    for c in conflitos:
        linhas.append(
            f"  • #{c['reserva_id']} — {c['customer_name']} | "
            f"Check-in: {c['check_in']} | Check-out: {c['check_out']} | "
            f"Status: {c['status']} → /reservas/{c['reserva_id']}"
        )

    _notificar_admins(
        titulo=f"⚠️ Overbooking: reserva #{reserva_id} paga — extorno em processamento",
        mensagem="\n".join(linhas),
        log_tag="_criar_aviso_pago_overbooking",
    )


def _criar_avisos_overbooking(
    reserva_id: int,
    conflitos: list[dict],
    *,
    refund_ok: bool = True,
    mp_payment_id: str | None = None,
    refund_error: str | None = None,
) -> None:
    """Aviso #2 — reserva cancelada por overbooking. Diferencia sucesso vs falha do extorno.

    Se refund_ok=True: confirma ao admin que tudo foi concluído.
    Se refund_ok=False: alerta que o extorno FALHOU e exige ação manual no MP.
    Nunca afirmar que o extorno foi feito quando a API do MP devolveu erro —
    admin pensa que está tudo resolvido e cliente fica sem reserva E sem dinheiro.
    """
    from app.extensions import db
    from app.models.reservas import Reserva

    reserva = db.session.get(Reserva, reserva_id)
    cliente = reserva.customer_name if reserva else f"ID {reserva_id}"

    if refund_ok:
        titulo = f"✓ Extorno concluído: reserva #{reserva_id} cancelada por overbooking"
        linhas = [
            f"A reserva #{reserva_id} ({cliente}) foi cancelada e o valor extornado "
            "automaticamente ao cliente via Mercado Pago.",
            f"Ver reserva cancelada: /reservas/{reserva_id}",
            "",
            "Reservas que ocupavam o slot:",
        ]
    else:
        titulo = (
            f"🚨 EXTORNO FALHOU: reserva #{reserva_id} cancelada — ação manual necessária"
        )
        linhas = [
            f"A reserva #{reserva_id} ({cliente}) foi cancelada por overbooking, "
            "mas o extorno automático ao cliente FALHOU.",
            "",
            "⚠️ AÇÃO MANUAL NECESSÁRIA — faça o reembolso direto no painel do "
            "Mercado Pago antes que o cliente acione o suporte.",
            "",
            f"  • ID do pagamento no MP: {mp_payment_id or 'indisponível'}",
            f"  • Painel MP: https://www.mercadopago.com.br/activities/{mp_payment_id or ''}",
        ]
        if refund_error:
            linhas.append(f"  • Motivo da falha: {refund_error}")
        linhas += [
            f"  • Ver reserva cancelada: /reservas/{reserva_id}",
            "  • Detalhes técnicos em /admin/logs (filtro REFUND_FAILED)",
            "",
            "Reservas que ocupavam o slot:",
        ]

    for c in conflitos:
        linhas.append(
            f"  • #{c['reserva_id']} — {c['customer_name']} | "
            f"Check-in: {c['check_in']} | Check-out: {c['check_out']} → /reservas/{c['reserva_id']}"
        )

    _notificar_admins(
        titulo=titulo,
        mensagem="\n".join(linhas),
        log_tag="_criar_avisos_overbooking",
    )


class MercadoPagoService:
    """Serviço de integração com o Mercado Pago para processamento de pagamentos.

    Responsabilidades:
      - Criar PaymentTransaction(PENDING) antes de cada chamada ao MP.
      - Processar pagamentos com cartão (via token do MP Bricks).
      - Processar pagamentos PIX (gera QR Code para exibição ao operador).
      - Processar notificações de webhook com dual-path (novo UUID / legado).
      - Recalcular saldo e status da Reserva após cada aprovação.

    Compatibilidade retroativa:
      O webhook aceita tanto `external_reference` UUID (novo) quanto o caminho
      legado via Reserva.payment_id para pagamentos criados antes da refatoração.
    """

    @classmethod
    def _access_token(cls) -> str | None:
        """Resolve o Access Token do Mercado Pago com fallback retrocompatível.

        Prioridade de leitura do Access Token:
          1. Cofre interno (`SecretService.MERCADOPAGO_ACCESS_TOKEN`).
          2. Configuração legada (`mercadopago.access_token` em SystemSetting).
          3. Variável de ambiente `MP_ACCESS_TOKEN`.

        Returns:
            Token configurado ou None.
        """
        try:
            from app.services.secret_service import SecretService

            token = SecretService.get(SecretService.MERCADOPAGO_ACCESS_TOKEN)
            if token:
                return token
        except Exception:
            pass

        try:
            from app.services.configuracao_service import (
                CHAVE_MP_ACCESS_TOKEN,
                ConfiguracaoService,
            )

            token = ConfiguracaoService.get(CHAVE_MP_ACCESS_TOKEN)
            if token:
                return token
        except Exception:
            pass

        return os.environ.get("MP_ACCESS_TOKEN")

    @classmethod
    def _sdk(cls):
        """Retorna instância autenticada do SDK do Mercado Pago.

        Returns:
            Instância de mercadopago.SDK configurada com o Access Token.

        Raises:
            RuntimeError: Se o Access Token não estiver definido em nenhuma das fontes.
        """
        import mercadopago  # importação lazy — não força dependência em testes sem MP

        access_token = cls._access_token()
        if not access_token:
            raise RuntimeError(
                "Mercado Pago Access Token não configurado. "
                "Configure em /admin/ia/segredos ou use o fallback legado em Configurações."
            )
        return mercadopago.SDK(access_token)

    @classmethod
    def processar_pagamento(
        cls,
        reserva_id: int,
        dados: dict,
        partial_amount: float | None = None,
    ) -> dict:
        """Processa pagamento via Mercado Pago com suporte a pagamentos parciais.

        Cria um PaymentTransaction(PENDING) antes de chamar o MP. O UUID da
        transação é enviado como `external_reference` para identificação no webhook.

        Args:
            reserva_id: ID da Reserva no banco de dados do Sistur.
            dados: Dicionário com os dados do pagamento:
                - transaction_amount (float): Valor a cobrar (usado como fallback).
                - payment_method_id (str): Ex: "pix", "visa", "master".
                - payer (dict): Dados do pagador (email, first_name, last_name, identification).
                - token (str, somente cartão): Token gerado pelo MP Bricks.
                - installments (int, somente cartão): Número de parcelas.
                - issuer_id (str, opcional): ID da bandeira emissora.
            partial_amount: Valor parcial a cobrar. Se fornecido e > 0, prevalece
                sobre reserva.total_amount. Deve ser validado pelo chamador (admin).

        Returns:
            Response da API do Mercado Pago com campo extra `_transaction_id`
            (UUID do PaymentTransaction criado). Para PIX inclui qr_code,
            qr_code_base64 e ticket_url.

        Raises:
            ValueError: Se a Reserva não for encontrada, deletada ou sem total válido.
            RuntimeError: Se MP_ACCESS_TOKEN não estiver configurado.
        """
        from decimal import Decimal

        from sqlalchemy.orm.attributes import flag_modified

        from app.extensions import db
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.models.reservas import Reserva, ReservaStatus

        reserva = db.session.get(Reserva, reserva_id)
        if not reserva or reserva.deleted_at is not None:
            raise ValueError(f"Reserva {reserva_id} não encontrada ou foi deletada.")

        # Soft lock vencido: o cliente demorou no checkout e o estoque já pode ter
        # sido tomado por outra reserva. O pagamento NÃO é bloqueado (isso quebraria
        # PIX de última hora), mas precisa ficar visível — é a origem típica de
        # "paguei e minha vaga sumiu" e hoje não deixava nenhum rastro.
        if (
            reserva.expires_at is not None
            and reserva.status == ReservaStatus.PENDING
        ):
            expira = reserva.expires_at
            if expira.tzinfo is None:
                expira = expira.replace(tzinfo=timezone.utc)
            if expira < datetime.now(timezone.utc):
                IntegrationLogService.registrar(
                    error_source="SISTUR_QR",
                    error_code="PAYMENT_ON_EXPIRED_RESERVA",
                    error_message=(
                        f"Pagamento iniciado para a reserva #{reserva_id} com soft lock "
                        f"já expirado em {expira.isoformat()}. O estoque pode ter sido "
                        f"liberado para outro cliente — verificar overbooking."
                    ),
                    reserva_id=reserva_id,
                    customer_name=reserva.customer_name,
                    raw_payload={
                        "expires_at": expira.isoformat(),
                        "payment_method_id": dados.get("payment_method_id"),
                    },
                )

        payment_method_id = dados.get("payment_method_id", "")
        payment_type = dados.get("payment_type", "")
        is_pix = payment_method_id == "pix" or payment_type == "bank_transfer"

        # Determina o valor a cobrar:
        #   - partial_amount: pagamento parcial (validado pelo operador admin)
        #   - reserva.total_amount: total calculado pelo servidor (anti-fraude)
        #   - dados["transaction_amount"]: fallback quando total_amount não está persistido
        if partial_amount is not None and partial_amount > 0:
            amount = round(float(partial_amount), 2)
        else:
            amount = float(reserva.total_amount or dados.get("transaction_amount") or 0)

        if amount <= 0:
            raise ValueError(
                "Valor do pagamento inválido. A Reserva pode não ter um total calculado."
            )

        # ── Cria PaymentTransaction(PENDING) antes de chamar o MP ──────────────
        txn = PaymentTransaction(
            id=str(uuid.uuid4()),
            reserva_group_id=reserva.group_id,
            reserva_id=reserva.id,
            status=TransactionStatus.PENDING,
            amount=Decimal(str(amount)),
            payment_method="pix" if is_pix else payment_method_id,
        )
        db.session.add(txn)
        db.session.flush()  # garante txn.id antes de enviar ao MP

        # ── Monta payload para o Mercado Pago ───────────────────────────────────
        payment_data: dict = {
            "transaction_amount": amount,
            "description": f"Reserva #{reserva_id}",
            "payment_method_id": "pix" if is_pix else payment_method_id,
            "payer": dados.get("payer", {}),
            # UUID da PaymentTransaction como external_reference para o webhook
            "external_reference": txn.id,
        }

        # Diz ao Mercado Pago QUAL ambiente deve receber a notificação.
        #
        # Sem isto o MP usa o webhook cadastrado na conta, que é único para toda
        # a conta e aponta para produção. Uma cobrança criada no staging era paga
        # de verdade, a notificação chegava em produção, e lá a transação não
        # existia — registrada como WEBHOOK_ORPHAN_TRANSACTION e descartada. A
        # reserva do staging ficava PENDING até alguém reprocessar na mão,
        # enquanto o cliente olhava um QR code já pago.
        #
        # Ausente, o comportamento é o de antes (webhook da conta), então não
        # quebra nada onde a variável não estiver definida.
        notification_url = os.environ.get("MP_NOTIFICATION_URL", "").strip()
        if notification_url:
            payment_data["notification_url"] = notification_url

        if is_pix:
            expiry = datetime.now(timezone.utc) + timedelta(minutes=12)
            payment_data["date_of_expiration"] = expiry.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )
        else:
            token = dados.get("token", "")
            if not token:
                raise ValueError("Token do cartão ausente para pagamento com cartão.")
            if not payment_method_id:
                # payment_method_id ausente: o Brick não identificou a bandeira.
                # O token já carrega a bandeira embutida — o MP processa normalmente.
                # Se o MP rejeitar, o erro vira um IntegrationLog com o código real.
                logger.warning(
                    "payment_method_id ausente para reserva_id=%s — tentando MP sem a bandeira.",
                    reserva_id,
                )
            payment_data["token"] = token
            payment_data["installments"] = int(dados.get("installments", 1))
            if dados.get("issuer_id"):
                payment_data["issuer_id"] = dados["issuer_id"]

        # ── Chama a API do Mercado Pago ──────────────────────────────────────────
        sdk = cls._sdk()
        result = sdk.payment().create(payment_data)
        response: dict = result.get("response", {})

        mp_payment_id = str(response.get("id", ""))
        mp_status = response.get("status", "")

        # ── Atualiza PaymentTransaction com dados retornados pelo MP ────────────
        txn.mp_payment_id = mp_payment_id if mp_payment_id else None
        txn.mp_status = mp_status
        txn.mp_status_detail = response.get("status_detail")
        txn.mp_response_snapshot = {
            "id": response.get("id"),
            "status": mp_status,
            "status_detail": response.get("status_detail"),
            "date_created": response.get("date_created"),
        }

        # ── Persiste QR Code na transação e na Reserva (compatibilidade legada) ──
        if is_pix:
            point = response.get("point_of_interaction", {})
            td = point.get("transaction_data", {})
            qr = td.get("qr_code", "")
            if qr:
                txn.qr_code = qr
                txn.qr_code_base64 = td.get("qr_code_base64", "")
                txn.ticket_url = td.get("ticket_url", "")
                # Mantém reserva.pix_data para exibição legada na UI admin
                reserva.pix_data = {
                    "qr_code": qr,
                    "qr_code_base64": td.get("qr_code_base64", ""),
                    "ticket_url": td.get("ticket_url", ""),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "transaction_id": txn.id,  # referência à transação para rastreio
                }
                flag_modified(reserva, "pix_data")

        # Atualiza payment_id legado na Reserva (mantém compatibilidade da UI)
        if mp_payment_id:
            reserva.payment_id = mp_payment_id
            logger.info(
                "Reserva %d: nova transação %s, mp_payment_id=%s status=%s",
                reserva_id,
                txn.id[:8],
                mp_payment_id,
                mp_status,
            )

        # ── Aprovação imediata (pagamentos de cartão aprovados na hora) ──────────
        if mp_status == "approved":
            txn.status = TransactionStatus.APPROVED
            txn.approved_at = datetime.now(timezone.utc)
            # Atualiza payment_data legado
            reserva.payment_data = {
                "transaction_id": mp_payment_id,
                "amount_paid": amount,
                "payment_type": "pix" if is_pix else payment_method_id,
                "approval_date": datetime.now(timezone.utc).isoformat(),
            }
            db.session.commit()
            # Reserva torna-se PAID
            cls._recalcular_saldo_reserva(reserva.group_id)

            # Defesa ativa: verificar overbooking APÓS PAID (cartão imediato)
            try:
                conflitos = _verificar_overbooking_ao_pagar(reserva_id)
            except Exception as _exc:
                conflitos = []
                logger.warning("processar_pagamento: erro na verificação de overbooking | %s", _exc)
            if conflitos:
                try:
                    _criar_aviso_pago_overbooking(reserva_id, conflitos)
                except Exception as _exc:
                    logger.error("processar_pagamento: falha no aviso #1 | %s", _exc)
                cls._emitir_reembolso_automatico(
                    mp_payment_id=mp_payment_id,
                    txn_id=txn.id,
                    reserva_id=reserva_id,
                    conflitos=conflitos,
                )
            else:
                try:
                    cls.cancelar_pagamentos_concorrentes(reserva_id)
                except Exception as _exc:
                    logger.error("processar_pagamento: erro ao cancelar concorrentes | %s", _exc)

            logger.info(
                "Reserva %d: transação %s aprovada imediatamente.",
                reserva_id,
                txn.id[:8],
            )
        else:
            db.session.commit()

        # Inclui transaction_id no response para uso pelo chamador (rota admin)
        response["_transaction_id"] = txn.id
        return response

    @classmethod
    def gerar_link_pagamento_cartao(
        cls,
        reserva_id: int,
        payer_email: str | None = None,
    ) -> dict:
        """Gera um link de pagamento por cartão via Mercado Pago Preferences.

        Cria um PaymentTransaction(PENDING, payment_method='credit_card') e uma
        Preference no MP com external_reference = txn.id (UUID). Quando o cliente
        pagar, o MP envia webhook type='payment' com o mesmo external_reference,
        e _processar_webhook_novo() atualiza a reserva normalmente.

        A URL de checkout é armazenada em txn.ticket_url para consulta futura.
        A Preference expira em 24 horas a partir da criação.

        Args:
            reserva_id: ID da Reserva no banco de dados do Sistur.
            payer_email: E-mail do pagador para pré-preencher no checkout MP.
                Se não informado, usa '<reserva_id>@sistur.local' como placeholder.

        Returns:
            Dicionário com:
                - checkout_url (str): URL de checkout hosted do Mercado Pago.
                - transaction_id (str): UUID do PaymentTransaction criado.
                - preference_id (str): ID da Preference criada no MP.
                - expires_at (str): ISO 8601 UTC da expiração da preference.
                - amount (float): Valor total cobrado.

        Raises:
            ValueError: Se a Reserva não for encontrada, deletada ou sem total válido.
            RuntimeError: Se MP_ACCESS_TOKEN não estiver configurado ou MP retornar erro.
        """
        from decimal import Decimal

        from app.extensions import db
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.models.reservas import Reserva

        reserva = db.session.get(Reserva, reserva_id)
        if not reserva or reserva.deleted_at is not None:
            raise ValueError(f"Reserva {reserva_id} não encontrada ou foi deletada.")

        amount = float(reserva.total_amount or 0)
        if amount <= 0:
            raise ValueError(
                "Reserva sem valor total calculado. Edite e salve antes de gerar o link."
            )

        email = payer_email or f"{reserva_id}@sistur.local"
        nome_parts = (reserva.customer_name or "Cliente").split(" ", 1)

        # ── Cria PaymentTransaction(PENDING) antes de chamar o MP ──────────────
        txn = PaymentTransaction(
            id=str(uuid.uuid4()),
            reserva_group_id=reserva.group_id,
            reserva_id=reserva.id,
            status=TransactionStatus.PENDING,
            amount=Decimal(str(amount)),
            payment_method="credit_card",
        )
        db.session.add(txn)
        db.session.flush()  # garante txn.id antes de enviar ao MP

        # ── Monta payload da Preference ─────────────────────────────────────────
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        expires_at_str = expires_at.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")

        payer_payload: dict = {
            "name": nome_parts[0],
            "surname": nome_parts[1] if len(nome_parts) > 1 else "",
            "email": email,
        }
        cpf = reserva.customer_document
        if cpf:
            payer_payload["identification"] = {
                "type": "CPF",
                "number": cpf.replace(".", "").replace("-", "").replace("/", ""),
            }

        preference_data: dict = {
            "items": [
                {
                    "id": f"reserva-{reserva_id}",
                    "title": f"Reserva #{reserva_id}",
                    "description": reserva.customer_name or "",
                    "quantity": 1,
                    "currency_id": "BRL",
                    "unit_price": amount,
                }
            ],
            "payer": payer_payload,
            # UUID da PaymentTransaction — webhook identifica e atualiza a reserva
            "external_reference": txn.id,
            "payment_methods": {
                # Exclui PIX e boleto: este link é exclusivo para cartão
                "excluded_payment_types": [
                    {"id": "ticket"},
                    {"id": "bank_transfer"},
                ],
                "installments": 12,
            },
            "statement_descriptor": "Cachoeira Girassol",
            "expires": True,
            "expiration_date_to": expires_at_str,
        }

        # Mesmo motivo do fluxo de pagamento direto: sem isto a notificação deste
        # link vai para o webhook da conta, que aponta para um único ambiente.
        _notification_url = os.environ.get("MP_NOTIFICATION_URL", "").strip()
        if _notification_url:
            preference_data["notification_url"] = _notification_url

        # ── Chama a API do Mercado Pago ──────────────────────────────────────────
        sdk = cls._sdk()
        result = sdk.preference().create(preference_data)
        response: dict = result.get("response", {})

        preference_id = response.get("id", "")
        if not preference_id:
            db.session.rollback()
            raise RuntimeError(
                f"Mercado Pago não retornou ID da Preference. "
                f"Status HTTP: {result.get('status')}. Resposta: {response}"
            )

        init_point = response.get("init_point", "")
        sandbox_init_point = response.get("sandbox_init_point", "")

        # Detecta automaticamente se o token é de teste (começa com "TEST-").
        # Em sandbox, o MP exige sandbox_init_point; usar init_point causa rejeição.
        access_token = cls._access_token() or ""
        is_sandbox = access_token.upper().startswith("TEST-")
        checkout_url = sandbox_init_point if (is_sandbox and sandbox_init_point) else init_point

        # ── Persiste URL e snapshot na transação ────────────────────────────────
        txn.ticket_url = checkout_url
        txn.mp_response_snapshot = {
            "preference_id": preference_id,
            "init_point": init_point,
            "sandbox_init_point": sandbox_init_point,
            "is_sandbox": is_sandbox,
            "expires_at": expires_at_str,
            "date_created": response.get("date_created"),
        }

        db.session.commit()

        logger.info(
            "Reserva %d: link de pagamento por cartão gerado — txn=%s preference=%s sandbox=%s",
            reserva_id,
            txn.id[:8],
            preference_id,
            is_sandbox,
        )

        return {
            "checkout_url": checkout_url,
            "transaction_id": txn.id,
            "preference_id": preference_id,
            "expires_at": expires_at_str,
            "amount": amount,
            "is_sandbox": is_sandbox,
        }

    @classmethod
    def _recalcular_saldo_reserva(cls, group_id: str) -> None:
        """Recalcula o status da Reserva com base no saldo de transações aprovadas.

        Soma todas as PaymentTransaction com status APPROVED para o group_id e
        compara com total_amount da Reserva ativa. Atualiza o status:
          - sum >= total_amount (tolerância 0.01): PAID
          - 0 < sum < total_amount: PARTIALLY_PAID
          - sum == 0: PENDING (sem alteração em geral)

        Chamado após cada transação aprovada pelo webhook ou imediatamente no
        caso de cartões aprovados na hora.

        Args:
            group_id: UUID do grupo de versões da Reserva (Reserva.group_id).
        """
        from sqlalchemy import func

        from app.extensions import db
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.models.reservas import Reserva, ReservaStatus

        # Soma dos pagamentos aprovados para este grupo de reserva
        total_paid_raw = (
            db.session.query(func.coalesce(func.sum(PaymentTransaction.amount), 0))
            .filter(
                PaymentTransaction.reserva_group_id == group_id,
                PaymentTransaction.status == TransactionStatus.APPROVED,
            )
            .scalar()
        )
        total_paid = float(total_paid_raw or 0)

        # Versão ativa da Reserva (mais recente, não arquivada, não deletada)
        reserva = (
            db.session.query(Reserva)
            .filter(
                Reserva.group_id == group_id,
                Reserva.deleted_at.is_(None),
                Reserva.status.notin_(
                    [
                        ReservaStatus.ARCHIVED_VERSION,
                        ReservaStatus.CANCELED,
                        ReservaStatus.COMPLETED,
                    ]
                ),
            )
            .order_by(Reserva.version.desc())
            .first()
        )

        if not reserva:
            logger.debug(
                "_recalcular_saldo: sem reserva ativa para group=%s", group_id
            )
            return

        total_amount = float(reserva.total_amount or 0)
        if total_amount <= 0:
            logger.warning(
                "_recalcular_saldo: group=%s sem total_amount válido", group_id
            )
            return

        old_status = reserva.status

        if total_paid >= total_amount - 0.005:
            novo_status = ReservaStatus.PAID
        elif total_paid > 0:
            novo_status = ReservaStatus.PARTIALLY_PAID
        else:
            novo_status = ReservaStatus.PENDING

        if novo_status != old_status:
            reserva.status = novo_status
            db.session.commit()
            logger.info(
                "_recalcular_saldo: group=%s paid=%.2f/%.2f %s → %s",
                group_id,
                total_paid,
                total_amount,
                old_status.value,
                novo_status.value,
            )

            # Dispara comprovante de pagamento via Chatwoot quando reserva atinge PAID
            if novo_status == ReservaStatus.PAID and reserva.id:
                try:
                    from app.services.chatwoot_service import ChatwootService

                    ChatwootService.enviar_comprovante_pagamento(reserva.id)
                except Exception as _exc:
                    logger.error(
                        "_recalcular_saldo: falha ao enviar comprovante Chatwoot "
                        "(reserva %d): %s",
                        reserva.id,
                        _exc,
                    )

    @classmethod
    def _emitir_reembolso_automatico(
        cls,
        mp_payment_id: str,
        txn_id: str | None,
        reserva_id: int,
        conflitos: list[dict],
    ) -> dict:
        """Emite reembolso automático no Mercado Pago e cancela a Reserva por overbooking.

        Chamado APÓS a Reserva já estar em PAID. Sequência:
          1. Chama MP POST /v1/payments/{id}/refunds para devolver o valor ao cliente.
          2. Marca o PaymentTransaction local como REFUNDED (se existir).
          3. Muda a Reserva de PAID → CANCELLED e seta overbooking_extornada=True.
          4. Cria Aviso #2 para super-admins confirmando o cancelamento.

        Args:
            mp_payment_id: ID do pagamento no Mercado Pago a ser reembolsado.
            txn_id: UUID do PaymentTransaction local. None no caminho legado.
            reserva_id: ID da Reserva (já PAID) a ser cancelada após extorno.
            conflitos: Dicts das reservas conflitantes (para o corpo do aviso).

        Returns:
            Dict com resultado da operação.
        """
        from decimal import Decimal

        from app.extensions import db
        from app.models.integration_log import IntegrationLog
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.models.reservas import Reserva, ReservaStatus

        logger.warning(
            "Defesa ativa: emitindo extorno por overbooking | "
            "mp_payment_id=%s reserva_id=%d conflitos=%d",
            mp_payment_id,
            reserva_id,
            len(conflitos),
        )

        # Resolve a txn original (caminho novo) para obter amount e method
        original_txn: PaymentTransaction | None = None
        if txn_id:
            original_txn = db.session.get(PaymentTransaction, txn_id)

        # Resolve o mp_payment_id real: prefere o da txn original (já persistido pelo webhook)
        # para garantir que o ID correto chegue à API do MP mesmo que o parâmetro esteja desatualizado
        real_mp_payment_id = (
            (original_txn.mp_payment_id if original_txn and original_txn.mp_payment_id else None)
            or mp_payment_id
        )

        # Chama MP Refund API — com idempotency key para evitar duplo-reembolso
        # em caso de retry de webhook ou concorrência entre dois caminhos (novo/legado).
        refund_ok = False
        mp_refund_id: str | None = None
        refund_status_detail: str | None = None
        refund_error_msg: str | None = None
        refund_raw: dict | None = None

        if not real_mp_payment_id:
            refund_error_msg = "mp_payment_id ausente — chamada ao MP impossível."
            logger.error(
                "Defesa ativa: mp_payment_id ausente — extorno MP impossível | reserva_id=%d",
                reserva_id,
            )
        else:
            # Idempotency-Key estável: mesmo payment + mesma tentativa ⇒ mesma chave.
            # Se o webhook for reentregue ou se dois caminhos tentarem extornar,
            # o MP retorna o mesmo refund em vez de criar um novo.
            from mercadopago.config import RequestOptions

            idem_key = f"refund-{real_mp_payment_id}"
            try:
                sdk = cls._sdk()
                req_opts = RequestOptions(custom_headers={"X-Idempotency-Key": idem_key})
                refund_result = sdk.refund().create(
                    real_mp_payment_id, request_options=req_opts
                )
                http_status = refund_result.get("status")
                refund_raw = refund_result.get("response") or {}
                mp_refund_status = str(refund_raw.get("status") or "").lower()
                refund_status_detail = refund_raw.get("status_detail") or (
                    (refund_raw.get("metadata") or {}).get("status_detail")
                )
                mp_refund_id = (
                    str(refund_raw.get("id")) if refund_raw.get("id") else None
                )
                # MP retorna 201 em sucesso; status no corpo é "approved" ou "in_process".
                # "rejected" / "cancelled" / qualquer outra coisa é falha.
                refund_ok = (
                    http_status in (200, 201)
                    and mp_refund_status in ("approved", "in_process")
                    and mp_refund_id is not None
                )
                if refund_ok:
                    logger.info(
                        "Defesa ativa: MP Refund emitido | mp_payment_id=%s "
                        "refund_id=%s status=%s",
                        real_mp_payment_id,
                        mp_refund_id,
                        mp_refund_status,
                    )
                else:
                    refund_error_msg = (
                        f"MP devolveu resposta inesperada: http={http_status} "
                        f"status={mp_refund_status!r} refund_id={mp_refund_id!r}"
                    )
                    logger.error(
                        "Defesa ativa: MP Refund recusado | mp_payment_id=%s %s | raw=%s",
                        real_mp_payment_id,
                        refund_error_msg,
                        refund_raw,
                    )
            except Exception as exc:
                refund_error_msg = f"Exceção ao chamar MP: {exc!r}"
                logger.exception(
                    "Defesa ativa: falha ao emitir MP Refund | mp_payment_id=%s",
                    real_mp_payment_id,
                )

        now_utc = datetime.now(timezone.utc)

        # Mantém a txn original como APPROVED (evidência do dinheiro recebido).
        # Cria uma txn separada para representar o extorno — status REFUNDED se
        # o MP confirmou a devolução, REFUND_FAILED se falhou (trilha de auditoria
        # honesta: nunca marcar REFUNDED sem confirmação real do MP).
        refund_amount: Decimal = Decimal("0")
        refund_group_id: str | None = None

        if original_txn:
            refund_amount = original_txn.amount or Decimal("0")
            refund_group_id = original_txn.reserva_group_id
        else:
            # Caminho legado: sem txn original — infere dados da Reserva
            reserva_tmp = db.session.get(Reserva, reserva_id)
            if reserva_tmp:
                refund_amount = Decimal(str(reserva_tmp.total_amount or 0))
                refund_group_id = reserva_tmp.group_id

        if refund_group_id:
            refund_txn = PaymentTransaction(
                id=str(uuid.uuid4()),
                reserva_group_id=refund_group_id,
                reserva_id=reserva_id,
                status=(
                    TransactionStatus.REFUNDED if refund_ok
                    else TransactionStatus.REFUND_FAILED
                ),
                amount=refund_amount,
                payment_method=(original_txn.payment_method if original_txn else "pix"),
                mp_payment_id=real_mp_payment_id,
                mp_status=("refunded" if refund_ok else "refund_failed"),
                mp_status_detail=refund_status_detail,
                refund_id=mp_refund_id,
                refunded_at=(now_utc if refund_ok else None),
                mp_response_snapshot=refund_raw,
            )
            db.session.add(refund_txn)
            logger.info(
                "Defesa ativa: PaymentTransaction %s criada | "
                "refund_txn=%s amount=%s refund_id=%s",
                refund_txn.status.value,
                refund_txn.id[:8],
                refund_amount,
                mp_refund_id,
            )

        # Se falhou: grava IntegrationLog para aparecer em /admin/logs com
        # error_category='system' — exige intervenção técnica/manual do operador.
        if not refund_ok:
            try:
                reserva_for_log = db.session.get(Reserva, reserva_id)
                cust_name = reserva_for_log.customer_name if reserva_for_log else None
                db.session.add(IntegrationLog(
                    reserva_id=reserva_id,
                    customer_name=cust_name,
                    error_source="SISTUR_SYNC",
                    error_code="REFUND_FAILED",
                    error_message=(refund_error_msg or "Falha ao emitir extorno no MP.")[:4000],
                    error_category=IntegrationLog.classify("SISTUR_SYNC", "REFUND_FAILED"),
                    mp_status_detail=refund_status_detail,
                    raw_payload={
                        "mp_payment_id": real_mp_payment_id,
                        "reserva_id": reserva_id,
                        "refund_response": refund_raw,
                        "idempotency_key": f"refund-{real_mp_payment_id}" if real_mp_payment_id else None,
                    },
                ))
            except Exception:
                logger.exception(
                    "Defesa ativa: falha ao gravar IntegrationLog de REFUND_FAILED | reserva_id=%d",
                    reserva_id,
                )

        # Reverte Reserva de PAID → CANCELLED e sinaliza extorno.
        # Mesmo com refund_ok=False mantemos o cancelamento: a alternativa
        # (manter PAID sem a vaga) gera pior experiência — admin é notificado
        # para ação manual via IntegrationLog + Aviso #2 específico.
        reserva = db.session.get(Reserva, reserva_id)
        if reserva and reserva.status not in (ReservaStatus.CANCELED, ReservaStatus.COMPLETED):
            status_anterior_overbooking = reserva.status.value
            reserva.status = ReservaStatus.CANCELED
            reserva.overbooking_extornada = True
            logger.warning(
                "Defesa ativa: Reserva %d revertida PAID → CANCELLED por overbooking.",
                reserva_id,
            )

            # Registra no log de auditoria
            try:
                from app.core.audit import AuditService

                AuditService.log_update(
                    "reservas",
                    reserva_id,
                    previous_state={"status": status_anterior_overbooking},
                    new_state={
                        "status": ReservaStatus.CANCELED.value,
                        "overbooking_extornada": True,
                        "motivo": (
                            "Cancelamento automático por overbooking: "
                            "outro pagamento confirmado ocupa o mesmo slot. "
                            "Extorno ao cliente processado automaticamente."
                        ),
                    },
                    actor_id=None,
                )
            except Exception:
                logger.exception(
                    "Defesa ativa: falha ao gravar AuditLog de overbooking | reserva_id=%d",
                    reserva_id,
                )

        db.session.commit()

        # Aviso #2 — diferencia sucesso vs falha do extorno para o admin.
        try:
            _criar_avisos_overbooking(
                reserva_id,
                conflitos,
                refund_ok=refund_ok,
                mp_payment_id=real_mp_payment_id,
                refund_error=refund_error_msg,
            )
        except Exception:
            logger.exception(
                "Defesa ativa: falha ao criar aviso de cancelamento | reserva_id=%d",
                reserva_id,
            )

        resultado = (
            f"Reserva {reserva_id} extornada (refund_ok={refund_ok}) e cancelada "
            "automaticamente por overbooking."
        )
        logger.warning("Defesa ativa: %s", resultado)
        return {
            "msg": resultado,
            "refund_ok": refund_ok,
            "mp_refund_id": mp_refund_id,
            "mp_payment_id": real_mp_payment_id,
            "error": refund_error_msg,
        }

    @classmethod
    def cancelar_pagamentos_concorrentes(cls, reserva_id_aprovada: int) -> None:
        """Cancela pagamentos PENDING de reservas que causariam overbooking após aprovação.

        Chamado após um pagamento ser aprovado com sucesso (sem overbooking) para
        eliminar proativamente concorrentes que, se pagas, excederiam o estoque do recurso.

        Antes de cancelar cada concorrente, verifica se ainda existe capacidade disponível
        no recurso físico para atendê-la. Reservas que cabem dentro do estoque restante
        NÃO são canceladas — o sistema só elimina quem de fato causaria overbooking.

        Sequência por reserva concorrente:
          1. Verifica, por recurso físico, se a concorrente causaria overbooking
             (usando _verificar_estoque, que já considera a reserva aprovada como PAID).
          2. Se não há conflito real de estoque: mantém a reserva PENDING intocada.
          3. Se há conflito: cancela transações MP pendentes + a reserva local,
             registra AuditLog e envia Aviso aos super-admins.

        Args:
            reserva_id_aprovada: ID da Reserva cujo pagamento acabou de ser aprovado.
        """
        from sqlalchemy import func

        from app.extensions import db
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.models.reservas import Reserva, ReservaItemLink, ReservaResource, ReservaStatus
        from app.services.reserva_service import OverbookingError, ReservaService

        reserva_aprovada = db.session.get(Reserva, reserva_id_aprovada)
        if not reserva_aprovada:
            return

        # Recursos físicos usados pela reserva aprovada
        links_aprovada = (
            db.session.query(ReservaItemLink)
            .filter(
                ReservaItemLink.reserva_id == reserva_id_aprovada,
                ReservaItemLink.resource_id.isnot(None),
                ReservaItemLink.deleted_at.is_(None),
            )
            .all()
        )
        resource_ids = {link.resource_id for link in links_aprovada}
        if not resource_ids:
            return
        checkout_aprovada = reserva_aprovada.check_out_date or reserva_aprovada.check_in_date

        # Reservas PENDING com datas sobrepostas nos mesmos recursos.
        # Ordenadas por ID (criação) para processar as mais antigas primeiro.
        concorrentes = (
            db.session.query(Reserva)
            .join(ReservaItemLink, ReservaItemLink.reserva_id == Reserva.id)
            .filter(
                ReservaItemLink.resource_id.in_(resource_ids),
                ReservaItemLink.deleted_at.is_(None),
                Reserva.id != reserva_id_aprovada,
                Reserva.deleted_at.is_(None),
                Reserva.status == ReservaStatus.PENDING,
                func.date(Reserva.check_in_date)
                <= func.date(checkout_aprovada),
                func.date(func.coalesce(Reserva.check_out_date, Reserva.check_in_date))
                >= func.date(reserva_aprovada.check_in_date),
            )
            .distinct()
            .order_by(Reserva.id)
            .all()
        )

        if not concorrentes:
            return

        logger.info(
            "Defesa ativa: %d reserva(s) concorrente(s) no mesmo período | "
            "reserva_aprovada=%d recursos=%s",
            len(concorrentes),
            reserva_id_aprovada,
            list(resource_ids),
        )

        sdk = cls._sdk()
        for concorrente in concorrentes:
            # Verifica se a concorrente de fato esgotaria o estoque do recurso.
            # _verificar_estoque já considera a reserva aprovada como PAID (consumida),
            # portanto reflete o estado real do estoque após a aprovação.
            links_concorrente = (
                db.session.query(ReservaItemLink)
                .filter(
                    ReservaItemLink.reserva_id == concorrente.id,
                    ReservaItemLink.resource_id.in_(resource_ids),
                    ReservaItemLink.deleted_at.is_(None),
                )
                .all()
            )

            causa_overbooking = False
            for link in links_concorrente:
                resource = db.session.get(ReservaResource, link.resource_id)
                if resource is None or resource.stock_quantity is None:
                    # Recurso sem limite de estoque definido — sem restrição de capacidade
                    continue
                try:
                    ReservaService._verificar_estoque(
                        resource,
                        concorrente.check_in_date,
                        concorrente.check_out_date,
                        link.quantity,
                        exclude_reserva_id=concorrente.id,
                    )
                except OverbookingError:
                    causa_overbooking = True
                    break

            if not causa_overbooking:
                logger.info(
                    "Defesa ativa: reserva %d tem capacidade disponível após aprovação "
                    "de %d — mantida PENDING.",
                    concorrente.id,
                    reserva_id_aprovada,
                )
                continue

            # Sem capacidade: cancela transações MP e a reserva local
            txns_pendentes = (
                db.session.query(PaymentTransaction)
                .filter(
                    PaymentTransaction.reserva_group_id == concorrente.group_id,
                    PaymentTransaction.status == TransactionStatus.PENDING,
                    PaymentTransaction.mp_payment_id.isnot(None),
                )
                .all()
            )

            for txn in txns_pendentes:
                try:
                    sdk.payment().update(txn.mp_payment_id, {"status": "cancelled"})
                    logger.info(
                        "Defesa ativa: pagamento MP cancelado | "
                        "mp_payment_id=%s reserva_id=%d",
                        txn.mp_payment_id,
                        concorrente.id,
                    )
                    txn.status = TransactionStatus.CANCELLED
                    txn.mp_status = "cancelled"
                except Exception as exc:
                    logger.error(
                        "Defesa ativa: falha ao cancelar MP | "
                        "mp_payment_id=%s reserva_id=%d erro=%s",
                        txn.mp_payment_id,
                        concorrente.id,
                        exc,
                    )

            status_anterior = concorrente.status.value
            concorrente.status = ReservaStatus.CANCELED
            logger.warning(
                "Defesa ativa: reserva concorrente cancelada por falta de estoque | "
                "reserva_id=%d reserva_aprovada=%d",
                concorrente.id,
                reserva_id_aprovada,
            )

            # Registra no log de auditoria
            try:
                from app.core.audit import AuditService

                AuditService.log_update(
                    "reservas",
                    concorrente.id,
                    previous_state={"status": status_anterior},
                    new_state={
                        "status": ReservaStatus.CANCELED.value,
                        "motivo": (
                            f"Cancelamento automático por defesa ativa: "
                            f"reserva #{reserva_id_aprovada} ({reserva_aprovada.customer_name}) "
                            f"foi paga e esgotou o estoque do recurso para o mesmo período."
                        ),
                    },
                    actor_id=None,
                )
            except Exception:
                logger.exception(
                    "Defesa ativa: falha ao gravar AuditLog | reserva_id=%d",
                    concorrente.id,
                )

            # Notifica admins com o motivo claro do cancelamento automático
            try:
                _notificar_admins(
                    titulo=f"Reserva #{concorrente.id} cancelada automaticamente — estoque esgotado",
                    mensagem=(
                        f"A reserva #{concorrente.id} ({concorrente.customer_name}) "
                        f"foi cancelada automaticamente pelo sistema.\n\n"
                        f"Motivo: A reserva #{reserva_id_aprovada} ({reserva_aprovada.customer_name}) "
                        f"teve seu pagamento confirmado e esgotou o estoque do recurso "
                        f"para o período de {reserva_aprovada.check_in_date} a "
                        f"{reserva_aprovada.check_out_date}. Não há mais vagas disponíveis "
                        f"para acomodar esta reserva.\n\n"
                        f"Ver reserva cancelada: /reservas/{concorrente.id}\n"
                        f"Ver reserva aprovada: /reservas/{reserva_id_aprovada}"
                    ),
                    log_tag="Defesa ativa",
                )
            except Exception:
                logger.exception(
                    "Defesa ativa: falha ao enviar aviso de cancelamento | reserva_id=%d",
                    concorrente.id,
                )

        db.session.commit()

    @classmethod
    def processar_webhook(cls, notification: dict) -> dict:
        """Processa notificação de webhook do Mercado Pago com dual-path.

        NOVO PATH (external_reference é UUID):
          Identifica o PaymentTransaction pelo UUID em external_reference.
          Atualiza status da transação e recalcula Reserva via _recalcular_saldo_reserva().

        LEGADO PATH (external_reference ausente ou formato antigo):
          Busca a Reserva diretamente por Reserva.payment_id.
          Comportamento idêntico ao anterior à refatoração multi-transação.

        O MP envia o payment_id da notificação, mas o status real é sempre buscado
        via API (nunca confiar somente no payload do webhook).

        Args:
            notification: Payload JSON enviado pelo MP:
                { "type": "payment", "data": { "id": "12345" } }
                Também aceita: { "action": "payment.updated", "data": { "id": "12345" } }

        Returns:
            Dict com chave "msg" descrevendo o resultado do processamento.

        Raises:
            RuntimeError: Se MP_ACCESS_TOKEN não estiver configurado.
        """
        from app.extensions import db
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.models.reservas import Reserva, ReservaStatus

        notification_type = notification.get("type") or notification.get("action", "")
        if "payment" not in str(notification_type):
            logger.debug(
                "Webhook: notificação ignorada (type=%s).", notification_type
            )
            return {"msg": "Notificação ignorada."}

        payment_id = str(
            notification.get("data", {}).get("id", "")
            or notification.get("data_id", "")
        )
        if not payment_id:
            logger.warning("Webhook: payment_id ausente na notificação.")
            return {"msg": "payment_id ausente na notificação."}

        # ── Consulta o MP pela API — nunca confiar só no payload do webhook ──────
        sdk = cls._sdk()
        result = sdk.payment().get(payment_id)
        mp_payment: dict = result.get("response", {})
        mp_status = mp_payment.get("status", "")
        external_reference = mp_payment.get("external_reference") or ""

        logger.info(
            "Webhook: mp_payment_id=%s mp_status=%s external_reference=%s",
            payment_id,
            mp_status,
            (external_reference[:36] if external_reference else "—"),
        )

        # ── Roteamento: UUID (novo) ou legado ────────────────────────────────────
        if external_reference and _UUID_RE.match(external_reference.strip()):
            return cls._processar_webhook_novo(
                payment_id=payment_id,
                mp_payment=mp_payment,
                mp_status=mp_status,
                transaction_id=external_reference.strip(),
                db=db,
                PaymentTransaction=PaymentTransaction,
                TransactionStatus=TransactionStatus,
            )
        else:
            return cls._processar_webhook_legado(
                payment_id=payment_id,
                mp_payment=mp_payment,
                mp_status=mp_status,
                db=db,
                Reserva=Reserva,
                ReservaStatus=ReservaStatus,
            )

    @classmethod
    def _processar_webhook_novo(
        cls,
        payment_id: str,
        mp_payment: dict,
        mp_status: str,
        transaction_id: str,
        db,
        PaymentTransaction,
        TransactionStatus,
    ) -> dict:
        """Atualiza PaymentTransaction identificada pelo external_reference UUID.

        Caminho principal do webhook após a refatoração multi-transação.
        Após atualizar o status da transação, chama _recalcular_saldo_reserva()
        para propagar a mudança para a Reserva.

        Args:
            payment_id: ID do pagamento no Mercado Pago.
            mp_payment: Response completo da API do MP para este pagamento.
            mp_status: Status atual do pagamento no MP.
            transaction_id: UUID do PaymentTransaction (do external_reference).
            db, PaymentTransaction, TransactionStatus: injetados para testabilidade.

        Returns:
            Dict com resultado do processamento.
        """
        txn = db.session.get(PaymentTransaction, transaction_id)
        if not txn:
            logger.warning(
                "Webhook novo: PaymentTransaction %s não encontrada.", transaction_id
            )
            if mp_status == "approved":
                # Dinheiro recebido no MP sem transação correspondente no Sistur:
                # nenhuma reserva será confirmada e o cliente vai reclamar.
                # Precisa aparecer em /admin/logs para conciliação manual.
                IntegrationLogService.registrar(
                    error_source="SISTUR_SYNC",
                    error_code="WEBHOOK_ORPHAN_TRANSACTION",
                    error_message=(
                        f"Pagamento aprovado no Mercado Pago (mp_payment_id={payment_id}) "
                        f"referencia a transação {transaction_id}, que não existe no Sistur. "
                        f"O pagamento não foi vinculado a nenhuma reserva — conciliar manualmente."
                    ),
                    raw_payload={
                        "mp_payment_id": payment_id,
                        "transaction_id": transaction_id,
                        "transaction_amount": mp_payment.get("transaction_amount"),
                        "payer_email": (mp_payment.get("payer") or {}).get("email"),
                    },
                    status_detail=mp_payment.get("status_detail"),
                )
            return {"msg": f"PaymentTransaction {transaction_id} não encontrada."}

        # Atualiza campos de rastreio (mesmo que status não mude)
        txn.mp_payment_id = payment_id
        txn.mp_status = mp_status
        txn.mp_status_detail = mp_payment.get("status_detail")

        if mp_status == "approved" and txn.status != TransactionStatus.APPROVED:
            txn.status = TransactionStatus.APPROVED
            txn.approved_at = datetime.now(timezone.utc)
            txn.mp_response_snapshot = {
                "id": payment_id,
                "status": mp_status,
                "status_detail": mp_payment.get("status_detail"),
                "date_approved": mp_payment.get("date_approved"),
                "transaction_amount": mp_payment.get("transaction_amount"),
                "payment_type_id": mp_payment.get("payment_type_id"),
            }

            # Atualiza campos legados na Reserva para compatibilidade com a UI
            if txn.reserva_id:
                from app.models.reservas import Reserva

                reserva = db.session.get(Reserva, txn.reserva_id)
                if reserva:
                    reserva.payment_id = payment_id
                    reserva.payment_data = {
                        "transaction_id": payment_id,
                        "amount_paid": float(
                            mp_payment.get("transaction_amount") or txn.amount or 0
                        ),
                        "payment_type": mp_payment.get("payment_type_id") or "pix",
                        "approval_date": (
                            mp_payment.get("date_approved")
                            or datetime.now(timezone.utc).isoformat()
                        ),
                    }

            db.session.commit()
            # Confirma pagamento → reserva torna-se PAID (sempre, mesmo com overbooking)
            cls._recalcular_saldo_reserva(txn.reserva_group_id)

            # Defesa ativa: verificar overbooking APÓS PAID
            overbooking_detectado = False
            if txn.reserva_id:
                try:
                    conflitos = _verificar_overbooking_ao_pagar(txn.reserva_id)
                except Exception as _exc:
                    conflitos = []
                    logger.warning(
                        "Webhook novo: erro na verificação de overbooking | %s", _exc
                    )
                if conflitos:
                    overbooking_detectado = True
                    # Aviso #1 — reserva paga com overbooking
                    try:
                        _criar_aviso_pago_overbooking(txn.reserva_id, conflitos)
                    except Exception as _exc:
                        logger.error("Webhook novo: falha no aviso #1 | %s", _exc)
                    # Extorno automático → PAID → CANCELLED + Aviso #2
                    cls._emitir_reembolso_automatico(
                        mp_payment_id=payment_id,
                        txn_id=transaction_id,
                        reserva_id=txn.reserva_id,
                        conflitos=conflitos,
                    )

            # Cancela pagamentos PENDING de concorrentes apenas se pagamento limpo
            if txn.reserva_id and not overbooking_detectado:
                try:
                    cls.cancelar_pagamentos_concorrentes(txn.reserva_id)
                except Exception as _exc:
                    logger.error(
                        "Webhook novo: erro ao cancelar concorrentes | %s", _exc
                    )

            logger.info(
                "Webhook novo: transação %s aprovada (mp_payment_id=%s).",
                transaction_id[:8],
                payment_id,
            )
            return {"msg": f"PaymentTransaction {transaction_id[:8]}… aprovada."}

        elif mp_status in ("cancelled", "rejected"):
            status_map = {
                "cancelled": TransactionStatus.CANCELLED,
                "rejected": TransactionStatus.REJECTED,
            }
            novo_status = status_map[mp_status]
            if txn.status not in (
                TransactionStatus.APPROVED,
                TransactionStatus.REFUNDED,
            ):
                txn.status = novo_status
                txn.mp_response_snapshot = {
                    "id": payment_id,
                    "status": mp_status,
                    "status_detail": mp_payment.get("status_detail"),
                }
                db.session.commit()
                logger.info(
                    "Webhook novo: transação %s → %s.",
                    transaction_id[:8],
                    novo_status.value,
                )
                return {
                    "msg": f"PaymentTransaction {transaction_id[:8]}… → {novo_status.value}."
                }
            db.session.commit()
            return {"msg": f"Nenhuma mudança (transação já em {txn.status.value})."}

        db.session.commit()  # Persiste mp_status/mp_payment_id atualizado
        return {"msg": f"Nenhuma mudança necessária (mp_status={mp_status})."}

    @classmethod
    def _processar_webhook_legado(
        cls,
        payment_id: str,
        mp_payment: dict,
        mp_status: str,
        db,
        Reserva,
        ReservaStatus,
    ) -> dict:
        """Atualiza Reserva diretamente via Reserva.payment_id — caminho legado.

        Mantém o comportamento original para pagamentos gerados antes da
        refatoração multi-transação (sem external_reference UUID).

        Args:
            payment_id: ID do pagamento no Mercado Pago.
            mp_payment: Response completo da API do MP para este pagamento.
            mp_status: Status atual do pagamento no MP.
            db, Reserva, ReservaStatus: injetados para testabilidade.

        Returns:
            Dict com resultado do processamento.
        """
        reserva = (
            db.session.query(Reserva)
            .filter(
                Reserva.payment_id == payment_id, Reserva.deleted_at.is_(None)
            )
            .first()
        )

        if not reserva:
            logger.warning(
                "Webhook legado: nenhuma Reserva com payment_id=%s.", payment_id
            )
            if mp_status == "approved":
                # Pagamento aprovado sem reserva vinculada — mesmo cenário órfão do
                # caminho novo: cliente pagou e nada foi confirmado.
                IntegrationLogService.registrar(
                    error_source="SISTUR_SYNC",
                    error_code="WEBHOOK_ORPHAN_PAYMENT",
                    error_message=(
                        f"Pagamento aprovado no Mercado Pago (mp_payment_id={payment_id}) "
                        f"sem reserva correspondente no Sistur. O pagamento não confirmou "
                        f"nenhuma reserva — conciliar manualmente."
                    ),
                    raw_payload={
                        "mp_payment_id": payment_id,
                        "transaction_amount": mp_payment.get("transaction_amount"),
                        "payer_email": (mp_payment.get("payer") or {}).get("email"),
                    },
                    status_detail=mp_payment.get("status_detail"),
                )
            return {"msg": f"Reserva com payment_id={payment_id} não encontrada."}

        if mp_status == "approved" and reserva.status not in (
            ReservaStatus.PAID,
            ReservaStatus.CANCELED,
            ReservaStatus.COMPLETED,
        ):
            approval_date = (
                mp_payment.get("date_approved")
                or datetime.now(timezone.utc).isoformat()
            )
            amount_paid = float(
                mp_payment.get("transaction_amount") or reserva.total_amount or 0
            )
            payment_type = (
                mp_payment.get("payment_type_id")
                or mp_payment.get("payment_method_id")
                or "pix"
            )
            total_amount = float(reserva.total_amount or 0)
            if total_amount > 0 and amount_paid >= total_amount - 0.005:
                novo_status = ReservaStatus.PAID
            elif amount_paid > 0:
                novo_status = ReservaStatus.PARTIALLY_PAID
            else:
                novo_status = ReservaStatus.PAID  # fallback: sem total cadastrado, confia no MP

            # Confirma pagamento → PAID sempre, mesmo com overbooking
            reserva.status = novo_status
            reserva.payment_data = {
                "transaction_id": payment_id,
                "amount_paid": amount_paid,
                "payment_type": payment_type,
                "approval_date": approval_date,
            }
            db.session.commit()
            logger.info(
                "Webhook legado: Reserva %d → %s (paid=%.2f total=%.2f, payment_id=%s).",
                reserva.id,
                novo_status.value,
                amount_paid,
                total_amount,
                payment_id,
            )

            # Dispara comprovante via Chatwoot quando reserva atinge PAID
            if novo_status == ReservaStatus.PAID:
                try:
                    from app.services.chatwoot_service import ChatwootService

                    ChatwootService.enviar_comprovante_pagamento(reserva.id)
                except Exception as _exc:
                    logger.error(
                        "Webhook legado: falha ao enviar comprovante Chatwoot "
                        "(reserva %d): %s",
                        reserva.id,
                        _exc,
                    )

            # Defesa ativa: verificar overbooking APÓS PAID
            overbooking_detectado = False
            if novo_status == ReservaStatus.PAID:
                try:
                    conflitos = _verificar_overbooking_ao_pagar(reserva.id)
                except Exception as _exc:
                    conflitos = []
                    logger.warning(
                        "Webhook legado: erro na verificação de overbooking | %s", _exc
                    )
                if conflitos:
                    overbooking_detectado = True
                    # Aviso #1 — reserva paga com overbooking
                    try:
                        _criar_aviso_pago_overbooking(reserva.id, conflitos)
                    except Exception as _exc:
                        logger.error("Webhook legado: falha no aviso #1 | %s", _exc)
                    # Extorno automático → PAID → CANCELLED + Aviso #2
                    cls._emitir_reembolso_automatico(
                        mp_payment_id=payment_id,
                        txn_id=None,
                        reserva_id=reserva.id,
                        conflitos=conflitos,
                    )

            # Cancela concorrentes apenas se pagamento limpo
            if not overbooking_detectado:
                try:
                    cls.cancelar_pagamentos_concorrentes(reserva.id)
                except Exception as _exc:
                    logger.error(
                        "Webhook legado: erro ao cancelar concorrentes | %s", _exc
                    )

            return {"msg": f"Reserva {reserva.id} atualizada para {novo_status.value} (legado)."}

        if mp_status in ("cancelled", "rejected") and reserva.status not in (
            ReservaStatus.CANCELED,
            ReservaStatus.PAID,
        ):
            status_anterior_legado = reserva.status.value
            reserva.status = ReservaStatus.CANCELED
            logger.info(
                "Webhook legado: Reserva %d cancelada (mp_status=%s).",
                reserva.id,
                mp_status,
            )

            # Registra no log de auditoria
            try:
                from app.core.audit import AuditService

                AuditService.log_update(
                    "reservas",
                    reserva.id,
                    previous_state={"status": status_anterior_legado},
                    new_state={
                        "status": ReservaStatus.CANCELED.value,
                        "motivo": (
                            f"Cancelamento automático via webhook Mercado Pago: "
                            f"status do pagamento retornado como '{mp_status}'."
                        ),
                    },
                    actor_id=None,
                )
            except Exception:
                logger.exception(
                    "Webhook legado: falha ao gravar AuditLog | reserva_id=%d",
                    reserva.id,
                )

            db.session.commit()

            # Notifica admins com o motivo claro do cancelamento automático
            try:
                _notificar_admins(
                    titulo=f"Reserva #{reserva.id} cancelada automaticamente — webhook MP",
                    mensagem=(
                        f"A reserva #{reserva.id} ({reserva.customer_name}) "
                        f"foi cancelada automaticamente pelo sistema.\n\n"
                        f"Motivo: O Mercado Pago notificou que o pagamento foi "
                        f"'{mp_status}' (cancelado ou rejeitado pelo banco/cliente).\n\n"
                        f"Ver reserva: /reservas/{reserva.id}"
                    ),
                    log_tag="Webhook legado",
                )
            except Exception:
                logger.exception(
                    "Webhook legado: falha ao enviar aviso de cancelamento | reserva_id=%d",
                    reserva.id,
                )

            return {
                "msg": f"Reserva {reserva.id} cancelada (legado, mp={mp_status})."
            }

        return {"msg": f"Nenhuma mudança necessária (mp_status={mp_status})."}
