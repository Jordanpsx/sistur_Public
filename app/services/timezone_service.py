# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Utilitários de fuso horário do sistema.

O banco armazena TimeEntry.punch_time em UTC. Valores vindos de inputs
`datetime-local` não carregam offset, então devem ser interpretados no fuso
configurado em `system.timezone` antes de persistir.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.configuracao_service import ConfiguracaoService


class TimezoneService:
    """Conversões centralizadas entre UTC e o fuso operacional do SISTUR."""

    DEFAULT_TIMEZONE = "America/Sao_Paulo"

    @staticmethod
    def system_timezone() -> ZoneInfo:
        """Retorna o fuso configurado em `system.timezone`."""
        tz_name = (
            ConfiguracaoService.get("system.timezone", TimezoneService.DEFAULT_TIMEZONE)
            or TimezoneService.DEFAULT_TIMEZONE
        )
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo(TimezoneService.DEFAULT_TIMEZONE)

    @staticmethod
    def ensure_utc(value: datetime) -> datetime:
        """Normaliza um datetime para UTC, preservando compatibilidade com valores naive."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def parse_datetime_local(value: str) -> datetime:
        """
        Interpreta um valor de `<input type="datetime-local">` no fuso do sistema.

        Retorna datetime aware em UTC, pronto para gravação em TimeEntry.punch_time.
        """
        raw = (value or "").strip()
        if not raw:
            raise ValueError("O horário é obrigatório.")

        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("Horário inválido.") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TimezoneService.system_timezone())

        return parsed.astimezone(timezone.utc)

    @staticmethod
    def local_date(value: datetime):
        """Retorna a data operacional no fuso do sistema para um datetime UTC."""
        return TimezoneService.ensure_utc(value).astimezone(
            TimezoneService.system_timezone()
        ).date()

    @staticmethod
    def datetime_local_input(value: datetime) -> str:
        """Formata UTC para o valor esperado por `<input type="datetime-local">`."""
        if not value:
            return ""
        return TimezoneService.ensure_utc(value).astimezone(
            TimezoneService.system_timezone()
        ).strftime("%Y-%m-%dT%H:%M")
