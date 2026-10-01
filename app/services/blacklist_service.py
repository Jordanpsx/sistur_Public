# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
BlacklistService — regras de bloqueio de clientes no módulo de Reservas.

Mantém a validação centralizada para que frontends externos, bot e operação
interna usem a mesma barreira definitiva antes de criar uma reserva.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.audit import AuditService
from app.extensions import db
from app.models.reservas import BlacklistEntry, ReservaSource
from app.services.base import BaseService


class ClientBlacklistedError(ValueError):
    """Lançada quando um cliente está impedido de reservar em um local."""

    code = "CLIENT_BANNED"

    def __init__(self, message: str, entry_id: int | None = None, matched_fields: list[str] | None = None) -> None:
        """Inicializa a exceção com metadados seguros para APIs.

        Args:
            message: Mensagem pública/administrativa do bloqueio.
            entry_id: ID interno da entrada que causou o bloqueio.
            matched_fields: Campos que acionaram a regra.
        """
        super().__init__(message)
        self.entry_id = entry_id
        self.matched_fields = matched_fields or []


class BlacklistService(BaseService):
    """Gerencia banimentos e consulta bloqueios ativos."""

    _CAMPOS_SNAPSHOT = [
        "id",
        "name",
        "cpf",
        "phone",
        "email",
        "block_by_name",
        "block_by_cpf",
        "block_by_phone",
        "block_by_email",
        "deleted_at",
    ]

    @classmethod
    def listar(cls) -> list[BlacklistEntry]:
        """Lista banimentos ativos.

        Returns:
            Lista de entradas ordenada por nome.
        """
        return (
            db.session.query(BlacklistEntry)
            .filter(BlacklistEntry.deleted_at.is_(None))
            .order_by(BlacklistEntry.name.asc())
            .all()
        )

    @classmethod
    def listar_sources_disponiveis(cls) -> list[ReservaSource]:
        """Lista locais ativos disponíveis para vincular bloqueios.

        Returns:
            Lista de ReservaSource ativos ordenada por nome.
        """
        return (
            db.session.query(ReservaSource)
            .filter(ReservaSource.deleted_at.is_(None), ReservaSource.is_active.is_(True))
            .order_by(ReservaSource.name.asc())
            .all()
        )

    @classmethod
    def obter(cls, entry_id: int) -> BlacklistEntry:
        """Busca um banimento ativo.

        Args:
            entry_id: ID da entrada.

        Returns:
            Entrada encontrada.

        Raises:
            ValueError: Se a entrada não existir ou estiver removida.
        """
        entry = db.session.get(BlacklistEntry, entry_id)
        if not entry or entry.deleted_at is not None:
            raise ValueError("Banimento não encontrado.")
        return entry

    @classmethod
    def criar(
        cls,
        *,
        name: str,
        cpf: str,
        phone: str | None = None,
        email: str | None = None,
        block_by_name: bool = False,
        block_by_cpf: bool = True,
        block_by_phone: bool = False,
        block_by_email: bool = False,
        source_ids: list[int] | None = None,
        ator_id: int | None = None,
    ) -> BlacklistEntry:
        """Cria um banimento ativo.

        Args:
            name: Nome de referência da pessoa bloqueada.
            cpf: CPF de referência da pessoa bloqueada.
            phone: Telefone opcional para regra por telefone.
            email: E-mail opcional para regra por e-mail.
            block_by_name: Se True, bloqueia por nome normalizado.
            block_by_cpf: Se True, bloqueia por CPF normalizado.
            block_by_phone: Se True, bloqueia por telefone normalizado.
            block_by_email: Se True, bloqueia por e-mail normalizado.
            source_ids: IDs dos locais onde a regra se aplica.
            ator_id: ID do operador para auditoria.

        Returns:
            Entrada persistida.

        Raises:
            ValueError: Se dados obrigatórios ou regras forem inválidos.
        """
        entry = BlacklistEntry(
            name=cls._require(name.strip(), "name"),
            cpf=cls._require(cls._digits(cpf), "cpf"),
            phone=cls._digits(phone) or None,
            email=cls._normalize_email(email) or None,
            block_by_name=block_by_name,
            block_by_cpf=block_by_cpf,
            block_by_phone=block_by_phone,
            block_by_email=block_by_email,
        )
        entry.sources = cls._carregar_sources(source_ids or [])
        cls._validar_entry(entry)

        db.session.add(entry)
        db.session.flush()
        AuditService.log_create(
            "reservas_blacklist",
            entity_id=entry.id,
            new_state=cls._snapshot_entry(entry),
            actor_id=ator_id,
        )
        db.session.commit()
        return entry

    @classmethod
    def atualizar(
        cls,
        entry_id: int,
        *,
        name: str,
        cpf: str,
        phone: str | None = None,
        email: str | None = None,
        block_by_name: bool = False,
        block_by_cpf: bool = True,
        block_by_phone: bool = False,
        block_by_email: bool = False,
        source_ids: list[int] | None = None,
        ator_id: int | None = None,
    ) -> BlacklistEntry:
        """Atualiza um banimento ativo.

        Args:
            entry_id: ID da entrada.
            name: Nome de referência.
            cpf: CPF de referência.
            phone: Telefone opcional.
            email: E-mail opcional.
            block_by_name: Se True, bloqueia por nome.
            block_by_cpf: Se True, bloqueia por CPF.
            block_by_phone: Se True, bloqueia por telefone.
            block_by_email: Se True, bloqueia por e-mail.
            source_ids: IDs dos locais vinculados.
            ator_id: ID do operador para auditoria.

        Returns:
            Entrada atualizada.

        Raises:
            ValueError: Se a entrada não existir ou a validação falhar.
        """
        entry = db.session.get(BlacklistEntry, entry_id)
        if not entry or entry.deleted_at is not None:
            raise ValueError("Banimento não encontrado.")

        previous = cls._snapshot_entry(entry)
        entry.name = cls._require(name.strip(), "name")
        entry.cpf = cls._require(cls._digits(cpf), "cpf")
        entry.phone = cls._digits(phone) or None
        entry.email = cls._normalize_email(email) or None
        entry.block_by_name = block_by_name
        entry.block_by_cpf = block_by_cpf
        entry.block_by_phone = block_by_phone
        entry.block_by_email = block_by_email
        entry.sources = cls._carregar_sources(source_ids or [])
        cls._validar_entry(entry)

        AuditService.log_update(
            "reservas_blacklist",
            entity_id=entry.id,
            previous_state=previous,
            new_state=cls._snapshot_entry(entry),
            actor_id=ator_id,
        )
        db.session.commit()
        return entry

    @classmethod
    def deletar(cls, entry_id: int, *, ator_id: int | None = None) -> None:
        """Remove logicamente um banimento.

        Args:
            entry_id: ID da entrada.
            ator_id: ID do operador para auditoria.

        Raises:
            ValueError: Se a entrada não existir.
        """
        entry = db.session.get(BlacklistEntry, entry_id)
        if not entry or entry.deleted_at is not None:
            raise ValueError("Banimento não encontrado.")

        previous = cls._snapshot_entry(entry)
        entry.deleted_at = datetime.now(timezone.utc)
        AuditService.log_delete(
            "reservas_blacklist",
            entity_id=entry.id,
            previous_state=previous,
            actor_id=ator_id,
        )
        db.session.commit()

    @classmethod
    def is_client_blacklisted(
        cls,
        *,
        name: str | None,
        cpf: str | None,
        phone: str | None,
        email: str | None,
        location_id: int,
    ) -> tuple[bool, BlacklistEntry | None, list[str]]:
        """Verifica se um cliente está bloqueado para um local.

        Args:
            name: Nome recebido na tentativa de reserva.
            cpf: CPF/documento recebido na tentativa de reserva.
            phone: Telefone recebido na tentativa de reserva.
            email: E-mail recebido na tentativa de reserva.
            location_id: ID de ReservaSource alvo.

        Returns:
            Tupla ``(bloqueado, entrada, campos)``. ``entrada`` é None quando
            não houver bloqueio.
        """
        norm_name = cls._normalize_name(name)
        norm_cpf = cls._digits(cpf)
        norm_phone = cls._digits(phone)
        norm_email = cls._normalize_email(email)

        entries = (
            db.session.query(BlacklistEntry)
            .join(BlacklistEntry.sources)
            .filter(
                BlacklistEntry.deleted_at.is_(None),
                ReservaSource.id == location_id,
            )
            .all()
        )

        for entry in entries:
            matched: list[str] = []
            if entry.block_by_name and norm_name and norm_name == cls._normalize_name(entry.name):
                matched.append("name")
            if entry.block_by_cpf and norm_cpf and norm_cpf == cls._digits(entry.cpf):
                matched.append("cpf")
            if entry.block_by_phone and norm_phone and norm_phone == cls._digits(entry.phone):
                matched.append("phone")
            if entry.block_by_email and norm_email and norm_email == cls._normalize_email(entry.email):
                matched.append("email")
            if matched:
                return True, entry, matched

        return False, None, []

    @classmethod
    def assert_client_allowed(
        cls,
        *,
        name: str | None,
        cpf: str | None,
        phone: str | None,
        email: str | None,
        location_id: int,
    ) -> None:
        """Interrompe a operação se o cliente estiver banido.

        Args:
            name: Nome recebido.
            cpf: CPF/documento recebido.
            phone: Telefone recebido.
            email: E-mail recebido.
            location_id: ID de ReservaSource alvo.

        Raises:
            ClientBlacklistedError: Se qualquer regra ativa do local corresponder.
        """
        blocked, entry, matched = cls.is_client_blacklisted(
            name=name,
            cpf=cpf,
            phone=phone,
            email=email,
            location_id=location_id,
        )
        if blocked:
            raise ClientBlacklistedError(
                "Cliente sem autorização para realizar reservas neste local.",
                entry_id=entry.id if entry else None,
                matched_fields=matched,
            )

    @classmethod
    def _validar_entry(cls, entry: BlacklistEntry) -> None:
        """Valida regras mínimas de um banimento.

        Args:
            entry: Entrada a validar.

        Raises:
            ValueError: Se nenhuma regra estiver ativa ou faltar dado para regra ativa.
        """
        if not any([entry.block_by_name, entry.block_by_cpf, entry.block_by_phone, entry.block_by_email]):
            raise ValueError("Selecione ao menos um critério de bloqueio.")
        if entry.block_by_cpf and not entry.cpf:
            raise ValueError("CPF é obrigatório para bloqueio por CPF.")
        if entry.block_by_phone and not entry.phone:
            raise ValueError("Telefone é obrigatório para bloqueio por telefone.")
        if entry.block_by_email and not entry.email:
            raise ValueError("E-mail é obrigatório para bloqueio por e-mail.")
        if not entry.sources:
            raise ValueError("Selecione ao menos um local para aplicar o bloqueio.")

    @classmethod
    def _carregar_sources(cls, source_ids: list[int]) -> list[ReservaSource]:
        """Carrega locais ativos por ID.

        Args:
            source_ids: Lista de IDs enviados pelo formulário.

        Returns:
            Lista de ReservaSource ativos.

        Raises:
            ValueError: Se nenhum local válido for encontrado.
        """
        try:
            ids = sorted({int(source_id) for source_id in source_ids if str(source_id).strip()})
        except (TypeError, ValueError):
            raise ValueError("Um ou mais locais selecionados são inválidos.")
        if not ids:
            raise ValueError("Selecione ao menos um local para aplicar o bloqueio.")
        sources = (
            db.session.query(ReservaSource)
            .filter(
                ReservaSource.id.in_(ids),
                ReservaSource.deleted_at.is_(None),
                ReservaSource.is_active.is_(True),
            )
            .order_by(ReservaSource.name.asc())
            .all()
        )
        if len(sources) != len(ids):
            raise ValueError("Um ou mais locais selecionados são inválidos.")
        return sources

    @classmethod
    def _snapshot_entry(cls, entry: BlacklistEntry) -> dict[str, Any]:
        """Gera snapshot serializável com locais vinculados.

        Args:
            entry: Banimento.

        Returns:
            Dict pronto para auditoria.
        """
        snap = cls._snapshot(entry, cls._CAMPOS_SNAPSHOT)
        snap["source_ids"] = [source.id for source in entry.sources]
        return snap

    @staticmethod
    def _digits(value: str | None) -> str:
        """Normaliza um valor mantendo apenas dígitos."""
        return re.sub(r"\D+", "", value or "")

    @staticmethod
    def _normalize_email(value: str | None) -> str:
        """Normaliza e-mail para comparação case-insensitive."""
        return (value or "").strip().lower()

    @staticmethod
    def _normalize_name(value: str | None) -> str:
        """Normaliza nome removendo espaços duplicados e diferença de caixa."""
        return " ".join((value or "").strip().lower().split())
