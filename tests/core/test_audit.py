# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Unit tests for AuditService.

Verifies that:
- log() persists AuditLog rows with correct fields
- log_login() / log_logout() capture user_id and action
- IP/user-agent are None outside a request context (graceful degradation)
- actor_id parameter correctly sets user_id without requiring a User object
"""

import pytest

from app.core.audit import AuditService
from app.core.models import AuditAction, AuditLog, UserType


class TestAuditServiceLog:

    def test_log_persiste_registro(self, app, db):
        with app.app_context():
            AuditService.log(AuditAction.create, "funcionarios", entity_id=1)
            count = db.session.query(AuditLog).count()

        assert count == 1

    def test_log_campos_obrigatorios(self, app, db):
        with app.app_context():
            AuditService.log(
                AuditAction.update,
                "funcionarios",
                entity_id=42,
                previous_state={"nome": "Antes"},
                new_state={"nome": "Depois"},
                actor_id=7,
            )
            log = db.session.query(AuditLog).first()

        assert log.action == AuditAction.update
        assert log.module == "funcionarios"
        assert log.entity_id == 42
        assert log.previous_state == {"nome": "Antes"}
        assert log.new_state == {"nome": "Depois"}
        assert log.user_id == 7
        assert log.user_type == UserType.employee

    def test_log_sem_ator_e_guest(self, app, db):
        with app.app_context():
            AuditService.log(AuditAction.login, "auth")
            log = db.session.query(AuditLog).first()

        assert log.user_id is None
        assert log.user_type == UserType.guest

    def test_log_actor_id_sobrescreve_guest(self, app, db):
        with app.app_context():
            AuditService.log(AuditAction.create, "test", actor_id=55)
            log = db.session.query(AuditLog).first()

        assert log.user_id == 55
        assert log.user_type == UserType.employee

    def test_log_ip_e_ua_sao_none_fora_de_request_context(self, app, db):
        """Outside a request context, IP and UA must be None — not raise."""
        with app.app_context():
            AuditService.log(AuditAction.view, "test")
            log = db.session.query(AuditLog).first()

        assert log.ip_address is None
        assert log.user_agent is None

    def test_log_ip_capturado_dentro_de_request_context(self, app, db, client):
        """Inside a real request, IP must be populated."""
        with app.app_context():
            with app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
                AuditService.log(AuditAction.view, "test")
            log = db.session.query(AuditLog).first()

        assert log.ip_address == "10.0.0.1"


class TestAuditServiceLogin:

    def test_log_login_success(self, app, db):
        with app.app_context():
            AuditService.log_login(user_id=1, success=True)
            log = db.session.query(AuditLog).first()

        assert log.action == AuditAction.login
        assert log.user_id == 1
        assert log.new_state == {"success": True}

    def test_log_login_falha(self, app, db):
        with app.app_context():
            AuditService.log_login(user_id=None, success=False)
            log = db.session.query(AuditLog).first()

        assert log.action == AuditAction.login
        assert log.new_state == {"success": False}

    def test_log_logout(self, app, db):
        with app.app_context():
            AuditService.log_logout(user_id=3)
            log = db.session.query(AuditLog).first()

        assert log.action == AuditAction.logout
        assert log.user_id == 3
