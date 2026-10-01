# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Modelos ORM do módulo Avisos (Notificações) e Ausências Justificadas.

Tabelas criadas:
    sistur_avisos                   — notificações internas para colaboradores
    sistur_ausencias_justificadas   — folgas, atestados e férias que suprimem alertas automáticos

Arquitetura em camadas: modelos só contêm definição ORM e propriedades simples.
Toda lógica de negócio reside em AvisoService e AusenciaService.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from app.extensions import db


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TipoAviso(str, enum.Enum):
    """Classificação do aviso interno."""

    ATRASO        = "ATRASO"        # Colaborador não registrou ponto no horário previsto
    FALTA         = "FALTA"         # Ausência não justificada confirmada ao fim do dia
    SISTEMA       = "SISTEMA"       # Mensagem genérica do sistema (RH, admin, etc.)
    AJUSTE_PONTO  = "AJUSTE_PONTO" # Notificação sobre solicitação de ajuste de ponto


class TipoAusencia(str, enum.Enum):
    """Tipo de ausência justificada que suprime alertas automáticos."""

    FOLGA    = "FOLGA"
    ATESTADO = "ATESTADO"
    FERIAS   = "FERIAS"


# ---------------------------------------------------------------------------
# Aviso — notificação interna
# ---------------------------------------------------------------------------


class Aviso(db.Model):
    """
    Notificação interna do sistema enviada a um colaborador.

    Gerada automaticamente pelo scheduler de monitoramento de presença
    (AvisoService.verificar_atrasos / finalizar_ausencias) ou manualmente
    por administradores/RH.

    Regras de negócio:
        - remetente_id é None para avisos gerados pelo sistema.
        - is_lido=False indica que o destinatário ainda não visualizou.
        - Apenas o destinatário pode marcar como lido ou excluir (IDOR guard no blueprint).
    """

    __tablename__ = "sistur_avisos"

    id = db.Column(db.Integer, primary_key=True)

    # Destinatário do aviso
    destinatario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Remetente humano — None quando gerado automaticamente pelo sistema
    remetente_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )

    titulo = db.Column(db.String(200), nullable=False)
    mensagem = db.Column(db.Text, nullable=True)

    tipo = db.Column(
        db.Enum(TipoAviso, native_enum=False),
        nullable=False,
        default=TipoAviso.SISTEMA,
    )

    is_lido = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # Referência opcional ao AjustePontoRequest relacionado (para avisos do tipo AJUSTE_PONTO)
    ajuste_ponto_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_ajuste_ponto_requests.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Vínculo polimórfico com a solicitação de origem (2.11 — aprovações
    # compartilhadas): permite encerrar os avisos-irmãos de todos os
    # aprovadores quando um deles resolve. Serve ajuste_ponto hoje e
    # troca de turno / tarefas (2.10) no futuro, sem nova coluna por tipo.
    referencia_tipo = db.Column(db.String(30), nullable=True, index=True)
    referencia_id = db.Column(db.Integer, nullable=True, index=True)

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Relationships
    destinatario = db.relationship(
        "Funcionario",
        foreign_keys=[destinatario_id],
        lazy="select",
    )
    remetente = db.relationship(
        "Funcionario",
        foreign_keys=[remetente_id],
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Aviso id={self.id} tipo={self.tipo.value}"
            f" dest={self.destinatario_id} lido={self.is_lido}>"
        )


# ---------------------------------------------------------------------------
# AusenciaJustificada — folga / atestado / férias por colaborador
# ---------------------------------------------------------------------------


class AusenciaJustificada(db.Model):
    """
    Registro de ausência justificada de um colaborador para uma data específica.

    Quando aprovado=True, o scheduler de monitoramento de presença ignora
    esse colaborador na data informada e NÃO cria avisos de atraso ou falta.

    Se um débito provisório (TimeDay.auto_debit_aplicado=True) já foi aplicado
    antes da aprovação, AusenciaService.aprovar() reverte o débito automaticamente.

    Regra de unicidade: um colaborador não pode ter duas ausências para a mesma data
    (UniqueConstraint funcionario_id + data).
    """

    __tablename__ = "sistur_ausencias_justificadas"
    __table_args__ = (
        db.UniqueConstraint(
            "funcionario_id", "data", name="uq_ausencia_funcionario_data"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    data = db.Column(db.Date, nullable=False, index=True)

    tipo = db.Column(db.Enum(TipoAusencia, native_enum=False), nullable=False)

    # False = registrado pelo RH mas ainda não aprovado pela gestão
    aprovado = db.Column(db.Boolean, nullable=False, default=False)

    # Admin/RH que criou o registro
    criado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
    )

    observacao = db.Column(db.Text, nullable=True)

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

    def __repr__(self) -> str:
        return (
            f"<AusenciaJustificada id={self.id} func={self.funcionario_id}"
            f" data={self.data} tipo={self.tipo.value} aprovado={self.aprovado}>"
        )
