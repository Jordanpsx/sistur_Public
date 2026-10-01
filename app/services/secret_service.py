# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""SecretService — cofre interno para credenciais sensíveis.

O service armazena segredos criptografados no banco e mantém a chave mestra fora
do banco (`SISTUR_SECRETS_KEY`). Snapshots de auditoria nunca incluem o valor
bruto nem o ciphertext completo.
"""

from __future__ import annotations

import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.audit import AuditService
from app.extensions import db
from app.models.secret import Secret
from app.services.base import BaseService


_SECRET_AUDIT_FIELDS = [
    "id",
    "key",
    "provider",
    "label",
    "last4",
    "ativo",
    "updated_by_id",
]


class SecretConfigError(RuntimeError):
    """Erro de configuração do cofre de segredos."""


class SecretService(BaseService):
    """Gerencia segredos criptografados para integrações sensíveis."""

    MERCADOPAGO_ACCESS_TOKEN = "mercadopago.access_token"
    CHATWOOT_API_TOKEN = "chatwoot.api_token"

    @staticmethod
    def get(key: str, default: Any = None) -> str | Any:
        """Retorna o valor bruto de um segredo ativo.

        Args:
            key: Identificador do segredo.
            default: Valor retornado se o segredo não existir ou estiver inativo.

        Returns:
            Valor descriptografado ou `default`.

        Raises:
            SecretConfigError: Se a chave mestra estiver ausente/inválida ou o
                valor criptografado não puder ser descriptografado.
        """
        secret = SecretService._buscar_ativo(key)
        if not secret:
            return default
        return SecretService._decrypt(secret.value_encrypted)

    @staticmethod
    def exists(key: str) -> bool:
        """Indica se há segredo ativo para a chave informada."""
        return SecretService._buscar_ativo(key) is not None

    @staticmethod
    def masked(key: str) -> str | None:
        """Retorna representação mascarada do segredo sem expor valor bruto.

        Args:
            key: Identificador do segredo.

        Returns:
            String como ``********abcd`` ou None se não existir.
        """
        secret = SecretService._buscar_ativo(key)
        if not secret:
            return None
        return SecretService._mask(secret.last4)

    @staticmethod
    def set(
        key: str,
        value: str,
        *,
        ator_id: int | None = None,
        provider: str | None = None,
        label: str | None = None,
    ) -> Secret:
        """Cria ou atualiza um segredo criptografado.

        Args:
            key: Identificador estável do segredo.
            value: Valor bruto a criptografar. Não é salvo nem auditado em claro.
            ator_id: Funcionário responsável pela alteração.
            provider: Integração proprietária do segredo.
            label: Nome legível opcional.

        Returns:
            Instância de `Secret` persistida.

        Raises:
            ValueError: Se `key` ou `value` estiverem vazios.
            SecretConfigError: Se `SISTUR_SECRETS_KEY` estiver ausente/inválida.
        """
        BaseService._require(key, "key")
        BaseService._require(value, "value")
        key = key.strip()
        encrypted = SecretService._encrypt(value.strip())
        last4 = value.strip()[-4:] if value.strip() else None

        secret = db.session.query(Secret).filter_by(key=key).first()
        if secret is None:
            secret = Secret(
                key=key,
                provider=provider,
                label=label,
                value_encrypted=encrypted,
                last4=last4,
                ativo=True,
                updated_by_id=ator_id,
            )
            db.session.add(secret)
            db.session.flush()
            AuditService.log_create(
                "secrets",
                entity_id=secret.id,
                new_state=SecretService._audit_snapshot(secret),
                actor_id=ator_id,
            )
            return secret

        previous = SecretService._audit_snapshot(secret)
        secret.value_encrypted = encrypted
        secret.last4 = last4
        secret.ativo = True
        secret.updated_by_id = ator_id
        if provider is not None:
            secret.provider = provider
        if label is not None:
            secret.label = label
        db.session.flush()
        AuditService.log_update(
            "secrets",
            entity_id=secret.id,
            previous_state=previous,
            new_state=SecretService._audit_snapshot(secret),
            actor_id=ator_id,
        )
        return secret

    @staticmethod
    def delete(key: str, *, ator_id: int | None = None) -> None:
        """Desativa um segredo de forma idempotente.

        Args:
            key: Identificador do segredo.
            ator_id: Funcionário responsável pela remoção lógica.
        """
        secret = db.session.query(Secret).filter_by(key=key).first()
        if not secret or not secret.ativo:
            return

        previous = SecretService._audit_snapshot(secret)
        secret.ativo = False
        secret.updated_by_id = ator_id
        db.session.flush()
        AuditService.log_update(
            "secrets",
            entity_id=secret.id,
            previous_state=previous,
            new_state=SecretService._audit_snapshot(secret),
            actor_id=ator_id,
        )

    @staticmethod
    def _buscar_ativo(key: str) -> Secret | None:
        """Busca um segredo ativo por chave."""
        return (
            db.session.query(Secret)
            .filter_by(key=key, ativo=True)
            .first()
        )

    @staticmethod
    def _fernet() -> Fernet:
        """Instancia Fernet com a chave mestra do ambiente."""
        raw_key = os.environ.get("SISTUR_SECRETS_KEY", "").strip()
        if not raw_key:
            raise SecretConfigError("SISTUR_SECRETS_KEY não configurada.")
        try:
            return Fernet(raw_key.encode("utf-8"))
        except Exception as exc:
            raise SecretConfigError("SISTUR_SECRETS_KEY inválida.") from exc

    @staticmethod
    def _encrypt(value: str) -> str:
        """Criptografa um valor bruto para persistência."""
        return SecretService._fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    @staticmethod
    def _decrypt(value_encrypted: str) -> str:
        """Descriptografa um valor persistido."""
        try:
            return SecretService._fernet().decrypt(
                value_encrypted.encode("utf-8")
            ).decode("utf-8")
        except InvalidToken as exc:
            raise SecretConfigError("Segredo não pôde ser descriptografado.") from exc

    @staticmethod
    def _mask(last4: str | None) -> str:
        """Gera máscara segura a partir dos últimos caracteres."""
        return f"********{last4 or ''}"

    @staticmethod
    def _audit_snapshot(secret: Secret) -> dict[str, Any]:
        """Snapshot seguro para auditoria, sem valor bruto/ciphertext."""
        return BaseService._snapshot(secret, _SECRET_AUDIT_FIELDS)
