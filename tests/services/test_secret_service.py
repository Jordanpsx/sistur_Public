# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes do cofre interno de segredos."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.core.models import AuditLog
from app.extensions import db
from app.models.secret import Secret
from app.services.secret_service import SecretConfigError, SecretService


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch):
    """Configura uma chave Fernet efêmera para cada teste."""
    monkeypatch.setenv("SISTUR_SECRETS_KEY", Fernet.generate_key().decode("utf-8"))


def test_set_get_e_masked_criptografam_valor(db):
    secret = SecretService.set(
        "openai.api_key",
        "sk-proj-segredo-1234",
        provider="openai",
        label="OpenAI API Key",
        ator_id=7,
    )

    row = db.session.get(Secret, secret.id)
    assert row is not None
    assert row.value_encrypted != "sk-proj-segredo-1234"
    assert "sk-proj" not in row.value_encrypted
    assert row.last4 == "1234"
    assert SecretService.get("openai.api_key") == "sk-proj-segredo-1234"
    assert SecretService.masked("openai.api_key") == "********1234"
    assert SecretService.exists("openai.api_key") is True


def test_set_atualiza_sem_vazar_valor_na_auditoria(db):
    SecretService.set("openai.api_key", "sk-primeira-1111", provider="openai", ator_id=1)
    SecretService.set("openai.api_key", "sk-segunda-2222", provider="openai", ator_id=2)

    row = db.session.query(Secret).filter_by(key="openai.api_key").one()
    assert SecretService.get("openai.api_key") == "sk-segunda-2222"
    assert row.last4 == "2222"

    logs = db.session.query(AuditLog).filter_by(module="secrets").order_by(AuditLog.id).all()
    assert len(logs) == 2
    assert logs[0].action.value == "create"
    assert logs[1].action.value == "update"
    serialized = str([log.previous_state for log in logs]) + str([log.new_state for log in logs])
    assert "sk-primeira" not in serialized
    assert "sk-segunda" not in serialized
    assert "value_encrypted" not in serialized
    assert "last4" in serialized


def test_delete_desativa_seguro_e_idempotente(db):
    SecretService.set("mercadopago.access_token", "APP_USR-token-9999", ator_id=1)

    SecretService.delete("mercadopago.access_token", ator_id=2)
    SecretService.delete("mercadopago.access_token", ator_id=2)

    row = db.session.query(Secret).filter_by(key="mercadopago.access_token").one()
    assert row.ativo is False
    assert SecretService.get("mercadopago.access_token", default=None) is None
    assert SecretService.masked("mercadopago.access_token") is None
    assert db.session.query(AuditLog).filter_by(module="secrets").count() == 2


def test_get_retorna_default_quando_nao_existe(db):
    assert SecretService.get("openai.api_key", default="env-fallback") == "env-fallback"
    assert SecretService.exists("openai.api_key") is False
    assert SecretService.masked("openai.api_key") is None


def test_set_sem_chave_mestra_levanta_erro(monkeypatch, db):
    monkeypatch.delenv("SISTUR_SECRETS_KEY", raising=False)

    with pytest.raises(SecretConfigError):
        SecretService.set("openai.api_key", "sk-1234")


def test_chave_mestra_invalida_levanta_erro(monkeypatch, db):
    monkeypatch.setenv("SISTUR_SECRETS_KEY", "invalida")

    with pytest.raises(SecretConfigError):
        SecretService.set("openai.api_key", "sk-1234")
