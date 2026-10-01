# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
GlobalEvent — eventos globais do sistema (feriados, alta temporada, manutenção).

Fonte única de verdade para todas as datas especiais do sistema.
Nenhum módulo deve hardcodar feriados ou datas especiais — todos devem
consultar o CalendarService.

Auditoria obrigatória: toda mutação é auditada via CalendarService.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from app.extensions import db


class TipoEvento(str, enum.Enum):
    """Tipo do evento global no calendário do sistema."""

    NATIONAL_HOLIDAY = "NATIONAL_HOLIDAY"
    LOCAL_HOLIDAY = "LOCAL_HOLIDAY"
    MAINTENANCE = "MAINTENANCE"
    HIGH_SEASON = "HIGH_SEASON"


class GlobalEvent(db.Model):
    """
    Evento global do calendário do sistema.

    Representa feriados nacionais, feriados locais, datas de manutenção
    e períodos de alta temporada. Serve como fonte centralizada de verdade
    para todos os módulos que precisam consultar datas especiais.

    Campos:
        titulo: Nome descritivo do evento (ex: 'Natal', 'Carnaval').
        data_evento: Data do evento (para recorrentes, o ano de referência).
            Quando `data_fim` está preenchida, é o primeiro dia do intervalo.
        data_fim: Último dia do intervalo (inclusive). NULL = evento de um dia só.
        tipo: Classificação do evento (TipoEvento) — rótulo/categoria.
        afeta_precificacao: Se True, módulos de precificação devem considerar este evento.
        afeta_folha: Se True, módulos de folha/ponto devem considerar este evento.
        recorrente_anual: Se True, repete todo ano (mês+dia); ex: Natal (25/12).
        origem: Procedência do cadastro — 'global' (Configurações) ou 'reservas'
            (calendário do módulo de Reservas). Define quem pode editar o evento:
            o módulo de Reservas só mantém o que ele mesmo criou e que ainda não
            foi promovido a evento de folha pelo super admin.

    Nota sobre o efeito em preço: o gatilho da tarifa especial é o flag
    `afeta_precificacao`, NÃO o `tipo`. Um evento de alta temporada com o flag
    ligado encarece a diária exatamente como um feriado.
    """

    __tablename__ = "sistur_global_events"

    ORIGEM_GLOBAL = "global"
    ORIGEM_RESERVAS = "reservas"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    titulo = db.Column(db.String(150), nullable=False)

    data_evento = db.Column(db.Date, nullable=False, index=True)

    data_fim = db.Column(db.Date, nullable=True)

    tipo = db.Column(
        db.Enum(TipoEvento, native_enum=False),
        nullable=False,
        default=TipoEvento.NATIONAL_HOLIDAY,
    )

    afeta_precificacao = db.Column(db.Boolean, default=False, nullable=False)

    afeta_folha = db.Column(db.Boolean, default=False, nullable=False)

    recorrente_anual = db.Column(db.Boolean, default=False, nullable=False)

    origem = db.Column(
        db.String(20),
        nullable=False,
        default=ORIGEM_GLOBAL,
        server_default=ORIGEM_GLOBAL,
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

    def __repr__(self) -> str:
        return (
            f"<GlobalEvent id={self.id} titulo={self.titulo!r}"
            f" data={self.data_evento} tipo={self.tipo.value}>"
        )
