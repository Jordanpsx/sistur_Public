# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Models de Escala mensal de turnos e folgas."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from app.extensions import db


class EscalaEntrada(db.Model):
    """Registro de escala de um funcionário em uma data específica.

    Uma linha por (funcionario_id, data). Ausência de linha significa dia
    não definido (não é folga implícita — apenas sem escala registrada).

    Modelo de dados:
        - `tipo` (obrigatório): estado base do dia — NORMAL (trabalha), FOLGA
          (descanso) ou FALTA_JUSTIFICADA (ausência com justificativa por
          escrito, não descontada do banco de horas).
        - `labels` (opcional, multivalor): rótulos extras de turno aplicáveis a
          um dia NORMAL — MANHA, TARDE, NOITE, PLANTAO. Um mesmo dia pode ter
          mais de um (ex.: ["MANHA", "TARDE"] para quem dobra). FOLGA e
          FALTA_JUSTIFICADA não usam labels.
        - `justificativa` (obrigatória quando tipo == FALTA_JUSTIFICADA, livre
          nos demais tipos): texto explicando o motivo da ausência. Pode
          conter URLs (http://, https://, www.) — a UI do calendário
          reconhece e exibe como link clicável; nunca como HTML confiável
          (o texto é sempre tratado como não-confiável e escapado antes de
          qualquer link ser extraído).

    FALTA_JUSTIFICADA soma-se a FOLGA em `PontoService._get_expected_minutes()`
    (minutos esperados = 0 nos dois casos) — é assim que a ausência não
    afeta o banco de horas: sem expectativa de trabalho, sem batida, saldo
    do dia fica zero. A diferença para FOLGA é semântica (dia de descanso
    planejado vs. ausência não-planejada porém justificada) e de auditoria
    (exige texto explicativo).

    UniqueConstraint garante idempotência: upsert via delete+insert ou update.
    """

    __tablename__ = "sistur_escala_entradas"

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data = db.Column(db.Date, nullable=False, index=True)
    # Estado base do dia: trabalha (NORMAL), descansa (FOLGA) ou ausência
    # justificada que não descontada do banco de horas (FALTA_JUSTIFICADA).
    tipo = db.Column(
        db.Enum("NORMAL", "FOLGA", "FALTA_JUSTIFICADA", native_enum=False),
        nullable=False,
        default="NORMAL",
        server_default="NORMAL",
    )
    # Rótulos opcionais de turno (lista). Vazio/None = sem rótulo. Só para NORMAL.
    labels = db.Column(db.JSON, nullable=True)
    equipe_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_equipes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Texto livre — obrigatório quando tipo == FALTA_JUSTIFICADA (validado no
    # service, não no banco). Text (não String) porque a justificativa pode
    # incluir URLs longas e prosa maior que 255 chars.
    justificativa = db.Column(db.Text, nullable=True)
    criado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    atualizado_em = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint("funcionario_id", "data", name="uq_escala_func_data"),
    )

    funcionario = db.relationship(
        "Funcionario",
        foreign_keys=[funcionario_id],
        backref=db.backref("escala_entradas", lazy="dynamic"),
    )
    equipe = db.relationship("Equipe", foreign_keys=[equipe_id])

    # Estado base do dia
    TIPOS = ("NORMAL", "FOLGA", "FALTA_JUSTIFICADA")
    TIPO_LABELS = {"NORMAL": "Normal", "FOLGA": "Folga", "FALTA_JUSTIFICADA": "Falta Justificada"}
    TIPO_CORES = {
        "NORMAL": "#d1fae5",             # verde claro — dia de trabalho
        "FOLGA": "#f1f5f9",              # cinza claro — descanso
        "FALTA_JUSTIFICADA": "#fef3c7",  # âmbar claro — ausência com justificativa
    }
    TIPO_TEXTO = {
        "NORMAL": "#065f46",
        "FOLGA": "#64748b",
        "FALTA_JUSTIFICADA": "#92400e",
    }
    # Texto curto para caber na célula estreita do calendário (40px) — o rótulo
    # completo (TIPO_LABELS) continua sendo usado na legenda, pincel, modal e
    # formulário de lote, onde há espaço.
    TIPO_ABBR = {"NORMAL": "Normal", "FOLGA": "Folga", "FALTA_JUSTIFICADA": "Falta"}

    # Rótulos opcionais de turno (multivalor) aplicáveis a um dia NORMAL
    LABELS = ("MANHA", "TARDE", "NOITE", "PLANTAO")
    LABEL_LABELS = {
        "MANHA": "Manhã",
        "TARDE": "Tarde",
        "NOITE": "Noite",
        "PLANTAO": "Plantão",
    }
    LABEL_ABBR = {"MANHA": "M", "TARDE": "T", "NOITE": "N", "PLANTAO": "P"}
    LABEL_CORES = {
        "MANHA": "#fef9c3",      # amarelo claro
        "TARDE": "#fed7aa",      # laranja claro
        "NOITE": "#e0e7ff",      # índigo claro
        "PLANTAO": "#fce7f3",    # rosa claro
    }
    LABEL_TEXTO = {
        "MANHA": "#713f12",
        "TARDE": "#9a3412",
        "NOITE": "#3730a3",
        "PLANTAO": "#9d174d",
    }
