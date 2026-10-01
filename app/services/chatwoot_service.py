# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
ChatwootService — envio de comprovante de pagamento via Chatwoot.

Fluxo (espelha exatamente o que foi validado via API direta):
  1. Buscar contato pelo telefone (metadata_info.telefone).
     - Não encontrado → criar contato com nome e telefone do cliente.
     - Encontrado sem nome ou com nome genérico → atualizar o nome.
  2. Buscar conversa aberta do contato no inbox correto (source.inbox_id).
     - Não existe → criar nova conversa no inbox.
  3. Enviar a mensagem de saída na conversa.

Arquitetura:
  Serviço puro — sem imports do Flask (sem request/session/current_app).
  Lê credenciais via os.environ para compatibilidade com Cython.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_CHATWOOT_INTERNAL_URL = "http://chatwoot-rails-1:3000"


class ChatwootService:
    """Serviço de integração com a API do Chatwoot para envio de comprovantes."""

    # ─── Configuração ──────────────────────────────────────────────────────────

    @staticmethod
    def _api_token() -> str:
        """Retorna o token de acesso à API do Chatwoot lido do ambiente.

        Returns:
            Token de API configurado em CHATWOOT_API_TOKEN.
        """
        return os.environ.get("CHATWOOT_API_TOKEN", "")

    @staticmethod
    def _account_id() -> str:
        """Retorna o ID da conta Chatwoot lido do ambiente.

        Returns:
            Account ID configurado em CHATWOOT_ACCOUNT_ID (padrão: "1").
        """
        return os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

    @staticmethod
    def _base_url() -> str:
        """Retorna a URL base da API interna do Chatwoot.

        Returns:
            URL interna do container Chatwoot.
        """
        return os.environ.get("CHATWOOT_INTERNAL_URL", _CHATWOOT_INTERNAL_URL)

    # ─── HTTP helpers ──────────────────────────────────────────────────────────

    @classmethod
    def _get(cls, path: str) -> Optional[dict]:
        """Executa GET autenticado na API do Chatwoot.

        Args:
            path: Caminho relativo à URL base.

        Returns:
            Dicionário com o JSON de resposta, ou None em caso de erro.
        """
        url = f"{cls._base_url()}{path}"
        req = urllib.request.Request(url, headers={
            "api_access_token": cls._api_token(),
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("ChatwootService GET %s falhou: %s", path, exc)
            return None

    @classmethod
    def _post(cls, path: str, payload: dict) -> Optional[dict]:
        """Executa POST autenticado na API do Chatwoot.

        Args:
            path: Caminho relativo à URL base.
            payload: Corpo da requisição como dicionário.

        Returns:
            Dicionário com o JSON de resposta, ou None em caso de erro.
        """
        url = f"{cls._base_url()}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "api_access_token": cls._api_token(),
            "Content-Type": "application/json",
        }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
                logger.debug("ChatwootService POST %s HTTP %d: %s", path, exc.code, body)
                return body
            except Exception:
                pass
            logger.warning("ChatwootService POST %s falhou: %s", path, exc)
            return None
        except Exception as exc:
            logger.warning("ChatwootService POST %s falhou: %s", path, exc)
            return None

    @classmethod
    def _patch(cls, path: str, payload: dict) -> Optional[dict]:
        """Executa PATCH autenticado na API do Chatwoot.

        Args:
            path: Caminho relativo à URL base.
            payload: Campos a atualizar.

        Returns:
            Dicionário com o JSON de resposta, ou None em caso de erro.
        """
        url = f"{cls._base_url()}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "api_access_token": cls._api_token(),
            "Content-Type": "application/json",
        }, method="PATCH")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("ChatwootService PATCH %s falhou: %s", path, exc)
            return None

    # ─── Contato ───────────────────────────────────────────────────────────────

    @classmethod
    def _normalizar_e164(cls, telefone: str) -> str:
        """Converte telefone para E.164 (+55XXXXXXXXXXX).

        Args:
            telefone: Número em qualquer formato.

        Returns:
            Número no formato +55..., ex: "+5561999990000".
        """
        digits = re.sub(r"\D", "", telefone)
        if not digits.startswith("55"):
            digits = "55" + digits
        return "+" + digits

    @classmethod
    def _buscar_contato(cls, telefone: str) -> Optional[dict]:
        """Busca contato no Chatwoot pelo telefone.

        Tenta variantes do número (com/sem +55, com/sem nono dígito) para
        maximizar a chance de encontrar contatos já existentes.

        Args:
            telefone: Número de telefone em qualquer formato.

        Returns:
            Dicionário do primeiro contato encontrado, ou None.
        """
        digits = re.sub(r"\D", "", telefone)
        local = digits[2:] if digits.startswith("55") and len(digits) >= 12 else digits
        local_sem9 = (local[:2] + local[3:]) if len(local) == 11 and local[2] == "9" else None

        candidatos: list[str] = []
        vistos: list[str] = []
        for variante in ([local, local_sem9] if local_sem9 else [local]):
            if variante and variante not in vistos:
                vistos.append(variante)
                candidatos.append("55" + variante)
                candidatos.append(variante)
        if len(local) >= 8:
            suf8 = local[-8:]
            if suf8 not in vistos:
                candidatos.append(suf8)

        account_id = cls._account_id()
        for candidato in candidatos:
            q = urllib.parse.quote(candidato)
            data = cls._get(
                f"/api/v1/accounts/{account_id}/contacts/search"
                f"?q={q}&include_contacts=true&page=1"
            )
            if not data:
                continue
            payload = data.get("payload", [])
            contatos = payload if isinstance(payload, list) else payload.get("contacts", [])
            if contatos:
                return contatos[0]

        return None

    @classmethod
    def _criar_contato(cls, nome: str, telefone: str) -> Optional[dict]:
        """Cria novo contato no Chatwoot com nome e telefone em E.164.

        Args:
            nome: Nome completo do cliente.
            telefone: Número de telefone em qualquer formato.

        Returns:
            Dicionário do contato criado, ou None em caso de falha.
        """
        account_id = cls._account_id()
        phone_e164 = cls._normalizar_e164(telefone)
        resultado = cls._post(
            f"/api/v1/accounts/{account_id}/contacts",
            {"name": nome, "phone_number": phone_e164},
        )
        contato = resultado
        if resultado and not resultado.get("id"):
            contato = (resultado.get("payload") or {}).get("contact") or resultado
        if contato and contato.get("id"):
            logger.info(
                "ChatwootService: contato id=%d nome=%r tel=%s",
                contato["id"], nome, phone_e164,
            )
            return contato
        logger.warning(
            "ChatwootService: falha ao criar contato nome=%r tel=%s — %s",
            nome, phone_e164, resultado,
        )
        return None

    @classmethod
    def _atualizar_nome_contato(cls, contact_id: int, nome: str) -> None:
        """Atualiza o nome de um contato existente.

        Chamado quando o contato foi criado automaticamente pelo WhatsApp
        e ainda não tem um nome legível associado.

        Args:
            contact_id: ID do contato no Chatwoot.
            nome: Nome do cliente vindo da reserva.
        """
        account_id = cls._account_id()
        cls._patch(f"/api/v1/accounts/{account_id}/contacts/{contact_id}", {"name": nome})
        logger.info("ChatwootService: nome do contato %d atualizado para %r", contact_id, nome)

    # ─── Conversa e Mensagem ───────────────────────────────────────────────────

    @classmethod
    def _obter_conversa_no_inbox(cls, contact_id: int, inbox_id: int) -> Optional[int]:
        """Retorna o ID da conversa aberta mais recente do contato no inbox.

        Args:
            contact_id: ID do contato no Chatwoot.
            inbox_id: ID do inbox a filtrar.

        Returns:
            ID da conversa, ou None se não houver nenhuma.
        """
        account_id = cls._account_id()
        data = cls._get(f"/api/v1/accounts/{account_id}/contacts/{contact_id}/conversations")
        if not data:
            return None

        conversas = data.get("payload", [])
        no_inbox = [
            c for c in conversas
            if c.get("inbox_id") == inbox_id and c.get("status") == "open"
        ]
        if not no_inbox:
            return None

        return sorted(no_inbox, key=lambda c: c.get("id", 0), reverse=True)[0]["id"]

    @classmethod
    def _criar_conversa(cls, contact_id: int, inbox_id: int) -> Optional[int]:
        """Cria nova conversa para o contato no inbox especificado.

        Args:
            contact_id: ID do contato no Chatwoot.
            inbox_id: ID do inbox onde criar a conversa.

        Returns:
            ID da conversa criada, ou None em caso de falha.
        """
        account_id = cls._account_id()
        resultado = cls._post(
            f"/api/v1/accounts/{account_id}/conversations",
            {"contact_id": contact_id, "inbox_id": inbox_id},
        )
        if resultado and resultado.get("id"):
            logger.info(
                "ChatwootService: conversa criada id=%d (contact=%d inbox=%d)",
                resultado["id"], contact_id, inbox_id,
            )
            return resultado["id"]
        logger.warning(
            "ChatwootService: falha ao criar conversa contact=%d inbox=%d — %s",
            contact_id, inbox_id, resultado,
        )
        return None

    @classmethod
    def _enviar_mensagem(cls, conversation_id: int, mensagem: str) -> bool:
        """Envia mensagem de saída em uma conversa.

        Args:
            conversation_id: ID da conversa no Chatwoot.
            mensagem: Texto da mensagem.

        Returns:
            True se enviada com sucesso, False caso contrário.
        """
        account_id = cls._account_id()
        resultado = cls._post(
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages",
            {"content": mensagem, "message_type": "outgoing", "private": False},
        )
        if resultado and resultado.get("id"):
            logger.info(
                "ChatwootService: mensagem %d enviada na conversa %d",
                resultado["id"], conversation_id,
            )
            return True
        logger.warning(
            "ChatwootService: falha ao enviar mensagem na conversa %d — %s",
            conversation_id, resultado,
        )
        return False

    # ─── Formatação do Comprovante ─────────────────────────────────────────────

    @classmethod
    def _formatar_comprovante(cls, reserva, source, payment_data: Optional[dict]) -> str:
        """Monta o texto formal do comprovante de pagamento.

        Usa formatação WhatsApp Markdown (*bold*, _italic_) para boa
        legibilidade no canal de mensageria.

        Args:
            reserva: Instância de Reserva com items carregados.
            source: Instância de ReservaSource do venue.
            payment_data: Dicionário com dados do pagamento (pode ser None).

        Returns:
            String com o comprovante formatado.
        """
        nome_venue = (
            (source.report_name or "").strip()
            or (source.display_name or "").strip()
            or source.name
        )

        nome_cliente = reserva.customer_name or "Cliente"
        documento = reserva.customer_document or ""

        def _fmt_data(dt) -> str:
            if not dt:
                return "—"
            return dt.strftime("%d/%m/%Y") if hasattr(dt, "strftime") else str(dt)

        def _fmt_brl(valor) -> str:
            v = float(valor or 0)
            return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

        data_checkin = _fmt_data(reserva.check_in_date)
        data_checkout = _fmt_data(reserva.check_out_date) if reserva.check_out_date else None

        # Itens
        linhas_itens: list[str] = []
        for link in (reserva.items or []):
            if link.deleted_at is not None:
                continue
            nome_item = (link.item.name if link.item else None) or f"Item #{link.item_id}"
            qty = link.quantity or 1
            total_item = float(link.item_total or (link.locked_price * qty) or 0)
            linhas_itens.append(
                f"  • {nome_item} (x{qty}) — {_fmt_brl(total_item)}"
            )
        bloco_itens = "\n".join(linhas_itens) if linhas_itens else "  (sem itens)"

        # Documento formatado
        doc_linha = ""
        if documento:
            doc_limpo = re.sub(r"\D", "", documento)
            if len(doc_limpo) == 11:
                doc_fmt = f"{doc_limpo[:3]}.{doc_limpo[3:6]}.{doc_limpo[6:9]}-{doc_limpo[9:]}"
            elif len(doc_limpo) == 14:
                doc_fmt = f"{doc_limpo[:2]}.{doc_limpo[2:5]}.{doc_limpo[5:8]}/{doc_limpo[8:12]}-{doc_limpo[12:]}"
            else:
                doc_fmt = documento
            doc_linha = f"\nDocumento: {doc_fmt}"

        saida_linha = f"\nData de saída: {data_checkout}" if data_checkout else ""

        desconto = float(reserva.discount_amount or 0)
        desconto_linha = f"\nDesconto: - {_fmt_brl(desconto)}" if desconto > 0.01 else ""

        taxa = float(reserva.service_fee_amount or 0)
        taxa_linha = f"\nTaxa de serviço: {_fmt_brl(taxa)}" if taxa > 0.01 else ""

        # Pagamento
        metodo = "—"
        data_pagamento = "—"
        if payment_data:
            tipo = (payment_data.get("payment_type") or "").lower()
            if "pix" in tipo:
                metodo = "PIX"
            elif "credit" in tipo:
                metodo = "Cartão de Crédito"
            elif "debit" in tipo:
                metodo = "Cartão de Débito"
            else:
                metodo = tipo.upper() or "—"

            data_aprov_raw = payment_data.get("approval_date")
            if data_aprov_raw:
                try:
                    dt = datetime.fromisoformat(str(data_aprov_raw).replace("Z", "+00:00"))
                    try:
                        from zoneinfo import ZoneInfo
                        dt = dt.astimezone(ZoneInfo("America/Sao_Paulo"))
                    except Exception:
                        pass
                    data_pagamento = dt.strftime("%d/%m/%Y às %H:%M")
                except Exception:
                    data_pagamento = str(data_aprov_raw)[:16]

        return (
            f"✅ *COMPROVANTE DE PAGAMENTO*\n"
            f"_{nome_venue}_\n"
            f"{'━' * 30}\n\n"
            f"Prezado(a) *{nome_cliente}*,\n\n"
            f"Confirmamos o recebimento do pagamento referente à sua reserva.\n\n"
            f"📋 *DADOS DA RESERVA*\n"
            f"Nº da reserva: *#{reserva.id}*\n"
            f"Data de entrada: {data_checkin}"
            f"{saida_linha}"
            f"{doc_linha}\n\n"
            f"🛒 *ITENS ADQUIRIDOS*\n"
            f"{bloco_itens}\n\n"
            f"💰 *RESUMO FINANCEIRO*\n"
            f"Subtotal: {_fmt_brl(reserva.subtotal_amount)}"
            f"{desconto_linha}"
            f"{taxa_linha}\n"
            f"*Total pago: {_fmt_brl(reserva.total_amount)}*\n\n"
            f"💳 *FORMA DE PAGAMENTO*\n"
            f"Método: {metodo}\n"
            f"Data: {data_pagamento}\n\n"
            f"{'━' * 30}\n"
            f"Este comprovante confirma que seu pagamento foi processado com sucesso. "
            f"Guarde esta mensagem como prova de pagamento.\n\n"
            f"*{nome_venue}* agradece a sua preferência. "
            f"Em caso de dúvidas, entre em contato por esta mesma conversa."
        )

    # ─── Método principal ──────────────────────────────────────────────────────

    @classmethod
    def enviar_comprovante_pagamento(cls, reserva_id: int) -> bool:
        """Envia o comprovante formal de pagamento ao cliente via Chatwoot.

        Fluxo:
          1. Carrega Reserva com items e source.
          2. Extrai telefone de metadata_info.telefone.
          3. Busca contato pelo telefone; cria se não existir.
          4. Atualiza nome do contato se estiver vazio ou genérico.
          5. Busca conversa aberta no inbox do venue; cria se não existir.
          6. Envia o comprovante formatado.

        Args:
            reserva_id: ID da Reserva a ser confirmada.

        Returns:
            True se enviado com sucesso, False caso contrário.
        """
        if not cls._api_token():
            logger.warning(
                "ChatwootService: CHATWOOT_API_TOKEN não configurado — comprovante não enviado."
            )
            return False

        try:
            from app.extensions import db
            from app.models.reservas import Reserva

            reserva = db.session.get(Reserva, reserva_id)
            if not reserva:
                logger.warning("ChatwootService: Reserva %d não encontrada.", reserva_id)
                return False

            source = reserva.source
            if not source:
                logger.warning(
                    "ChatwootService: Reserva %d sem source — comprovante não enviado.",
                    reserva_id,
                )
                return False

            inbox_id = source.inbox_id
            if not inbox_id:
                logger.warning(
                    "ChatwootService: source '%s' sem inbox_id configurado — "
                    "configure o inbox em /reservas/admin/sources.",
                    source.name,
                )
                return False

            # Telefone em metadata_info.telefone
            meta = reserva.metadata_info or {}
            telefone = (
                meta.get("telefone") or meta.get("phone") or meta.get("tel") or ""
            ).strip()
            if not telefone:
                logger.info(
                    "ChatwootService: Reserva %d sem telefone em metadata_info — "
                    "comprovante não enviado.",
                    reserva_id,
                )
                return False

            # ── Passo 1: localiza ou cria o contato ───────────────────────────
            contato = cls._buscar_contato(telefone)
            if not contato:
                logger.info(
                    "ChatwootService: contato não encontrado para tel=%s — criando (reserva %d).",
                    telefone, reserva_id,
                )
                contato = cls._criar_contato(reserva.customer_name, telefone)

            if not contato:
                logger.warning(
                    "ChatwootService: não foi possível localizar nem criar contato "
                    "para tel=%s (reserva %d).",
                    telefone, reserva_id,
                )
                return False

            contact_id = contato["id"]

            # Atualiza nome se vazio ou parece número de telefone
            nome_atual = (contato.get("name") or "").strip()
            nome_reserva = (reserva.customer_name or "").strip()
            if nome_reserva and (not nome_atual or bool(re.fullmatch(r"[\d\s\+\-\(\)]+", nome_atual))):
                cls._atualizar_nome_contato(contact_id, nome_reserva)

            # ── Passo 2: obtém ou cria conversa no inbox do venue ─────────────
            conversation_id = cls._obter_conversa_no_inbox(contact_id, inbox_id)
            if not conversation_id:
                conversation_id = cls._criar_conversa(contact_id, inbox_id)

            if not conversation_id:
                logger.warning(
                    "ChatwootService: não foi possível obter/criar conversa "
                    "para contact=%d inbox=%d (reserva %d).",
                    contact_id, inbox_id, reserva_id,
                )
                return False

            # ── Passo 3: envia o comprovante ──────────────────────────────────
            _ = reserva.items  # garante que items estão carregados
            mensagem = cls._formatar_comprovante(reserva, source, reserva.payment_data or {})
            return cls._enviar_mensagem(conversation_id, mensagem)

        except Exception as exc:
            logger.error(
                "ChatwootService: erro inesperado ao enviar comprovante da reserva %d: %s",
                reserva_id, exc, exc_info=True,
            )
            return False
