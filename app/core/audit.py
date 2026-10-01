# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
AuditService — mandatory logging for every mutation in the Employee Portal.

Auditoria obrigatória:
    Every mutation must record previous_state and new_state as JSON snapshots,
    along with the actor, timestamp, and action context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flask import request as flask_request

from app.extensions import db
from app.core.models import Area, AuditAction, AuditLog, UserType

if TYPE_CHECKING:
    from app.core.models import User


class AuditService:

    # ------------------------------------------------------------------
    # Core log method
    # ------------------------------------------------------------------

    @staticmethod
    def log(
        action: AuditAction | str,
        module: str,
        *,
        entity_id: int | None = None,
        previous_state: dict[str, Any] | None = None,
        new_state: dict[str, Any] | None = None,
        actor: "User | None" = None,
        actor_id: int | None = None,
        area_slug: str | None = None,
    ) -> AuditLog:
        """
        Persist one audit record.

        Args:
            action:         AuditAction enum value or its string equivalent.
            module:         System module name (e.g. 'employees', 'timebank').
            entity_id:      PK of the affected row (None for non-entity actions).
            previous_state: Dict snapshot of the record *before* the change.
            new_state:      Dict snapshot of the record *after* the change.
            actor:          User who performed the action (None = guest).
            area_slug:      Slug of the Area this mutation belongs to.
                            Required for supervisors to filter logs by area.
        """
        if isinstance(action, str):
            action = AuditAction(action)

        area_id: int | None = None
        if area_slug:
            area = db.session.query(Area).filter_by(slug=area_slug).first()
            if area:
                area_id = area.id

        ip = AuditService._get_ip()
        ua = AuditService._get_user_agent()

        user_type = UserType.guest
        user_id = None
        if actor is not None:
            user_id = actor.id
            user_type = UserType.employee
        elif actor_id is not None:
            user_id = actor_id
            user_type = UserType.employee

        log = AuditLog(
            user_id=user_id,
            user_type=user_type,
            action=action,
            module=module,
            entity_id=entity_id,
            previous_state=previous_state,
            new_state=new_state,
            area_id=area_id,
            ip_address=ip,
            user_agent=ua,
        )
        db.session.add(log)
        db.session.commit()
        return log

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @staticmethod
    def log_create(
        module: str,
        entity_id: int,
        new_state: dict[str, Any],
        *,
        actor: "User | None" = None,
        actor_id: int | None = None,
        area_slug: str | None = None,
    ) -> AuditLog:
        return AuditService.log(
            AuditAction.create,
            module,
            entity_id=entity_id,
            new_state=new_state,
            actor=actor,
            actor_id=actor_id,
            area_slug=area_slug,
        )

    @staticmethod
    def log_update(
        module: str,
        entity_id: int,
        previous_state: dict[str, Any],
        new_state: dict[str, Any],
        *,
        actor: "User | None" = None,
        actor_id: int | None = None,
        area_slug: str | None = None,
    ) -> AuditLog:
        return AuditService.log(
            AuditAction.update,
            module,
            entity_id=entity_id,
            previous_state=previous_state,
            new_state=new_state,
            actor=actor,
            actor_id=actor_id,
            area_slug=area_slug,
        )

    @staticmethod
    def log_delete(
        module: str,
        entity_id: int,
        previous_state: dict[str, Any],
        *,
        actor: "User | None" = None,
        actor_id: int | None = None,
        area_slug: str | None = None,
    ) -> AuditLog:
        return AuditService.log(
            AuditAction.delete,
            module,
            entity_id=entity_id,
            previous_state=previous_state,
            actor=actor,
            actor_id=actor_id,
            area_slug=area_slug,
        )

    @staticmethod
    def log_login(user_id: int, *, success: bool = True) -> AuditLog:
        action = AuditAction.login
        log = AuditLog(
            user_id=user_id,
            user_type=UserType.employee,
            action=action,
            module="auth",
            new_state={"success": success},
            ip_address=AuditService._get_ip(),
            user_agent=AuditService._get_user_agent(),
        )
        db.session.add(log)
        db.session.commit()
        return log

    @staticmethod
    def log_logout(user_id: int) -> AuditLog:
        log = AuditLog(
            user_id=user_id,
            user_type=UserType.employee,
            action=AuditAction.logout,
            module="auth",
            ip_address=AuditService._get_ip(),
            user_agent=AuditService._get_user_agent(),
        )
        db.session.add(log)
        db.session.commit()
        return log

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @staticmethod
    def get_logs(
        *,
        module: str | None = None,
        area_slug: str | None = None,
        user_id: int | None = None,
        start_date=None,
        end_date=None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        """
        Fetch audit log rows with optional filters.

        area_slug is used by supervisors to see only their area's logs.
        """
        query = db.session.query(AuditLog)

        if module:
            query = query.filter(AuditLog.module == module)
        if user_id:
            query = query.filter(AuditLog.user_id == user_id)
        if area_slug:
            query = query.join(Area).filter(Area.slug == area_slug)
        if start_date:
            query = query.filter(AuditLog.created_at >= start_date)
        if end_date:
            query = query.filter(AuditLog.created_at <= end_date)

        return (
            query.order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_ip() -> str | None:
        """Retorna IP do cliente ou None quando não há request context/header.

        Retorna None (em vez de "") quando o header está ausente para manter
        o DB limpo: registros sem IP real ficam com NULL, não string vazia.
        """
        try:
            xff = flask_request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            return xff or flask_request.remote_addr or None
        except RuntimeError:
            return None

    @staticmethod
    def _get_user_agent() -> str | None:
        """Retorna User-Agent do cliente ou None quando ausente.

        Retorna None (em vez de "") quando o header está ausente — seja por
        falta de request context (produção) ou por request sem UA (pytest-flask).
        """
        try:
            ua = flask_request.headers.get("User-Agent") or ""
            return ua[:255] or None
        except RuntimeError:
            return None
