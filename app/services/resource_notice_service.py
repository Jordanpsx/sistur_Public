# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
ResourceNoticeService — lógica de negócio para avisos de recursos.

Regras:
  - Aviso global (source_id=NULL) afeta todos os sources.
  - Aviso local (source_id=int) afeta apenas o source especificado.
  - blocks_source: impede criação de reserva no período.
  - blocks_items: torna itens indisponíveis no picker (stock virtual = 0).
  - agreed_notices_json na Reserva: prova legal do aceite no momento da reserva.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_

from app.core.audit import AuditService
from app.extensions import db
from app.models.reservas import ResourceNotice
from app.services.base import BaseService

_AUDIT_MODULE = "resource_notices"
_SNAPSHOT_FIELDS = [
    "id", "source_id", "event_name", "start_date", "end_date", "recurring_weekday",
    "blocks_source", "blocks_items", "blocked_item_ids",
]

_PT_WEEKDAYS = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]


def _intervalo_contem_weekday(inicio: date, fim: date, weekday: int) -> bool:
    """Verifica se algum dia em [inicio, fim] cai no dia da semana informado.

    Calcula a primeira ocorrência do weekday a partir de `inicio` sem iterar
    dia a dia, e checa se ela ainda cabe dentro de `fim`.

    Args:
        inicio: Início do intervalo (inclusive).
        fim: Fim do intervalo (inclusive).
        weekday: 0=segunda ... 6=domingo (convenção de date.weekday()).

    Returns:
        True se existe ao menos uma ocorrência do weekday no intervalo.
    """
    if inicio > fim:
        return False
    dias_ate_o_weekday = (weekday - inicio.weekday()) % 7
    primeira_ocorrencia = inicio + timedelta(days=dias_ate_o_weekday)
    return primeira_ocorrencia <= fim


class ResourceNoticeService:
    """Serviço de avisos de recursos — consultas e validações de período."""

    @staticmethod
    def formatar_texto(notice: ResourceNotice) -> str:
        """Retorna texto formatado do aviso com datas no padrão brasileiro.

        Args:
            notice: Instância de ResourceNotice a ser formatada.

        Returns:
            String formatada com nome do evento e período.
            Exemplo de data única: "Manutenção em 25/12/2026"
            Exemplo de intervalo: "Manutenção de 25/12/2026 a 31/12/2026"
            Exemplo de recorrência: "Manutenção toda quarta-feira, de 01/09/2026
            a 31/12/2026"
        """
        if notice.recurring_weekday is not None:
            dia = _PT_WEEKDAYS[notice.recurring_weekday]
            artigo = "todo" if notice.recurring_weekday >= 5 else "toda"
            return (
                f"{notice.event_name} {artigo} {dia}, de {notice.start_date:%d/%m/%Y}"
                f" a {notice.end_date:%d/%m/%Y}"
            )
        if notice.start_date == notice.end_date:
            return f"{notice.event_name} em {notice.start_date:%d/%m/%Y}"
        return (
            f"{notice.event_name} de {notice.start_date:%d/%m/%Y}"
            f" a {notice.end_date:%d/%m/%Y}"
        )

    @staticmethod
    def buscar_avisos_periodo(
        start_date: date,
        end_date: date,
        source_id: int | None = None,
    ) -> list[ResourceNotice]:
        """Retorna avisos ativos que intersectam o período informado.

        A condição de interseção é: notice.start_date <= end_date E
        notice.end_date >= start_date (abrange qualquer sobreposição de datas).
        Para avisos recorrentes (recurring_weekday preenchido), essa sobreposição
        de intervalo é só um pré-filtro — o aviso só entra no resultado se a
        janela de sobreposição realmente contém uma ocorrência do dia da semana
        (ex.: um aviso "toda quarta-feira" não deve bloquear uma consulta de
        segunda a terça só porque os intervalos se tocam).

        Filtro de escopo:
          - Se source_id for fornecido: retorna avisos globais (source_id IS NULL)
            ou locais para o source especificado.
          - Se source_id não fornecido: retorna todos os avisos (globais + locais).

        Args:
            start_date: Data de início do período de consulta.
            end_date: Data de fim do período de consulta.
            source_id: ID do source para filtrar avisos locais. None retorna todos.

        Returns:
            Lista de ResourceNotice ordenada por start_date ascendente.
        """
        query = db.session.query(ResourceNotice).filter(
            ResourceNotice.deleted_at.is_(None),
            ResourceNotice.start_date <= end_date,
            ResourceNotice.end_date >= start_date,
        )

        if source_id is not None:
            query = query.filter(
                or_(
                    ResourceNotice.source_id.is_(None),
                    ResourceNotice.source_id == source_id,
                )
            )

        candidatos = query.order_by(ResourceNotice.start_date.asc()).all()

        resultado = []
        for notice in candidatos:
            if notice.recurring_weekday is None:
                resultado.append(notice)
                continue
            janela_inicio = max(notice.start_date, start_date)
            janela_fim = min(notice.end_date, end_date)
            if _intervalo_contem_weekday(janela_inicio, janela_fim, notice.recurring_weekday):
                resultado.append(notice)
        return resultado

    @classmethod
    def avisos_para_api(
        cls,
        start_date: date,
        end_date: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Retorna avisos do período formatados como lista de dicionários para API.

        Args:
            start_date: Data de início do período de consulta.
            end_date: Data de fim do período de consulta.
            source_id: ID do source para filtrar avisos locais. None retorna todos.

        Returns:
            Lista de dicts com chaves: id, formatted_text, blocks_source,
            blocks_items, scope.
        """
        notices = cls.buscar_avisos_periodo(start_date, end_date, source_id)
        return [
            {
                "id": notice.id,
                "formatted_text": cls.formatar_texto(notice),
                "blocks_source": notice.blocks_source,
                "blocks_items": notice.blocks_items,
                "scope": "global" if notice.source_id is None else "local",
            }
            for notice in notices
        ]

    @classmethod
    def validar_aceite(
        cls,
        notice_ids_aceitos: list[int],
        start_date: date,
        end_date: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Verifica se todos os avisos obrigatórios do período foram aceitos.

        Para cada aviso ativo no período que não estiver em notice_ids_aceitos,
        adiciona ao resultado como aviso pendente de aceite.

        Args:
            notice_ids_aceitos: Lista de IDs de avisos já aceitos pelo cliente.
            start_date: Data de início do período.
            end_date: Data de fim do período.
            source_id: ID do source para filtrar avisos locais. None retorna todos.

        Returns:
            Lista de dicts de avisos não aceitos (mesmo formato de avisos_para_api).
            Lista vazia indica que todos os avisos foram reconhecidos.
        """
        notices = cls.buscar_avisos_periodo(start_date, end_date, source_id)
        aceitos_set = set(notice_ids_aceitos)
        pendentes = []
        for notice in notices:
            if notice.id not in aceitos_set:
                pendentes.append(
                    {
                        "id": notice.id,
                        "formatted_text": cls.formatar_texto(notice),
                        "blocks_source": notice.blocks_source,
                        "blocks_items": notice.blocks_items,
                        "scope": "global" if notice.source_id is None else "local",
                    }
                )
        return pendentes

    @classmethod
    def registrar_aceite(
        cls,
        notice_ids: list[int],
        start_date: date,
        end_date: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Gera registros de aceite para armazenar em Reserva.agreed_notices_json.

        Para cada notice_id fornecido que exista e intersecte o período, cria
        um registro com timestamp de aceite. Esses registros constituem prova
        legal do aceite pelo cliente no momento da reserva.

        Args:
            notice_ids: Lista de IDs de avisos aceitos pelo cliente.
            start_date: Data de início do período da reserva.
            end_date: Data de fim do período da reserva.
            source_id: ID do source para filtrar avisos locais. None retorna todos.

        Returns:
            Lista de dicts prontos para persistir em Reserva.agreed_notices_json.
            Cada dict contém: id, text, agreed_at (ISO 8601 UTC).
        """
        notices = cls.buscar_avisos_periodo(start_date, end_date, source_id)
        notice_map = {n.id: n for n in notices}
        resultado = []
        for nid in notice_ids:
            notice = notice_map.get(nid)
            if notice is not None:
                resultado.append(
                    {
                        "id": notice.id,
                        "text": cls.formatar_texto(notice),
                        "agreed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        return resultado

    @staticmethod
    def tem_bloqueio_source(source_id: int, check_date: date) -> bool:
        """Verifica se há aviso com bloqueio de source ativo na data informada.

        Retorna True se qualquer aviso com blocks_source=True (global ou local
        para o source) cobrir check_date. Para avisos recorrentes, além de
        check_date cair em [start_date, end_date], o dia da semana precisa bater.

        Args:
            source_id: ID do source a verificar.
            check_date: Data de verificação.

        Returns:
            True se houver bloqueio de source; False caso contrário.
        """
        count = (
            db.session.query(ResourceNotice)
            .filter(
                ResourceNotice.deleted_at.is_(None),
                ResourceNotice.blocks_source.is_(True),
                ResourceNotice.start_date <= check_date,
                ResourceNotice.end_date >= check_date,
                or_(
                    ResourceNotice.recurring_weekday.is_(None),
                    ResourceNotice.recurring_weekday == check_date.weekday(),
                ),
                or_(
                    ResourceNotice.source_id.is_(None),
                    ResourceNotice.source_id == source_id,
                ),
            )
            .count()
        )
        return count > 0

    @staticmethod
    def tem_bloqueio_items(
        source_id: int,
        check_date: date,
        item_id: int | None = None,
    ) -> bool:
        """Verifica se há aviso com bloqueio de itens ativo na data informada.

        Quando item_id é fornecido, verifica se o item específico está bloqueado:
          - Aviso com blocked_item_ids=NULL bloqueia TODOS os itens.
          - Aviso com blocked_item_ids=[...] bloqueia apenas os IDs listados.
        Quando item_id não é fornecido, retorna True se qualquer bloqueio existir.
        Para avisos recorrentes, o dia da semana de check_date precisa bater.

        Args:
            source_id: ID do source a verificar.
            check_date: Data de verificação.
            item_id: ID do item específico. None = qualquer bloqueio de itens.

        Returns:
            True se houver bloqueio de itens aplicável; False caso contrário.
        """
        notices = (
            db.session.query(ResourceNotice)
            .filter(
                ResourceNotice.deleted_at.is_(None),
                ResourceNotice.blocks_items.is_(True),
                ResourceNotice.start_date <= check_date,
                ResourceNotice.end_date >= check_date,
                or_(
                    ResourceNotice.recurring_weekday.is_(None),
                    ResourceNotice.recurring_weekday == check_date.weekday(),
                ),
                or_(
                    ResourceNotice.source_id.is_(None),
                    ResourceNotice.source_id == source_id,
                ),
            )
            .all()
        )

        for notice in notices:
            if notice.blocked_item_ids is None:
                # bloqueia todos os itens
                return True
            if item_id is not None and item_id in notice.blocked_item_ids:
                return True
        return False

    # ------------------------------------------------------------------
    # Mutações — CRUD com auditoria (Auditoria obrigatória)
    # ------------------------------------------------------------------

    @staticmethod
    def criar(
        event_name: str,
        start_date: date,
        end_date: date,
        blocks_source: bool = False,
        blocks_items: bool = False,
        blocked_item_ids: list[int] | None = None,
        source_id: int | None = None,
        recurring_weekday: int | None = None,
        ator_id: int | None = None,
    ) -> ResourceNotice:
        """Cria um novo aviso de recurso e registra no log de auditoria.

        Args:
            event_name: Nome do evento ou motivo do aviso.
            start_date: Data de início do aviso.
            end_date: Data de fim do aviso (deve ser >= start_date).
            blocks_source: Se True, bloqueia toda a reserva do source no período.
            blocks_items: Se True, torna itens indisponíveis no picker.
            blocked_item_ids: IDs dos itens específicos a bloquear. None = bloqueia todos.
            source_id: ID do source afetado. None = aviso global.
            recurring_weekday: 0=segunda ... 6=domingo. Quando informado, o aviso
                cobre só as ocorrências desse dia da semana dentro de
                [start_date, end_date], não o intervalo inteiro. None = intervalo
                contínuo (comportamento padrão).
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            Instância de ResourceNotice persistida.

        Raises:
            ValueError: Se event_name estiver vazio, end_date < start_date, ou
                        recurring_weekday fora do intervalo 0–6.
        """
        BaseService._require(event_name, "event_name")
        if end_date < start_date:
            raise ValueError("end_date não pode ser anterior a start_date.")
        if recurring_weekday is not None and not (0 <= recurring_weekday <= 6):
            raise ValueError("recurring_weekday deve estar entre 0 (segunda) e 6 (domingo).")

        notice = ResourceNotice(
            event_name=event_name.strip(),
            start_date=start_date,
            end_date=end_date,
            recurring_weekday=recurring_weekday,
            blocks_source=blocks_source,
            blocks_items=blocks_items,
            blocked_item_ids=blocked_item_ids if blocks_items else None,
            source_id=source_id,
            criado_em=datetime.now(timezone.utc),
        )
        db.session.add(notice)
        db.session.flush()

        AuditService.log_create(
            module=_AUDIT_MODULE,
            entity_id=notice.id,
            new_state=BaseService._snapshot(notice, _SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return notice

    @staticmethod
    def atualizar(
        notice_id: int,
        event_name: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        blocks_source: bool | None = None,
        blocks_items: bool | None = None,
        blocked_item_ids: list[int] | None = None,
        clear_blocked_items: bool = False,
        source_id: int | None = None,
        clear_source: bool = False,
        recurring_weekday: int | None = None,
        clear_recurring_weekday: bool = False,
        ator_id: int | None = None,
    ) -> ResourceNotice:
        """Atualiza um aviso existente e registra no log de auditoria.

        Apenas os campos fornecidos (não None) são atualizados.
        Para limpar o source_id (tornar global), use clear_source=True.
        Para limpar blocked_item_ids (bloquear todos), use clear_blocked_items=True.
        Para remover a recorrência (voltar a intervalo contínuo), use
        clear_recurring_weekday=True.

        Args:
            notice_id: PK do aviso a atualizar.
            event_name: Novo nome do evento (opcional).
            start_date: Nova data de início (opcional).
            end_date: Nova data de fim (opcional).
            blocks_source: Novo valor do flag de bloqueio de source (opcional).
            blocks_items: Novo valor do flag de bloqueio de itens (opcional).
            blocked_item_ids: IDs dos itens específicos a bloquear. None mantém o atual.
            clear_blocked_items: Se True, define blocked_item_ids como NULL (bloqueia todos).
            source_id: Novo source_id (opcional).
            clear_source: Se True, define source_id como NULL (aviso global).
            recurring_weekday: 0=segunda ... 6=domingo (opcional). None mantém o atual.
            clear_recurring_weekday: Se True, define recurring_weekday como NULL.
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            Instância de ResourceNotice atualizada.

        Raises:
            ValueError: Se o aviso não for encontrado, event_name vazio,
                        end_date < start_date, ou recurring_weekday fora de 0–6.
        """
        notice = db.session.get(ResourceNotice, notice_id)
        if not notice or notice.deleted_at is not None:
            raise ValueError(f"Aviso {notice_id} não encontrado.")

        snapshot_antes = BaseService._snapshot(notice, _SNAPSHOT_FIELDS)

        if event_name is not None:
            BaseService._require(event_name, "event_name")
            notice.event_name = event_name.strip()
        if start_date is not None:
            notice.start_date = start_date
        if end_date is not None:
            notice.end_date = end_date
        if blocks_source is not None:
            notice.blocks_source = blocks_source
        if blocks_items is not None:
            notice.blocks_items = blocks_items
            if not blocks_items:
                notice.blocked_item_ids = None
        if clear_blocked_items:
            notice.blocked_item_ids = None
        elif blocked_item_ids is not None:
            notice.blocked_item_ids = blocked_item_ids
        if clear_source:
            notice.source_id = None
        elif source_id is not None:
            notice.source_id = source_id
        if clear_recurring_weekday:
            notice.recurring_weekday = None
        elif recurring_weekday is not None:
            if not (0 <= recurring_weekday <= 6):
                raise ValueError("recurring_weekday deve estar entre 0 (segunda) e 6 (domingo).")
            notice.recurring_weekday = recurring_weekday

        if notice.end_date < notice.start_date:
            raise ValueError("end_date não pode ser anterior a start_date.")

        db.session.flush()

        AuditService.log_update(
            module=_AUDIT_MODULE,
            entity_id=notice.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(notice, _SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return notice

    @staticmethod
    def deletar(notice_id: int, ator_id: int | None = None) -> None:
        """Remove um aviso via soft-delete e registra no log de auditoria.

        Args:
            notice_id: PK do aviso a remover.
            ator_id: ID do funcionário que realiza a ação.

        Raises:
            ValueError: Se o aviso não for encontrado.
        """
        notice = db.session.get(ResourceNotice, notice_id)
        if not notice or notice.deleted_at is not None:
            raise ValueError(f"Aviso {notice_id} não encontrado.")

        snapshot = BaseService._snapshot(notice, _SNAPSHOT_FIELDS)
        notice.deleted_at = datetime.now(timezone.utc)
        db.session.flush()

        AuditService.log_delete(
            module=_AUDIT_MODULE,
            entity_id=notice_id,
            previous_state=snapshot,
            actor_id=ator_id,
        )
        db.session.commit()

    @staticmethod
    def listar_todos(source_id: int | None = None) -> list[ResourceNotice]:
        """Retorna todos os avisos ativos, opcionalmente filtrados por source.

        Args:
            source_id: Se fornecido, retorna apenas avisos deste source + globais.

        Returns:
            Lista de ResourceNotice ordenada por start_date descendente.
        """
        query = db.session.query(ResourceNotice).filter(
            ResourceNotice.deleted_at.is_(None)
        )
        if source_id is not None:
            query = query.filter(
                or_(
                    ResourceNotice.source_id.is_(None),
                    ResourceNotice.source_id == source_id,
                )
            )
        return query.order_by(ResourceNotice.start_date.desc()).all()

    @staticmethod
    def avisos_ativos_hoje(source_id: int) -> list[ResourceNotice]:
        """Retorna todos os avisos ativos para o source que cobrem a data de hoje.

        Usado pelo mapa de ativos para sinalizar restrições vigentes no dia atual.

        Args:
            source_id: ID do source a consultar.

        Returns:
            Lista de ResourceNotice com deleted_at IS NULL que cobrem date.today(),
            globais ou locais para o source especificado.
        """
        today = date.today()
        return (
            db.session.query(ResourceNotice)
            .filter(
                ResourceNotice.deleted_at.is_(None),
                ResourceNotice.start_date <= today,
                ResourceNotice.end_date >= today,
                or_(
                    ResourceNotice.recurring_weekday.is_(None),
                    ResourceNotice.recurring_weekday == today.weekday(),
                ),
                or_(
                    ResourceNotice.source_id.is_(None),
                    ResourceNotice.source_id == source_id,
                ),
            )
            .order_by(ResourceNotice.start_date.asc())
            .all()
        )
