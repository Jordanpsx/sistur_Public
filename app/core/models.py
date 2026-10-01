# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

import enum
from datetime import datetime, timezone

from app.extensions import db


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserType(str, enum.Enum):
    employee = "employee"
    admin = "admin"
    guest = "guest"


class AuditAction(str, enum.Enum):
    create = "create"
    update = "update"
    delete = "delete"
    view = "view"
    export = "export"
    login = "login"
    logout = "logout"
    permission_change = "permission_change"


# ---------------------------------------------------------------------------
# Area  (maps to legacy wp_sistur_departments)
# ---------------------------------------------------------------------------

class Area(db.Model):
    """
    Organizational area / department.

    Slugs are intentionally kept compatible with legacy department names
    to allow direct data migration from wp_sistur_departments.
    """
    __tablename__ = "sistur_areas"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    # Índice da paleta categórica (0-7, classes .area-cN do design system).
    # NULL = cor derivada do id (id % 8), comportamento legado.
    color_index = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    @property
    def cor_indice(self) -> int:
        """Índice de cor efetivo: o escolhido pelo operador ou o derivado do id."""
        return self.color_index if self.color_index is not None else self.id % 8

    # Relationships
    user_permissions = db.relationship(
        "UserAreaPermission", back_populates="area", lazy="dynamic"
    )
    users = db.relationship("User", back_populates="department", lazy="dynamic")
    audit_logs = db.relationship("AuditLog", back_populates="area", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<Area slug={self.slug!r}>"


# ---------------------------------------------------------------------------
# User  (maps to legacy wp_sistur_employees)
# ---------------------------------------------------------------------------

class User(db.Model):
    """
    System user / employee.  Authentication is CPF-based (no username).
    """
    __tablename__ = "sistur_users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    cpf = db.Column(db.String(14), unique=True, nullable=False)  # format: 000.000.000-00
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey("sistur_areas.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    department = db.relationship("Area", back_populates="users", foreign_keys=[department_id])
    area_permissions = db.relationship(
        "UserAreaPermission", back_populates="user", lazy="dynamic", cascade="all, delete-orphan"
    )

    def is_supervisor_of(self, area_slug: str) -> bool:
        """Returns True if this user is a supervisor in the given area."""
        return (
            db.session.query(UserAreaPermission)
            .join(Area)
            .filter(
                UserAreaPermission.user_id == self.id,
                UserAreaPermission.is_supervisor == True,
                Area.slug == area_slug,
                Area.is_active == True,
            )
            .first()
            is not None
        )

    def __repr__(self) -> str:
        return f"<User id={self.id} cpf={self.cpf!r}>"


# ---------------------------------------------------------------------------
# UserAreaPermission
# ---------------------------------------------------------------------------

class UserAreaPermission(db.Model):
    """
    Links a User to an Area with an optional supervisor flag.

    Supervision is area-scoped:
        A supervisor in 'rh' can view audit logs filtered to 'rh'.
        They cannot see 'restaurante' logs unless they also have a
        UserAreaPermission row for 'restaurante' with is_supervisor=True.
    """
    __tablename__ = "sistur_user_area_permissions"
    __table_args__ = (
        db.UniqueConstraint("user_id", "area_id", name="uq_user_area"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("sistur_users.id", ondelete="CASCADE"), nullable=False
    )
    area_id = db.Column(
        db.Integer, db.ForeignKey("sistur_areas.id", ondelete="CASCADE"), nullable=False
    )
    is_supervisor = db.Column(db.Boolean, default=False, nullable=False)
    granted_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # Relationships
    user = db.relationship("User", back_populates="area_permissions")
    area = db.relationship("Area", back_populates="user_permissions")

    def __repr__(self) -> str:
        return (
            f"<UserAreaPermission user={self.user_id} area={self.area_id}"
            f" supervisor={self.is_supervisor}>"
        )


# ---------------------------------------------------------------------------
# AuditLog  (maps to legacy wp_sistur_audit_logs)
# ---------------------------------------------------------------------------

class AuditLog(db.Model):
    """
    Immutable record of every mutation in the Employee Portal.

    Mandatory audit logging: every mutation must record
    previous_state and new_state as JSON snapshots.
    """
    __tablename__ = "sistur_audit_logs"

    # BigInteger no MySQL/MariaDB; Integer no SQLite (autoincrement exige INTEGER no SQLite)
    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    # No FK — user_id is polymorphic: holds a sistur_users.id OR sistur_funcionarios.id
    # Disambiguated by user_type field below.
    user_id = db.Column(db.Integer, nullable=True, index=True)
    user_type = db.Column(
        db.Enum(UserType, native_enum=False), default=UserType.employee, nullable=False
    )
    action = db.Column(db.Enum(AuditAction, native_enum=False), nullable=False)
    module = db.Column(db.String(50), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=True, index=True)

    # Before / after snapshots (Auditoria obrigatória)
    previous_state = db.Column(db.JSON, nullable=True)
    new_state = db.Column(db.JSON, nullable=True)

    # Area context — allows supervisors to filter logs by their area
    area_id = db.Column(
        db.Integer, db.ForeignKey("sistur_areas.id", ondelete="SET NULL"), nullable=True
    )

    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Relationships
    area = db.relationship("Area", back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog action={self.action} module={self.module!r} entity={self.entity_id}>"
