# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Serviço de registro de falhas de integração (IntegrationLog).

Centraliza a persistência dos erros que aparecem em `/admin/logs`. Antes deste
serviço cada chamador montava o modelo à mão, o que fez com que vários pontos de
falha do fluxo de pagamento simplesmente não registrassem nada.

Regra de ouro: **nunca propagar exceção**. O chamador está sempre em um caminho
de erro — se o log falhar, o erro original é o que importa e não pode ser
mascarado por uma falha secundária de persistência.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Conjunto fechado de origens aceitas — espelha _VALID_SOURCES do blueprint de logs.
FONTES_VALIDAS = frozenset({"MP_BRICKS", "WP_INTERNAL", "SISTUR_SYNC", "SISTUR_QR"})

_MAX_MESSAGE_LEN = 2000
_MAX_CODE_LEN = 100
_MAX_NAME_LEN = 255
_MAX_DETAIL_LEN = 100
_MAX_UA_LEN = 255


class IntegrationLogService:
    """Persiste falhas de integração de forma resiliente."""

    @staticmethod
    def registrar(
        error_source: str,
        error_code: str | None = None,
        error_message: str = "",
        reserva_id=None,
        customer_name: str | None = None,
        raw_payload: dict | None = None,
        status_detail: str | None = None,
        user_agent: str | None = None,
    ) -> int | None:
        """Grava um IntegrationLog, truncando campos e classificando o erro.

        Args:
            error_source: Origem do erro. Um de MP_BRICKS, WP_INTERNAL,
                SISTUR_SYNC ou SISTUR_QR. Valores fora do conjunto são
                normalizados para SISTUR_SYNC (nunca descartar o log).
            error_code: Código curto identificador do tipo de falha.
            error_message: Descrição legível do problema.
            reserva_id: ID da reserva afetada (aceita str/int/None).
            customer_name: Nome do cliente, para diagnóstico sem JOIN.
            raw_payload: Dados brutos para diagnóstico. Ignorado se não for dict.
            status_detail: status_detail do Mercado Pago (cc_rejected_*).
            user_agent: User-Agent de origem, quando disponível.

        Returns:
            ID do log criado, ou None se a persistência falhou.
        """
        from app.extensions import db
        from app.models.integration_log import IntegrationLog

        try:
            source = (error_source or "").strip().upper()
            if source not in FONTES_VALIDAS:
                # Fonte desconhecida não pode custar o log — registra como SISTUR_SYNC
                # e preserva o valor original na mensagem para não perder contexto.
                logger.warning("IntegrationLog: error_source inválido %r — usando SISTUR_SYNC.", error_source)
                error_message = f"[source={error_source}] {error_message}"
                source = "SISTUR_SYNC"

            rid: int | None = None
            if reserva_id is not None:
                try:
                    rid = int(reserva_id)
                except (ValueError, TypeError):
                    rid = None

            code = (str(error_code).strip()[:_MAX_CODE_LEN] or None) if error_code else None

            log = IntegrationLog(
                reserva_id=rid,
                customer_name=(str(customer_name)[:_MAX_NAME_LEN] if customer_name else None),
                error_source=source,
                error_code=code,
                error_message=(str(error_message)[:_MAX_MESSAGE_LEN] if error_message else None),
                error_category=IntegrationLog.classify(source, code),
                mp_status_detail=(str(status_detail)[:_MAX_DETAIL_LEN] if status_detail else None),
                raw_payload=raw_payload if isinstance(raw_payload, dict) else None,
                user_agent=(str(user_agent)[:_MAX_UA_LEN] if user_agent else None),
            )
            db.session.add(log)
            db.session.commit()

            logger.debug(
                "IntegrationLog criado: id=%s source=%s code=%s reserva_id=%s",
                log.id, source, code, rid,
            )
            return log.id

        except Exception as exc:
            # Rollback para não deixar a sessão poluída para o chamador.
            try:
                from app.extensions import db as _db
                _db.session.rollback()
            except Exception:
                pass
            logger.error(
                "Falha ao persistir IntegrationLog (source=%s code=%s reserva_id=%s): %s",
                error_source, error_code, reserva_id, exc,
            )
            return None

    @staticmethod
    def buscar_customer_name(reserva_id) -> str | None:
        """Busca o nome do cliente de uma reserva sem propagar exceções.

        Usado nos caminhos de erro para enriquecer o log mesmo quando o contexto
        da reserva pode estar parcialmente inválido (reserva deletada, ID textual).

        Args:
            reserva_id: ID da reserva (int, str ou None).

        Returns:
            Nome do cliente, ou None se indisponível.
        """
        if not reserva_id:
            return None
        try:
            from app.extensions import db
            from app.models.reservas import Reserva

            reserva = db.session.get(Reserva, int(reserva_id))
            return reserva.customer_name if reserva else None
        except Exception:
            return None
