# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
EscalaService — Calendário mensal de turnos e folgas.

Arquitetura em camadas: sem imports do Flask — puro Python, compatível com Cython.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime

from sqlalchemy import func

from app.core.audit import AuditAction, AuditService
from app.extensions import db
from app.models.escala import EscalaEntrada
from app.models.funcionario import Funcionario
from app.services.base import BaseService

_TIPOS_VALIDOS = {"NORMAL", "FOLGA", "FALTA_JUSTIFICADA"}
_LABELS_VALIDOS = {"MANHA", "TARDE", "NOITE", "PLANTAO"}

# Chaves de jornada_semanal alinhadas a date.weekday() (0=segunda ... 6=domingo).
_DIAS_SEMANA = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")


def _normalizar_labels(labels) -> list[str]:
    """Valida e normaliza a lista de rótulos opcionais de turno.

    Args:
        labels: Iterável de rótulos (ex.: ["MANHA", "tarde"]) ou None.

    Returns:
        Lista ordenada e sem duplicatas, apenas com rótulos válidos.

    Raises:
        ValueError: Se algum rótulo for inválido.
    """
    if not labels:
        return []
    vistos = []
    for raw in labels:
        v = (raw or "").strip().upper()
        if not v:
            continue
        if v not in _LABELS_VALIDOS:
            raise ValueError(f"Rótulo '{v}' inválido. Use: {', '.join(sorted(_LABELS_VALIDOS))}")
        if v not in vistos:
            vistos.append(v)
    return sorted(vistos)


def _label_do_horario(horario) -> str | None:
    """Deriva um rótulo de turno a partir do horário de entrada padrão.

    Regra: até 12h → MANHA; 12h–17h → TARDE; 17h em diante → NOITE.

    Args:
        horario: datetime.time ou None.

    Returns:
        Rótulo derivado, ou None se o horário não estiver definido.
    """
    if horario is None:
        return None
    if horario.hour < 12:
        return "MANHA"
    if horario.hour < 17:
        return "TARDE"
    return "NOITE"


_LABELS_HUMANOS = {"MANHA": "Manhã", "TARDE": "Tarde", "NOITE": "Noite", "PLANTAO": "Plantão"}


def _descrever_dia(tipo: str, labels: list[str]) -> str:
    """Descrição legível de um dia de escala (ex.: "Trabalho (Manhã)" ou "Folga")."""
    if tipo == "FOLGA":
        return "Folga"
    if tipo == "FALTA_JUSTIFICADA":
        return "Falta justificada"
    if labels:
        turnos = ", ".join(_LABELS_HUMANOS.get(l, l) for l in labels)
        return f"Trabalho ({turnos})"
    return "Trabalho"


def _reprocessar_ponto_se_falta_justificada(funcionario_id: int, data: date, tipo: str) -> None:
    """Reprocessa o dia no Ponto quando a escala passa a ser FALTA_JUSTIFICADA.

    `_get_expected_minutes()` já passa a devolver 0 para o dia assim que a
    EscalaEntrada existe — mas isso só se reflete num `TimeDay` já criado
    (ex.: débito provisório de `AvisoService.verificar_atrasos()`,
    `auto_debit_aplicado=True`, saldo negativo) se alguém reprocessar esse
    dia. Sem este gatilho, a justificativa ficaria correta "na teoria" e o
    débito continuaria pendurado até a próxima batida real ou um
    reprocessamento manual — dias depois, na prática.

    Diferente de `_notificar_escala`, isto NÃO é melhor esforço: se
    `processar_dia` falhar, a exceção propaga. A garantia pedida é que a
    justificativa anule a pendência no banco de horas na hora do
    lançamento — engolir o erro silenciosamente deixaria o operador achando
    que anulou quando não anulou.

    Args:
        funcionario_id: PK do funcionário.
        data: Data da escala recém-registrada.
        tipo: Tipo já normalizado da entrada — só reprocessa se
            "FALTA_JUSTIFICADA".
    """
    if tipo != "FALTA_JUSTIFICADA":
        return
    from app.services.ponto_service import PontoService

    PontoService.processar_dia(funcionario_id, data)


def _notificar_escala(funcionario_id: int, titulo: str, mensagem: str, ator_id: int | None) -> None:
    """Envia um Aviso de escala ao funcionário (melhor esforço).

    A notificação nunca deve impedir o registro da escala: destinatário
    inativo (ou outra falha de validação do aviso) é ignorado silenciosamente.
    """
    from app.services.aviso_service import AvisoService

    try:
        AvisoService.criar(
            destinatario_id=funcionario_id,
            titulo=titulo,
            mensagem=mensagem,
            tipo="SISTEMA",
            remetente_id=ator_id,
            ator_id=ator_id,
        )
    except ValueError:
        pass


class EscalaService(BaseService):
    """Gerencia escalas por funcionário × dia (estado base + rótulos de turno)."""

    @staticmethod
    def calendario_mes(
        ano: int,
        mes: int,
        area_id: int | None = None,
        equipe_id: int | None = None,
    ) -> dict:
        """Monta o calendário mensal de escalas.

        Retorna estrutura pronta para o template: lista de funcionários com
        um dict {dia: EscalaEntrada | None} para cada dia do mês.

        Args:
            ano: Ano (ex.: 2026).
            mes: Mês (1–12).
            area_id: Filtra funcionários por área. None = todas.
            equipe_id: Filtra funcionários por equipe. None = todas.

        Returns:
            Dicionário com:
            - dias_no_mes (int)
            - fins_semana (set[int])
            - linhas (list[dict]):
                - funcionario (Funcionario)
                - dias (dict[int, EscalaEntrada | None])
            - totais (dict[str, int]): contagem por tipo (NORMAL/FOLGA) no mês
            - totais_labels (dict[str, int]): contagem por rótulo de turno no mês
        """
        inicio = date(ano, mes, 1)
        dias_no_mes = calendar.monthrange(ano, mes)[1]
        fim = date(ano, mes, dias_no_mes)

        fins_semana = {
            d for d in range(1, dias_no_mes + 1)
            if date(ano, mes, d).weekday() >= 5
        }

        query = db.session.query(Funcionario).filter_by(ativo=True).order_by(Funcionario.nome)

        if area_id:
            query = query.filter(Funcionario.area_id == area_id)

        if equipe_id:
            from app.models.organizacao import EquipeMembro
            membros_ids = db.session.query(EquipeMembro.funcionario_id).filter_by(equipe_id=equipe_id).subquery()
            query = query.filter(Funcionario.id.in_(membros_ids))

        funcionarios = query.all()
        fids = [f.id for f in funcionarios]

        entradas = (
            db.session.query(EscalaEntrada)
            .filter(
                EscalaEntrada.funcionario_id.in_(fids),
                EscalaEntrada.data >= inicio,
                EscalaEntrada.data <= fim,
            )
            .all()
        ) if fids else []

        # lookup: (funcionario_id, dia) → EscalaEntrada
        lookup: dict[tuple[int, int], EscalaEntrada] = {
            (e.funcionario_id, e.data.day): e for e in entradas
        }

        # totais por tipo (NORMAL/FOLGA) e por rótulo de turno
        totais: dict[str, int] = {t: 0 for t in _TIPOS_VALIDOS}
        totais_labels: dict[str, int] = {lb: 0 for lb in _LABELS_VALIDOS}
        for e in entradas:
            totais[e.tipo] = totais.get(e.tipo, 0) + 1
            for lb in (e.labels or []):
                totais_labels[lb] = totais_labels.get(lb, 0) + 1

        linhas = [
            {
                "funcionario": f,
                "dias": {
                    d: lookup.get((f.id, d))
                    for d in range(1, dias_no_mes + 1)
                },
            }
            for f in funcionarios
        ]

        return {
            "dias_no_mes": dias_no_mes,
            "fins_semana": fins_semana,
            "linhas": linhas,
            "totais": totais,
            "totais_labels": totais_labels,
        }

    _SNAP_FIELDS = ["funcionario_id", "data", "tipo", "labels", "equipe_id", "justificativa"]
    _TIPOS_SEM_LABELS = {"FOLGA", "FALTA_JUSTIFICADA"}

    @staticmethod
    def registrar(
        funcionario_id: int,
        data: date,
        tipo: str,
        labels: list[str] | None = None,
        equipe_id: int | None = None,
        justificativa: str | None = None,
        ator_id: int | None = None,
        notificar: bool = True,
    ) -> EscalaEntrada:
        """Registra ou atualiza a escala de um funcionário em uma data.

        Upsert: se já existir entrada para (funcionario_id, data), atualiza.
        Publicar = existir entrada (D7): por isso o funcionário é notificado
        via Aviso quando o dia é criado ou muda de estado (tipo/labels).

        Quando `tipo == "FALTA_JUSTIFICADA"`, reprocessa o dia no Ponto
        (`PontoService.processar_dia`) antes de retornar — garante que um
        débito provisório já aplicado (`TimeDay.auto_debit_aplicado`, por
        `AvisoService.verificar_atrasos()`) seja anulado na hora do
        lançamento da justificativa, não só na próxima batida real ou
        reprocessamento manual. Ver `_reprocessar_ponto_se_falta_justificada`.

        Args:
            funcionario_id: PK do funcionário.
            data: Data da escala.
            tipo: Estado base do dia — "NORMAL" (trabalha), "FOLGA" (descanso)
                ou "FALTA_JUSTIFICADA" (ausência justificada, não descontada
                do banco de horas).
            labels: Rótulos opcionais de turno (MANHA/TARDE/NOITE/PLANTAO).
                Ignorados quando `tipo` é "FOLGA" ou "FALTA_JUSTIFICADA".
            equipe_id: Equipe associada (opcional).
            justificativa: Texto livre. **Obrigatório** quando
                `tipo == "FALTA_JUSTIFICADA"` — é o registro escrito do
                motivo da ausência; opcional nos demais tipos. Pode conter
                URLs, que a UI reconhece como links.
            ator_id: ID do usuário que realizou a ação (para auditoria).
            notificar: Se True (padrão), envia Aviso ao funcionário quando o
                dia é criado ou muda. Fluxos em massa (geração/lote) passam
                False e enviam o próprio resumo.

        Returns:
            EscalaEntrada persistida.

        Raises:
            ValueError: Se tipo/rótulo inválido, funcionário não encontrado,
                ou FALTA_JUSTIFICADA sem justificativa.
        """
        tipo = (tipo or "").strip().upper()
        if tipo not in _TIPOS_VALIDOS:
            raise ValueError(f"Tipo '{tipo}' inválido. Use: {', '.join(sorted(_TIPOS_VALIDOS))}")

        justificativa = (justificativa or "").strip() or None
        if tipo == "FALTA_JUSTIFICADA" and not justificativa:
            raise ValueError(
                "Falta justificada exige uma justificativa por escrito."
            )

        labels_norm = [] if tipo in EscalaService._TIPOS_SEM_LABELS else _normalizar_labels(labels)

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError("Funcionário não encontrado.")

        existente = db.session.query(EscalaEntrada).filter_by(
            funcionario_id=funcionario_id, data=data
        ).first()

        if existente:
            mudou = existente.tipo != tipo or (existente.labels or []) != labels_norm
            snap_before = EscalaService._snapshot(existente, EscalaService._SNAP_FIELDS)
            existente.tipo = tipo
            existente.labels = labels_norm
            existente.equipe_id = equipe_id
            existente.justificativa = justificativa
            existente.criado_por_id = ator_id
            existente.atualizado_em = datetime.utcnow()
            db.session.flush()
            snap_after = EscalaService._snapshot(existente, EscalaService._SNAP_FIELDS)
            AuditService.log(
                AuditAction.update, "escala",
                entity_id=existente.id,
                previous_state=snap_before,
                new_state=snap_after,
                actor_id=ator_id,
            )
            if notificar and mudou:
                _notificar_escala(
                    funcionario_id,
                    "Alteração na sua escala",
                    f"Sua escala de {data.strftime('%d/%m/%Y')} mudou para: "
                    f"{_descrever_dia(tipo, labels_norm)}.",
                    ator_id,
                )
            _reprocessar_ponto_se_falta_justificada(funcionario_id, data, tipo)
            return existente

        entrada = EscalaEntrada(
            funcionario_id=funcionario_id,
            data=data,
            tipo=tipo,
            labels=labels_norm,
            equipe_id=equipe_id,
            justificativa=justificativa,
            criado_por_id=ator_id,
        )
        db.session.add(entrada)
        db.session.flush()
        snap = EscalaService._snapshot(entrada, EscalaService._SNAP_FIELDS)
        AuditService.log(
            AuditAction.create, "escala",
            entity_id=entrada.id,
            previous_state=None,
            new_state=snap,
            actor_id=ator_id,
        )
        if notificar:
            _notificar_escala(
                funcionario_id,
                "Escala publicada",
                f"Sua escala de {data.strftime('%d/%m/%Y')} foi definida: "
                f"{_descrever_dia(tipo, labels_norm)}.",
                ator_id,
            )
        _reprocessar_ponto_se_falta_justificada(funcionario_id, data, tipo)
        return entrada

    @staticmethod
    def registrar_lote(
        funcionario_ids: list[int],
        datas: list[date],
        tipo: str,
        labels: list[str] | None = None,
        equipe_id: int | None = None,
        justificativa: str | None = None,
        ator_id: int | None = None,
    ) -> int:
        """Registra o mesmo estado para múltiplos funcionários e/ou datas.

        Chama `registrar` para cada combinação (funcionario_id, data) — herda
        de lá a exigência de justificativa quando tipo == "FALTA_JUSTIFICADA"
        (a primeira combinação sem justificativa aborta o lote inteiro com
        ValueError; combinações já processadas no loop permanecem persistidas,
        mesma limitação que já existia para qualquer outra falha de validação
        no meio do lote).

        Args:
            funcionario_ids: Lista de PKs de funcionários.
            datas: Lista de datas.
            tipo: Estado base a aplicar a todos — "NORMAL", "FOLGA" ou
                "FALTA_JUSTIFICADA".
            labels: Rótulos opcionais de turno.
            equipe_id: Equipe associada (opcional).
            justificativa: Texto livre — obrigatório quando
                `tipo == "FALTA_JUSTIFICADA"`, aplicado a todas as
                combinações do lote (ex.: um atestado que cobre vários dias).
            ator_id: ID do usuário que realizou a ação.

        Returns:
            Número de entradas criadas ou atualizadas.

        Notas:
            Em vez de um Aviso por célula, cada funcionário recebe um único
            resumo com as datas afetadas.
        """
        count = 0
        tipo_norm = (tipo or "").strip().upper()
        labels_norm = [] if tipo_norm in EscalaService._TIPOS_SEM_LABELS else _normalizar_labels(labels)
        for fid in funcionario_ids:
            for d in datas:
                EscalaService.registrar(
                    fid, d, tipo, labels, equipe_id, justificativa, ator_id,
                    notificar=False,
                )
                count += 1
            if datas:
                dias_fmt = ", ".join(d.strftime("%d/%m") for d in sorted(datas))
                _notificar_escala(
                    fid,
                    "Alteração na sua escala",
                    f"Sua escala foi atualizada para {_descrever_dia(tipo_norm, labels_norm)} "
                    f"no(s) dia(s): {dias_fmt}.",
                    ator_id,
                )
        return count

    @staticmethod
    def remover(
        funcionario_id: int,
        data: date,
        ator_id: int | None = None,
    ) -> None:
        """Remove a entrada de escala de um funcionário em uma data.

        Args:
            funcionario_id: PK do funcionário.
            data: Data a remover.
            ator_id: ID do usuário (auditoria).

        Raises:
            ValueError: Se não existir entrada para (funcionario_id, data).
        """
        entrada = db.session.query(EscalaEntrada).filter_by(
            funcionario_id=funcionario_id, data=data
        ).first()
        if not entrada:
            raise ValueError("Entrada de escala não encontrada.")

        snap = EscalaService._snapshot(entrada, EscalaService._SNAP_FIELDS)
        eid = entrada.id
        db.session.delete(entrada)
        db.session.flush()
        AuditService.log(
            AuditAction.delete, "escala",
            entity_id=eid,
            previous_state=snap,
            new_state=None,
            actor_id=ator_id,
        )
        _notificar_escala(
            funcionario_id,
            "Alteração na sua escala",
            f"A escala de {data.strftime('%d/%m/%Y')} foi removida — "
            "vale sua jornada padrão nesse dia.",
            ator_id,
        )

    @staticmethod
    def funcionarios_de_folga(data: date) -> list[int]:
        """Retorna IDs dos funcionários com FOLGA registrada em uma data.

        Args:
            data: Data a consultar.

        Returns:
            Lista de funcionario_id com tipo == 'FOLGA' na data.
        """
        rows = (
            db.session.query(EscalaEntrada.funcionario_id)
            .filter(EscalaEntrada.data == data, EscalaEntrada.tipo == "FOLGA")
            .all()
        )
        return [r.funcionario_id for r in rows]

    @staticmethod
    def resumo_mes(funcionario_id: int, ano: int, mes: int) -> dict[str, int]:
        """Conta entradas por tipo para um funcionário em um mês.

        Args:
            funcionario_id: PK do funcionário.
            ano: Ano.
            mes: Mês (1–12).

        Returns:
            Dict tipo → contagem (ex.: {"NORMAL": 22, "FOLGA": 8}).
        """
        inicio = date(ano, mes, 1)
        dias_no_mes = calendar.monthrange(ano, mes)[1]
        fim = date(ano, mes, dias_no_mes)

        rows = (
            db.session.query(EscalaEntrada.tipo, func.count(EscalaEntrada.id))
            .filter(
                EscalaEntrada.funcionario_id == funcionario_id,
                EscalaEntrada.data >= inicio,
                EscalaEntrada.data <= fim,
            )
            .group_by(EscalaEntrada.tipo)
            .all()
        )
        return {tipo: cnt for tipo, cnt in rows}

    @staticmethod
    def gerar_a_partir_do_padrao(
        ano: int,
        mes: int,
        area_id: int | None = None,
        equipe_id: int | None = None,
        sobrescrever: bool = False,
        ator_id: int | None = None,
    ) -> dict:
        """Gera a escala do mês a partir da jornada semanal de cada funcionário.

        Para cada funcionário CLT ativo com `jornada_semanal` definida, percorre
        os dias do mês e escala automaticamente:
          - dia da semana **ativo** na jornada → tipo NORMAL, com o rótulo de
            turno derivado do `horario_entrada_padrao` (quando houver);
          - dia da semana **inativo** → tipo FOLGA.

        Diaristas e funcionários sem jornada não são escalados (entram em
        `sem_padrao`). Por padrão preenche apenas dias ainda vazios; com
        `sobrescrever=True` substitui entradas existentes.

        Args:
            ano: Ano (ex.: 2026).
            mes: Mês (1–12).
            area_id: Filtra funcionários por área. None = todas.
            equipe_id: Filtra funcionários por equipe. None = todas.
            sobrescrever: Se True, substitui entradas já existentes no mês.
            ator_id: ID do usuário que realizou a ação (auditoria).

        Returns:
            Dicionário com:
            - criados (int), atualizados (int), pulados (int)
            - sem_padrao (list[Funcionario]): CLTs sem jornada definida
            - total_funcionarios (int): considerados na geração
        """
        inicio = date(ano, mes, 1)
        dias_no_mes = calendar.monthrange(ano, mes)[1]
        fim = date(ano, mes, dias_no_mes)

        query = (
            db.session.query(Funcionario)
            .filter(Funcionario.ativo == True, Funcionario.tipo_contrato == "CLT")
            .order_by(Funcionario.nome)
        )
        if area_id:
            query = query.filter(Funcionario.area_id == area_id)
        if equipe_id:
            from app.models.organizacao import EquipeMembro
            membros_ids = db.session.query(EquipeMembro.funcionario_id).filter_by(equipe_id=equipe_id).subquery()
            query = query.filter(Funcionario.id.in_(membros_ids))

        funcionarios = query.all()
        fids = [f.id for f in funcionarios]

        # Entradas já existentes no mês para evitar uma query por célula.
        existentes: set[tuple[int, int]] = set()
        if fids:
            for e in (
                db.session.query(EscalaEntrada.funcionario_id, EscalaEntrada.data)
                .filter(
                    EscalaEntrada.funcionario_id.in_(fids),
                    EscalaEntrada.data >= inicio,
                    EscalaEntrada.data <= fim,
                )
                .all()
            ):
                existentes.add((e.funcionario_id, e.data.day))

        criados = atualizados = pulados = 0
        sem_padrao: list[Funcionario] = []

        for f in funcionarios:
            jornada = f.jornada_semanal
            if not jornada:
                sem_padrao.append(f)
                continue
            label_padrao = _label_do_horario(f.horario_entrada_padrao)
            dias_trabalho = dias_folga = escritos = 0
            for d in range(1, dias_no_mes + 1):
                data = date(ano, mes, d)
                ja_existe = (f.id, d) in existentes
                if ja_existe and not sobrescrever:
                    pulados += 1
                    continue
                dia_cfg = jornada.get(_DIAS_SEMANA[data.weekday()]) or {}
                if dia_cfg.get("ativo"):
                    tipo = "NORMAL"
                    labels = [label_padrao] if label_padrao else []
                    dias_trabalho += 1
                else:
                    tipo = "FOLGA"
                    labels = []
                    dias_folga += 1
                EscalaService.registrar(
                    f.id, data, tipo, labels, equipe_id, None, ator_id,
                    notificar=False,
                )
                escritos += 1
                if ja_existe:
                    atualizados += 1
                else:
                    criados += 1
            # Um único resumo por funcionário, em vez de um Aviso por dia.
            if escritos > 0:
                _notificar_escala(
                    f.id,
                    "Escala publicada",
                    f"Sua escala de {mes:02d}/{ano} foi publicada: "
                    f"{dias_trabalho} dia(s) de trabalho e {dias_folga} folga(s).",
                    ator_id,
                )

        return {
            "criados": criados,
            "atualizados": atualizados,
            "pulados": pulados,
            "sem_padrao": sem_padrao,
            "total_funcionarios": len(funcionarios),
        }

    @staticmethod
    def presenca_dia(data: date, area_id: int | None = None) -> list[dict]:
        """Retorna o status de presença de cada funcionário escalado (NORMAL) em `data`.

        Cruza EscalaEntrada NORMAL × TimeDay × primeira TimeEntry para derivar um
        status operacional por colaborador. Útil para o painel de Presença do Dia
        no dashboard de RH.

        Args:
            data: Data a consultar (geralmente date.today()).
            area_id: Filtra por área. None = todas as áreas.

        Returns:
            Lista de dicionários, um por funcionário escalado como NORMAL, com:
            - funcionario (Funcionario): instância do modelo
            - status (str): "presente" | "atrasado" | "ausente" | "revisar" | "pendente"
            - primeira_batida (datetime | None): horário UTC da primeira batida, se houver
            - saldo (int | None): saldo_final_minutos do TimeDay, se disponível
            - labels (list[str]): rótulos de turno (MANHA/TARDE/NOITE/PLANTAO)
        """
        from datetime import datetime, timezone
        from app.models.ponto import TimeDay, TimeEntry
        from app.services.timezone_service import TimezoneService

        query = (
            db.session.query(EscalaEntrada, Funcionario)
            .join(Funcionario, EscalaEntrada.funcionario_id == Funcionario.id)
            .filter(
                EscalaEntrada.data == data,
                EscalaEntrada.tipo == "NORMAL",
                Funcionario.ativo == True,
            )
        )
        if area_id:
            query = query.filter(Funcionario.area_id == area_id)

        escalados = query.order_by(Funcionario.nome).all()
        if not escalados:
            return []

        fids = [funcionario.id for _, funcionario in escalados]

        # Busca TimeDay em lote
        time_days: dict[int, TimeDay] = {
            td.funcionario_id: td
            for td in db.session.query(TimeDay).filter(
                TimeDay.funcionario_id.in_(fids),
                TimeDay.shift_date == data,
            ).all()
        }

        # Busca a primeira batida de cada funcionário no dia
        primeiras: dict[int, datetime] = {
            row.funcionario_id: row.primeira
            for row in db.session.query(
                TimeEntry.funcionario_id,
                func.min(TimeEntry.punch_time).label("primeira"),
            ).filter(
                TimeEntry.funcionario_id.in_(fids),
                TimeEntry.shift_date == data,
            ).group_by(TimeEntry.funcionario_id).all()
        }

        hoje = datetime.now(timezone.utc).date()
        tz = TimezoneService.system_timezone()
        tolerancia_min = 10

        resultado: list[dict] = []
        for entrada_escala, funcionario in escalados:
            td = time_days.get(funcionario.id)
            primeira = primeiras.get(funcionario.id)

            if td and td.auto_debit_aplicado and not primeira:
                status = "ausente"
            elif td and td.needs_review:
                status = "revisar"
            elif primeira:
                if funcionario.horario_entrada_padrao:
                    primeira_local = TimezoneService.ensure_utc(primeira).astimezone(tz)
                    esperado_min = (
                        funcionario.horario_entrada_padrao.hour * 60
                        + funcionario.horario_entrada_padrao.minute
                    )
                    real_min = primeira_local.hour * 60 + primeira_local.minute
                    status = "atrasado" if (real_min - esperado_min) > tolerancia_min else "presente"
                else:
                    status = "presente"
            elif data < hoje:
                status = "ausente"
            elif data == hoje and funcionario.horario_entrada_padrao:
                agora_local = datetime.now(tz)
                esperado_min = (
                    funcionario.horario_entrada_padrao.hour * 60
                    + funcionario.horario_entrada_padrao.minute
                )
                agora_min = agora_local.hour * 60 + agora_local.minute
                status = "ausente" if (agora_min - esperado_min) > tolerancia_min else "pendente"
            else:
                status = "pendente"

            resultado.append({
                "funcionario": funcionario,
                "status": status,
                "primeira_batida": primeira,
                "saldo": td.saldo_final_minutos if td else None,
                "labels": entrada_escala.labels or [],
            })

        return resultado
