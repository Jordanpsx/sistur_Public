# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
PontoImportService — importação em lote de batidas do relógio de ponto externo.

Arquitetura em camadas: sem imports do Flask — pure Python, Cython-compatível.

O relatório .xls do aparelho é lido fora do SISTUR, por `externo/importador_ponto.py`,
que resolve o vínculo entre o usuário do relógio e o funcionário e envia a lista já
pronta. Este serviço recebe essa lista, aplica as regras de consistência e grava.

Regras de negócio
-----------------
1. **Deduplicação por (funcionario_id, punch_time).**
   Batida que já existe é pulada em silêncio — reimportar o mesmo período é seguro
   e é o caminho normal depois de corrigir um vínculo.

   A `UniqueConstraint(funcionario_id, punch_time, punch_type)` NÃO serve para isso:
   `punch_type` é inferido pela contagem de batidas do dia, então numa reimportação
   as repetidas entrariam como `extra`, não colidiriam, e a jornada dobraria sem
   nenhum erro na tela.

2. **Dia que passaria de 4 batidas é pulado inteiro.**
   A jornada CLT tem 4 batidas (entrada, saída-almoço, retorno, saída). Um dia que
   terminaria com mais que isso é dado inconsistente — leitura dupla no aparelho,
   vínculo trocado, arquivo do período errado. Nenhuma batida daquele dia é gravada
   e um Aviso é emitido para quem cuida da Folha de Ponto.

   A regra só é avaliada quando há batidas novas a gravar: um dia que já estava com
   5 batidas antes da importação não gera aviso, porque não é obra dela.

3. **Funcionário inválido rejeita o lote inteiro.**
   O `funcionario_id` vem digitado à mão num CSV, fora do sistema. Um ID inexistente
   ou inativo significa vínculo errado — e gravar o resto enquanto se descarta um
   funcionário em silêncio é pior do que parar. Como a deduplicação torna a
   reimportação segura, corrigir e rodar de novo não custa nada.

4. **Um único AuditLog por importação.**
   No padrão de `PontoService.reprocess_interval()`, não um log por batida.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.audit import AuditAction, AuditService
from app.extensions import db
from app.services.base import BaseService

# Jornada CLT: entrada, saída-almoço, retorno-almoço, saída.
MAX_BATIDAS_DIA = 4

# Quantos dias inconsistentes são listados no corpo do aviso antes de resumir.
_LIMITE_DETALHE_AVISO = 20


class PontoImportService(BaseService):
    """Grava lotes de batidas vindas do relógio de ponto externo."""

    # -----------------------------------------------------------------------
    # Normalização da entrada
    # -----------------------------------------------------------------------

    @staticmethod
    def _para_utc(valor: str, fuso: ZoneInfo) -> datetime:
        """
        Converte o horário local do relógio para UTC.

        O aparelho grava hora de parede sem fuso; o `timezone` declarado no lote diz
        qual é. A conversão acontece aqui, no servidor, porque é ele quem manda no
        relógio do sistema — a máquina do aparelho não precisa carregar base de fusos.

        Args:
            valor: Horário ISO 8601, tipicamente sem offset ("2026-08-01T08:00:00").
            fuso:  Fuso do relógio.

        Returns:
            Datetime aware em UTC.

        Raises:
            ValueError: Se o valor não for ISO 8601 válido.
        """
        try:
            parsed = datetime.fromisoformat(str(valor).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Horário inválido: {valor!r}. Use ISO 8601.") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=fuso)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _normalizar_batidas(
        batidas: list[dict], timezone_relogio: str
    ) -> dict[int, list[datetime]]:
        """
        Valida o payload e agrupa as batidas por funcionário, já em UTC.

        Args:
            batidas:          Lista de {funcionario_id, punch_time}.
            timezone_relogio: Nome do fuso do aparelho (ex: "America/Sao_Paulo").

        Returns:
            Dicionário {funcionario_id: [datetimes UTC ordenados]}, sem repetições
            dentro do próprio lote.

        Raises:
            ValueError: Se a lista estiver vazia, se algum item não tiver os campos
                obrigatórios, ou se o fuso for desconhecido.
        """
        if not batidas:
            raise ValueError("A lista de batidas está vazia.")

        try:
            fuso = ZoneInfo(timezone_relogio)
        except Exception as exc:
            raise ValueError(f"Fuso horário desconhecido: {timezone_relogio!r}.") from exc

        por_funcionario: dict[int, set[datetime]] = defaultdict(set)

        for indice, item in enumerate(batidas):
            if not isinstance(item, dict):
                raise ValueError(f"Batida {indice}: formato inválido.")

            bruto_id = item.get("funcionario_id")
            bruto_hora = item.get("punch_time")

            if bruto_id is None or bruto_hora is None:
                raise ValueError(
                    f"Batida {indice}: 'funcionario_id' e 'punch_time' são obrigatórios."
                )

            try:
                funcionario_id = int(bruto_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Batida {indice}: funcionario_id {bruto_id!r} não é um número."
                ) from exc

            por_funcionario[funcionario_id].add(
                PontoImportService._para_utc(bruto_hora, fuso)
            )

        return {fid: sorted(horas) for fid, horas in por_funcionario.items()}

    @staticmethod
    def _validar_funcionarios(ids: list[int]) -> dict[int, Any]:
        """
        Confirma que todo funcionário do lote existe e está ativo (Regra 3).

        Args:
            ids: IDs vindos do arquivo de vínculo.

        Returns:
            Dicionário {funcionario_id: Funcionario}.

        Raises:
            ValueError: Listando todos os IDs problemáticos de uma vez, para que o
                operador corrija o CSV numa única passada.
        """
        from app.models.funcionario import Funcionario

        encontrados = {
            f.id: f
            for f in db.session.query(Funcionario).filter(Funcionario.id.in_(ids)).all()
        }

        inexistentes = sorted(set(ids) - set(encontrados))
        inativos = sorted(fid for fid, f in encontrados.items() if not f.ativo)

        problemas = []
        if inexistentes:
            problemas.append(
                "não existem: " + ", ".join(str(i) for i in inexistentes)
            )
        if inativos:
            problemas.append("estão inativos: " + ", ".join(str(i) for i in inativos))

        if problemas:
            raise ValueError(
                "Nenhuma batida foi importada. Corrija o arquivo de vínculo — "
                "funcionários que " + "; ".join(problemas) + "."
            )

        return encontrados

    # -----------------------------------------------------------------------
    # Importação
    # -----------------------------------------------------------------------

    @staticmethod
    def importar_lote(
        batidas: list[dict],
        dispositivo: str = "REP-01",
        timezone_relogio: str = "America/Sao_Paulo",
        periodo_inicio: date | None = None,
        periodo_fim: date | None = None,
        ator_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Importa um lote de batidas do relógio de ponto externo.

        Grava numa única transação e recalcula o saldo uma vez por par
        (funcionário, dia) afetado — nunca uma vez por batida.

        Args:
            batidas:          Lista de {funcionario_id, punch_time}. `punch_time` em
                              ISO 8601, tipicamente hora local do aparelho sem offset.
            dispositivo:      Identificador do relógio, registrado na auditoria.
            timezone_relogio: Fuso do aparelho, usado para converter para UTC.
            periodo_inicio:   Início do período do relatório, para a auditoria.
            periodo_fim:      Fim do período do relatório, para a auditoria.
            ator_id:          Funcionário que disparou a importação. None quando vem
                              da rota interna, autenticada por token de máquina.

        Returns:
            Dicionário com o resultado:
                importadas         — batidas gravadas
                duplicadas         — batidas que já existiam e foram puladas
                dias_ignorados     — dias descartados por passarem de 4 batidas
                batidas_ignoradas  — batidas contidas nesses dias
                dias_em_revisao    — dias importados com número ímpar (Rule #5)
                inconsistencias    — detalhe dos dias descartados

        Raises:
            ValueError: Lote vazio, payload malformado, fuso desconhecido, ou
                funcionário inexistente/inativo (Regra 3) — nesses casos nada é gravado.
        """
        from app.models.ponto import ProcessingStatus, PunchSource, PunchType, TimeEntry
        from app.services.ponto_service import PontoService
        from app.services.timezone_service import TimezoneService

        por_funcionario = PontoImportService._normalizar_batidas(batidas, timezone_relogio)
        funcionarios = PontoImportService._validar_funcionarios(list(por_funcionario))

        importadas = 0
        duplicadas = 0
        batidas_ignoradas = 0
        inconsistencias: list[dict[str, Any]] = []
        dias_afetados: set[tuple[int, date]] = set()

        for funcionario_id, horarios in por_funcionario.items():
            # Batidas já gravadas nos dias que o lote toca.
            #
            # A busca é por shift_date, não pelo intervalo de punch_time do lote:
            # um dia que já tem batidas às 08:00 e recebe uma nova às 17:00 precisa
            # contar as antigas para o limite da Regra 2, e elas ficam fora da janela
            # dos horários novos. A folga de um dia em cada ponta cobre viragens de
            # meia-noite, em que shift_date difere da data de punch_time.
            dias_do_lote = {TimezoneService.local_date(h) for h in horarios}
            existentes = (
                db.session.query(TimeEntry)
                .filter(
                    TimeEntry.funcionario_id == funcionario_id,
                    TimeEntry.shift_date >= min(dias_do_lote) - timedelta(days=1),
                    TimeEntry.shift_date <= max(dias_do_lote) + timedelta(days=1),
                )
                .all()
            )
            ja_gravadas = {
                TimezoneService.ensure_utc(e.punch_time) for e in existentes
            }

            # Regra 1 — o que já existe é pulado.
            novas = [h for h in horarios if h not in ja_gravadas]
            duplicadas += len(horarios) - len(novas)
            if not novas:
                continue

            # Agrupa por data operacional, no fuso do sistema.
            novas_por_dia: dict[date, list[datetime]] = defaultdict(list)
            for horario in novas:
                novas_por_dia[TimezoneService.local_date(horario)].append(horario)

            for shift_date, horarios_do_dia in sorted(novas_por_dia.items()):
                existentes_no_dia = [
                    e for e in existentes
                    if TimezoneService.local_date(e.punch_time) == shift_date
                ]
                total = len(existentes_no_dia) + len(horarios_do_dia)

                # Regra 2 — dia que passaria de 4 batidas não entra.
                if total > MAX_BATIDAS_DIA:
                    batidas_ignoradas += len(horarios_do_dia)
                    inconsistencias.append(
                        {
                            "funcionario_id": funcionario_id,
                            "funcionario_nome": funcionarios[funcionario_id].nome,
                            "shift_date": shift_date.isoformat(),
                            "batidas_no_dia": total,
                            "ja_existiam": len(existentes_no_dia),
                        }
                    )
                    continue

                # O tipo sai da posição cronológica da batida dentro do dia inteiro,
                # contando as que já estavam gravadas.
                ordem_do_dia = sorted(
                    [TimezoneService.ensure_utc(e.punch_time) for e in existentes_no_dia]
                    + horarios_do_dia
                )

                for horario in horarios_do_dia:
                    db.session.add(
                        TimeEntry(
                            funcionario_id=funcionario_id,
                            punch_time=horario,
                            shift_date=shift_date,
                            punch_type=PontoService._inferir_tipo_batida(
                                ordem_do_dia.index(horario)
                            ),
                            source=PunchSource.IMPORT,
                            processing_status=ProcessingStatus.PENDENTE,
                        )
                    )
                    importadas += 1

                dias_afetados.add((funcionario_id, shift_date))

        # Regra 4 — um log por importação, não um por batida.
        AuditService.log(
            AuditAction.create,
            "ponto",
            new_state={
                "operacao": "importacao_lote",
                "dispositivo": dispositivo,
                "timezone": timezone_relogio,
                "periodo_inicio": periodo_inicio.isoformat() if periodo_inicio else None,
                "periodo_fim": periodo_fim.isoformat() if periodo_fim else None,
                "funcionarios": sorted(por_funcionario),
                "importadas": importadas,
                "duplicadas": duplicadas,
                "dias_ignorados": len(inconsistencias),
                "batidas_ignoradas": batidas_ignoradas,
                "inconsistencias": inconsistencias,
            },
            actor_id=ator_id,
        )
        db.session.commit()

        # Recalcula uma vez por dia afetado — não uma vez por batida.
        dias_em_revisao = 0
        for funcionario_id, shift_date in sorted(dias_afetados):
            dia = PontoService.processar_dia(funcionario_id, shift_date)
            if dia and dia.needs_review:
                dias_em_revisao += 1

        if inconsistencias:
            PontoImportService._avisar_inconsistencias(
                inconsistencias, dispositivo, ator_id
            )

        return {
            "importadas": importadas,
            "duplicadas": duplicadas,
            "dias_ignorados": len(inconsistencias),
            "batidas_ignoradas": batidas_ignoradas,
            "dias_em_revisao": dias_em_revisao,
            "inconsistencias": inconsistencias,
        }

    # -----------------------------------------------------------------------
    # Aviso de inconsistência
    # -----------------------------------------------------------------------

    @staticmethod
    def _destinatarios_folha_ponto(): # noqa: ANN205
        """Funcionários ativos que podem corrigir a Folha de Ponto, mais super admins."""
        from app.models.funcionario import Funcionario
        from app.models.role import Role, RolePermission

        roles_com_permissao = (
            db.session.query(RolePermission.role_id)
            .filter_by(modulo="folha_ponto", acao="edit")
            .subquery()
        )
        com_permissao = (
            db.session.query(Funcionario)
            .filter(
                Funcionario.ativo == True,  # noqa: E712
                Funcionario.role_id.in_(db.session.query(roles_com_permissao.c.role_id)),
            )
            .all()
        )
        super_admins = (
            db.session.query(Funcionario)
            .join(Role, Funcionario.role_id == Role.id)
            .filter(
                Funcionario.ativo == True,  # noqa: E712
                Role.is_super_admin == True,  # noqa: E712
            )
            .all()
        )
        return {f.id for f in com_permissao} | {f.id for f in super_admins}

    @staticmethod
    def _avisar_inconsistencias(
        inconsistencias: list[dict[str, Any]],
        dispositivo: str,
        ator_id: int | None,
    ) -> int:
        """
        Emite um Aviso por destinatário resumindo os dias descartados (Regra 2).

        Um aviso por importação, não um por dia: um relatório com vinte dias
        inconsistentes geraria sessenta notificações e ninguém leria nenhuma.

        Args:
            inconsistencias: Dias descartados, no formato devolvido por importar_lote.
            dispositivo:     Identificador do relógio, para o título do aviso.
            ator_id:         Autor da ação, para a auditoria dos avisos.

        Returns:
            Quantidade de avisos criados.
        """
        from app.models.avisos import TipoAviso
        from app.services.aviso_service import AvisoService

        destinatarios = PontoImportService._destinatarios_folha_ponto()
        if not destinatarios:
            return 0

        linhas = [
            f"• {i['funcionario_nome']} — {_data_br(i['shift_date'])}: "
            f"{i['batidas_no_dia']} batidas"
            for i in inconsistencias[:_LIMITE_DETALHE_AVISO]
        ]
        if len(inconsistencias) > _LIMITE_DETALHE_AVISO:
            linhas.append(f"• … e mais {len(inconsistencias) - _LIMITE_DETALHE_AVISO} dia(s).")

        muitos = len(inconsistencias) > 1
        plural = "s" if muitos else ""
        mensagem = (
            f"A importação do relógio {dispositivo} encontrou {len(inconsistencias)} "
            f"dia{plural} com mais de {MAX_BATIDAS_DIA} batidas. "
            + (
                "Esses dias NÃO foram importados — nenhuma batida deles está no sistema."
                if muitos
                else "Esse dia NÃO foi importado — nenhuma batida dele está no sistema."
            )
            + "\n\n"
            + "\n".join(linhas)
            + "\n\nCausas comuns: leitura dupla no aparelho, vínculo trocado no arquivo "
            "de importação, ou relatório de período errado. Confira e lance as batidas "
            "corretas pela Folha de Ponto."
        )

        criados = 0
        for destinatario_id in sorted(destinatarios):
            try:
                AvisoService.criar(
                    destinatario_id=destinatario_id,
                    titulo=f"Importação de ponto: {len(inconsistencias)} dia{plural} inconsistente{plural}",
                    mensagem=mensagem,
                    tipo=TipoAviso.SISTEMA.value,
                    ator_id=ator_id,
                )
                criados += 1
            except ValueError:
                # Destinatário desativado entre a consulta e o envio — segue adiante.
                continue

        return criados


def _data_br(iso: str) -> str:
    """Converte 'AAAA-MM-DD' para 'DD/MM/AAAA'."""
    try:
        ano, mes, dia = iso.split("-")
        return f"{dia}/{mes}/{ano}"
    except ValueError:
        return iso
