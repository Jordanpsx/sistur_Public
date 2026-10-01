# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Modelos ORM do módulo Ponto Eletrônico.

Tabelas criadas:
    sistur_time_entries — registros individuais de batida de ponto
    sistur_time_days    — agregado diário por colaborador (calculado via PontoService)

Referências legacy:
    legacy/includes/class-sistur-time-tracking.php
    legacy/includes/class-sistur-timebank-manager.php
    legacy/sql/create-timebank-tables.sql

Arquitetura em camadas: modelos só contêm definição ORM e propriedades simples.
Toda lógica de negócio reside em PontoService.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from app.extensions import db


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PunchType(str, enum.Enum):
    """Tipo da batida de ponto."""
    clock_in    = "clock_in"
    lunch_start = "lunch_start"
    lunch_end   = "lunch_end"
    clock_out   = "clock_out"
    extra       = "extra"


class PunchSource(str, enum.Enum):
    """Origem do registro de ponto.

    `IMPORT` identifica batidas vindas do relógio de ponto externo, carregadas em
    lote por `externo/importador_ponto.py`. Manter essa origem separada de `admin`
    é o que permite responder, numa auditoria trabalhista, se a batida veio do
    aparelho da portaria ou foi digitada por alguém.
    """
    employee = "employee"   # colaborador pelo portal
    admin    = "admin"      # edição administrativa
    KIOSK    = "KIOSK"      # totem físico
    QR       = "QR"         # scanner de QR code (ponto.py legado)
    IMPORT   = "IMPORT"     # importação em lote do relógio externo


class ProcessingStatus(str, enum.Enum):
    """Status de processamento da batida."""
    PENDENTE    = "PENDENTE"
    PROCESSADO  = "PROCESSADO"


class DeductionType(str, enum.Enum):
    """Tipo do abatimento do banco de horas."""
    folga = "folga"
    pagamento = "pagamento"


# ---------------------------------------------------------------------------
# TimeEntry — batida individual
# ---------------------------------------------------------------------------

class TimeEntry(db.Model):
    """
    Registro individual de batida de ponto.

    Cada linha representa um evento de entrada ou saída.
    O saldo diário é calculado a partir dos pares de batidas em TimeDay.

    Regras de negócio (ver PontoService):
        Rule #3: edições administrativas exigem admin_change_reason não vazio.
        Rule #5: número ímpar de batidas no dia → TimeDay.needs_review = True.
    """
    __tablename__ = "sistur_time_entries"
    __table_args__ = (
        db.UniqueConstraint(
            "funcionario_id", "punch_time", "punch_type", name="unique_punch_idx"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Colaborador vinculado à batida
    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Timestamp exato da batida (UTC)
    punch_time = db.Column(db.DateTime, nullable=False, index=True)

    # Data do turno — pode diferir de punch_time.date() em viragens de meia-noite
    shift_date = db.Column(db.Date, nullable=False, index=True)

    punch_type = db.Column(
        db.Enum(PunchType, native_enum=False),
        nullable=False,
        default=PunchType.clock_in,
    )

    source = db.Column(
        db.Enum(PunchSource, native_enum=False),
        nullable=False,
        default=PunchSource.employee,
    )

    processing_status = db.Column(
        db.Enum(ProcessingStatus, native_enum=False),
        nullable=False,
        default=ProcessingStatus.PENDENTE,
    )

    # Preenchido somente em edições administrativas (Rule #3)
    admin_change_reason = db.Column(db.Text, nullable=True)

    # FK do admin que realizou a edição (nullable em registros automáticos)
    changed_by_user_id = db.Column(db.Integer, nullable=True)

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    funcionario = db.relationship(
        "Funcionario",
        foreign_keys=[funcionario_id],
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<TimeEntry id={self.id} func={self.funcionario_id}"
            f" type={self.punch_type} time={self.punch_time}>"
        )


# ---------------------------------------------------------------------------
# TimeDay — agregado diário
# ---------------------------------------------------------------------------

class TimeDay(db.Model):
    """
    Agregado diário de ponto para um colaborador.

    É criado ou recalculado por PontoService.processar_dia() sempre que
    uma nova TimeEntry é registrada ou editada para aquele dia.

    Campos de saldo:
        minutos_trabalhados    — soma bruta dos pares de batidas
        saldo_calculado_minutos — após aplicação da tolerância de 10 min (Rule #4)
        saldo_final_minutos    — após deduções manuais de banco de horas

    Regras de negócio:
        needs_review = True quando o número de batidas do dia é ímpar (Rule #5).
        expected_minutes_snapshot é imutável após a criação — representa a
        jornada contratada naquele dia, usada para auditoria futura.
    """
    __tablename__ = "sistur_time_days"
    __table_args__ = (
        db.UniqueConstraint(
            "funcionario_id", "shift_date", name="uq_funcionario_shift_date"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    shift_date = db.Column(db.Date, nullable=False, index=True)

    # Minutos brutos (soma dos pares sem tolerância)
    minutos_trabalhados = db.Column(db.Integer, nullable=False, default=0)

    # Saldo após Rule #4 (tolerância de 10 min)
    saldo_calculado_minutos = db.Column(db.Integer, nullable=False, default=0)

    # Saldo final após deduções manuais do banco de horas
    saldo_final_minutos = db.Column(db.Integer, nullable=False, default=0)

    # True quando número de batidas é ímpar (Rule #5)
    needs_review = db.Column(db.Boolean, nullable=False, default=False)

    # Snapshot da jornada esperada no dia (imutável após criação)
    expected_minutes_snapshot = db.Column(db.Integer, nullable=False, default=480)

    # Snapshot da tolerância (regra 4) aplicável no dia
    tolerance_snapshot = db.Column(db.Integer, nullable=False, default=10)

    # True quando o scheduler de monitoramento aplicou um débito provisório de ausência.
    # Revertido automaticamente por processar_dia() quando batidas reais são registradas.
    # Veja: AvisoService.verificar_atrasos(), PontoService.processar_dia().
    auto_debit_aplicado = db.Column(db.Boolean, nullable=False, default=False)

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

    # Relationships
    funcionario = db.relationship(
        "Funcionario",
        foreign_keys=[funcionario_id],
        lazy="select",
    )
    entries = db.relationship(
        "TimeEntry",
        primaryjoin="and_(TimeDay.funcionario_id == TimeEntry.funcionario_id,"
                    " TimeDay.shift_date == TimeEntry.shift_date)",
        foreign_keys="[TimeEntry.funcionario_id, TimeEntry.shift_date]",
        lazy="select",
        viewonly=True,
        order_by="TimeEntry.punch_time",
    )

    def __repr__(self) -> str:
        return (
            f"<TimeDay func={self.funcionario_id}"
            f" date={self.shift_date} saldo={self.saldo_calculado_minutos}>"
        )


# ---------------------------------------------------------------------------
# TimeBankDeduction — Registro de Abatimentos
# ---------------------------------------------------------------------------

class TimeBankDeduction(db.Model):
    """Lançamento no banco de horas global — folga compensatória ou pagamento em dinheiro.

    Nada nesta tabela é apagado ou editado. Um lançamento errado é desfeito por
    **contra-lançamento**: uma segunda linha, apontando para a primeira via
    `estorno_de_id`, com `minutos_abatidos` e `pagamento_valor` invertidos.

    A vantagem é que a invariante do saldo continua valendo sem nenhuma cláusula
    extra nas agregações:

        banco_horas_acumulado = Σ TimeDay.saldo_final_minutos
                                − Σ TimeBankDeduction.minutos_abatidos

    Com o estorno somando −N, a soma volta sozinha ao valor anterior. `PontoService.
    recalcular_banco_global` e o detector de divergência do `RHService` continuam
    corretos sem alteração — e não existe o risco clássico de um filtro de status
    ser lembrado num lugar e esquecido no outro.

    Sinais:
        minutos_abatidos > 0  → lançamento normal (consome saldo)
        minutos_abatidos < 0  → estorno (devolve saldo); `estorno_de_id` preenchido

    O colaborador vê estas linhas no próprio portal, com a `observacao` escrita
    pelo operador — por isso ela é obrigatória em lançamentos de pagamento e nunca
    deve conter nota interna.
    """
    __tablename__ = "sistur_timebank_deductions"

    id = db.Column(db.Integer, primary_key=True)

    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    deduction_type = db.Column(
        db.Enum(DeductionType, native_enum=False),
        nullable=False,
    )

    # Positivo consome saldo; negativo devolve (linha de estorno).
    minutos_abatidos = db.Column(db.Integer, nullable=False)

    data_registro = db.Column(db.Date, nullable=False, index=True)

    # Valor em R$ quando deduction_type == pagamento. Negativo na linha de estorno,
    # para que a soma de valores pagos também volte a zero ao desfazer.
    pagamento_valor = db.Column(db.Numeric(10, 2), nullable=True)

    # Texto exibido AO COLABORADOR no extrato do portal. Obrigatório em pagamento.
    observacao = db.Column(db.String(500), nullable=True)

    # Quem lançou. Fica na própria linha (e não só no AuditLog) porque o extrato
    # do colaborador mostra o responsável junto do lançamento.
    criado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Preenchido apenas na linha de estorno, apontando para o lançamento desfeito.
    estorno_de_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_timebank_deductions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    funcionario = db.relationship(
        "Funcionario",
        foreign_keys=[funcionario_id],
        lazy="select",
    )

    criado_por = db.relationship(
        "Funcionario",
        foreign_keys=[criado_por_id],
        lazy="select",
    )

    # Lançamento original (a partir da linha de estorno) e seu inverso.
    estorno_de = db.relationship(
        "TimeBankDeduction",
        remote_side=[id],
        foreign_keys=[estorno_de_id],
        backref=db.backref("estornos", lazy="select"),
        lazy="select",
    )

    @property
    def eh_estorno(self) -> bool:
        """True quando esta linha é o contra-lançamento de outra."""
        return self.estorno_de_id is not None

    @property
    def estornado(self) -> bool:
        """True quando já existe um contra-lançamento desfazendo esta linha."""
        return bool(self.estornos)

    def __repr__(self) -> str:
        return (
            f"<TimeBankDeduction id={self.id} func={self.funcionario_id} "
            f"type={self.deduction_type} min={self.minutos_abatidos}>"
        )
