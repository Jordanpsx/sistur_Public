# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Funcionario — core business entity for the Employee Portal.

Maps to legacy wp_sistur_employees. All business-domain fields use
Portuguese names as required by CLAUDE.md.

CPF is the single login identifier (no password required in this phase).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.extensions import db


# ---------------------------------------------------------------------------
# CPF validation
# ---------------------------------------------------------------------------

def validar_cpf(cpf: str) -> str:
    """
    Validate a Brazilian CPF and return the cleaned 11-digit string.

    Ported verbatim from the PHP algorithm in:
        legacy/includes/login-funcionario-new.php :: sistur_validate_cpf()

    Args:
        cpf: CPF string in any format (e.g. '529.982.247-25' or '52998224725').

    Returns:
        Cleaned 11-digit CPF string if valid.

    Raises:
        ValueError: If the CPF is structurally or algorithmically invalid.
    """
    cleaned = re.sub(r"[^0-9]", "", cpf)

    if len(cleaned) != 11:
        raise ValueError("CPF deve conter 11 dígitos.")

    # Reject sequences of identical digits (e.g. 111.111.111-11)
    if len(set(cleaned)) == 1:
        raise ValueError("CPF inválido.")

    # Validate the two check digits (positions 9 and 10)
    for t in range(9, 11):
        d = 0
        for c in range(t):
            d += int(cleaned[c]) * ((t + 1) - c)
        d = ((10 * d) % 11) % 10
        if int(cleaned[t]) != d:
            raise ValueError("CPF inválido.")

    return cleaned


def formatar_cpf(cpf: str) -> str:
    """Format an 11-digit CPF string as '000.000.000-00'."""
    c = re.sub(r"[^0-9]", "", cpf)
    return f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:11]}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class Funcionario(db.Model):
    """
    Employee record — the primary entity for the Portal do Colaborador.

    Field naming convention (CLAUDE.md):
        Business fields → Portuguese (nome, cpf, cargo, …)
        Technical/infra fields → English (id, created_at pattern kept as criado_em/atualizado_em
        for consistency with the team's convention)
    """
    __tablename__ = "sistur_funcionarios"

    # ------------------------------------------------------------------
    # Identity & auth
    # ------------------------------------------------------------------
    id = db.Column(db.Integer, primary_key=True)

    nome = db.Column(db.String(255), nullable=False)

    # CPF is the login identifier — format stored as raw 11 digits (no punctuation)
    cpf = db.Column(db.String(11), unique=True, nullable=False, index=True)

    email = db.Column(db.String(255), nullable=True)
    telefone = db.Column(db.String(20), nullable=True)

    # Nullable: CPF-only login in this phase; hash stored when password is set later
    senha_hash = db.Column(db.String(255), nullable=True)

    # QR token used by the clock-in scanner (legacy: token_qr)
    token_qr = db.Column(db.String(36), unique=True, nullable=True)

    # ------------------------------------------------------------------
    # Employment details
    # ------------------------------------------------------------------
    cargo = db.Column(db.String(255), nullable=True)          # job title / position

    # CLT = funcionário com jornada fixa; DIARISTA = contratado por diária sem jornada recorrente
    tipo_contrato = db.Column(
        db.Enum("CLT", "DIARISTA", native_enum=False),
        nullable=False,
        default="CLT",
        server_default="CLT",
    )
    # Remuneração por diária — preenchido apenas para tipo_contrato=DIARISTA
    valor_diaria = db.Column(db.Numeric(10, 2), nullable=True)

    matricula = db.Column(db.String(50), nullable=True)        # employee registration number
    data_admissao = db.Column(db.Date, nullable=True)          # hire date
    ctps = db.Column(db.String(50), nullable=True)             # work card number
    ctps_uf = db.Column(db.String(2), nullable=True)           # work card state
    cbo = db.Column(db.String(20), nullable=True)              # job classification code
    foto = db.Column(db.String(500), nullable=True)            # profile photo URL
    bio = db.Column(db.Text, nullable=True)

    # ------------------------------------------------------------------
    # Work schedule — mirrors legacy time_expected_minutes / lunch_minutes
    # ------------------------------------------------------------------
    minutos_esperados_dia = db.Column(
        db.SmallInteger, default=480, nullable=False
    )  # fallback global (8 h × 60 = 480 min)
    minutos_almoco = db.Column(
        db.SmallInteger, default=60, nullable=False
    )  # fallback global lunch (1 h)

    # Jornada semanal por dia — sobrepõe os campos globais acima quando presente.
    # Formato: {"segunda": {"ativo": true, "minutos": 480, "almoco": 60}, ...}
    # Dias sem entrada assumem {"ativo": false}.
    jornada_semanal = db.Column(db.JSON, nullable=True)

    # Horário de início esperado para monitoramento automático de atrasos.
    # Quando None, o colaborador é excluído do scheduler de verificação de presença.
    # Exemplo: time(8, 0) = 08:00. Usado por AvisoService.verificar_atrasos().
    horario_entrada_padrao = db.Column(db.Time, nullable=True)

    # ------------------------------------------------------------------
    # Organisation
    # ------------------------------------------------------------------
    area_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_areas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Role que define as permissões de acesso ao portal (nullable = sem role atribuído)
    role_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_roles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # State & timestamps
    # ------------------------------------------------------------------
    ativo = db.Column(db.Boolean, default=True, nullable=False)

    # Saldo em cache do banco de horas global (minutos)
    banco_horas_acumulado = db.Column(db.Integer, nullable=False, default=0)

    # Preferência de tema da interface ('light' | 'dark'). NULL = seguir o padrão
    # do sistema (prefers-color-scheme). Por conta, isolada por usuário — sem
    # conflito em terminais compartilhados; segue o colaborador entre dispositivos.
    tema_preferido = db.Column(
        db.Enum("light", "dark", native_enum=False),
        nullable=True,
    )

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    area = db.relationship("Area", foreign_keys=[area_id], lazy="select")
    role = db.relationship("Role", foreign_keys=[role_id], back_populates="funcionarios", lazy="select")

    # ------------------------------------------------------------------
    # Business logic
    # ------------------------------------------------------------------

    def saldo_banco_horas(self) -> int:
        """
        Return the employee's current hour-bank balance in minutes.
        """
        return self.banco_horas_acumulado

    def saldo_banco_horas_formatado(self) -> str:
        """Return the hour-bank balance as a human-readable string (e.g. '2h 30min')."""
        total = self.saldo_banco_horas()
        sinal = "-" if total < 0 else ""
        total = abs(total)
        horas = total // 60
        minutos = total % 60
        return f"{sinal}{horas}h {minutos:02d}min"

    def cpf_formatado(self) -> str:
        """Return CPF in display format '000.000.000-00'."""
        return formatar_cpf(self.cpf)

    def __repr__(self) -> str:
        return f"<Funcionario id={self.id} cpf={self.cpf!r} nome={self.nome!r}>"
