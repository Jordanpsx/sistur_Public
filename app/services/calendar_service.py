# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
CalendarService — fonte única de verdade para feriados e eventos globais.

Nenhum módulo deve hardcodar feriados ou datas especiais. Todos devem
consultar este serviço para verificar se uma data é feriado, alta temporada,
ou manutenção programada.

Regras de arquitetura:
    - Sem importações Flask — puro Python, compatível com Cython.
    - Toda mutação chama AuditService.
    - Recebe actor_id: int | None, nunca um objeto User/Funcionario.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import extract, func, or_

from app.core.audit import AuditService
from app.extensions import db
from app.models.calendario import GlobalEvent, TipoEvento
from app.services.base import BaseService

_HOLIDAY_TYPES = (TipoEvento.NATIONAL_HOLIDAY, TipoEvento.LOCAL_HOLIDAY)

_AUDIT_MODULE = "calendario"
_AUDIT_FIELDS = [
    "id",
    "titulo",
    "data_evento",
    "data_fim",
    "tipo",
    "afeta_precificacao",
    "afeta_folha",
    "recorrente_anual",
    "origem",
]


def _cobre(inicio: date, fim: date | None, dia: date, recorrente: bool) -> bool:
    """Verifica se um evento cobre um dia específico.

    Args:
        inicio: Primeiro dia do evento (`data_evento`).
        fim: Último dia do evento (`data_fim`); None = evento de um dia só.
        dia: Data a testar.
        recorrente: Se o evento se repete todo ano (match por mês+dia).

    Returns:
        True se o dia cai dentro do evento.
    """
    fim = fim or inicio

    if not recorrente:
        return inicio <= dia <= fim

    # Recorrente: compara mês+dia projetando o intervalo no ano do `dia`.
    # (mês, dia) ordena igual à data dentro de um mesmo ano.
    alvo = (dia.month, dia.day)
    ini = (inicio.month, inicio.day)
    end = (fim.month, fim.day)

    if ini <= end:
        return ini <= alvo <= end
    # Intervalo que cruza a virada de ano (ex: 20/12 → 05/01).
    return alvo >= ini or alvo <= end


class CalendarService(BaseService):
    """Consultas e CRUD de eventos globais do calendário.

    Todos os métodos são estáticos — sem estado de instância.
    """

    # ------------------------------------------------------------------
    # Consultas (read-only)
    # ------------------------------------------------------------------

    @staticmethod
    def _candidatos(inicio: date, fim: date, *filtros) -> list[tuple]:
        """Pré-filtra no banco os eventos que podem tocar o intervalo [inicio, fim].

        Faz uma única query e devolve as tuplas para o refinamento em Python,
        que é quem sabe lidar com recorrência anual e virada de ano.

        Args:
            inicio: Primeiro dia do intervalo (inclusive).
            fim: Último dia do intervalo (inclusive).
            *filtros: Cláusulas SQLAlchemy adicionais (tipo, flags…).

        Returns:
            Lista de tuplas (data_evento, data_fim, recorrente_anual).
        """
        fim_evento = func.coalesce(GlobalEvent.data_fim, GlobalEvent.data_evento)
        return (
            db.session.query(
                GlobalEvent.data_evento,
                GlobalEvent.data_fim,
                GlobalEvent.recorrente_anual,
            )
            .filter(
                *filtros,
                or_(
                    GlobalEvent.recorrente_anual.is_(True),
                    db.and_(GlobalEvent.data_evento <= fim, fim_evento >= inicio),
                ),
            )
            .all()
        )

    @staticmethod
    def eh_feriado(data: date) -> bool:
        """Verifica se a data é feriado (nacional ou local).

        Considera eventos de data exata, eventos com intervalo (`data_fim`) e
        eventos recorrentes anuais (match por mês+dia, ignorando o ano).

        Só os tipos NATIONAL_HOLIDAY e LOCAL_HOLIDAY contam aqui. Alta temporada
        NÃO é feriado — este método alimenta folha de ponto e banco de horas
        (dia sem expediente, hora em dobro), e temporada não muda jornada.
        Para efeito em PREÇO use `datas_que_afetam_preco`.

        Args:
            data: Data a verificar.

        Returns:
            True se a data corresponde a um feriado cadastrado.
        """
        rows = CalendarService._candidatos(
            data, data, GlobalEvent.tipo.in_(_HOLIDAY_TYPES)
        )
        return any(_cobre(ini, fim, data, rec) for ini, fim, rec in rows)

    @staticmethod
    def datas_que_afetam_preco(inicio: date, fim: date) -> set[date]:
        """Retorna as datas de [inicio, fim) marcadas como relevantes para preço.

        Fonte única para qualquer módulo que precifique por dia. O gatilho é o
        flag `afeta_precificacao`, NÃO o tipo do evento: feriado nacional, feriado
        local, alta temporada e até manutenção entram na mesma cesta desde que o
        flag esteja ligado. Eventos cadastrados só para efeito de folha
        (`afeta_precificacao=False`) são ignorados.

        Respeita intervalos (`data_fim`) e recorrência anual.

        Args:
            inicio: Primeira data considerada (inclusive).
            fim: Data limite (EXCLUSIVE — no padrão de check-out não cobrado).

        Returns:
            Conjunto de datas dentro de [inicio, fim) com efeito em precificação.
        """
        if fim <= inicio:
            return set()

        ultimo_dia = fim - timedelta(days=1)
        rows = CalendarService._candidatos(
            inicio, ultimo_dia, GlobalEvent.afeta_precificacao.is_(True)
        )
        if not rows:
            return set()

        datas: set[date] = set()
        dia = inicio
        while dia < fim:
            if any(_cobre(ini, f, dia, rec) for ini, f, rec in rows):
                datas.add(dia)
            dia += timedelta(days=1)
        return datas

    @staticmethod
    def obter_eventos_mes(ano: int, mes: int) -> list[GlobalEvent]:
        """Retorna todos os eventos de um determinado mês.

        Inclui eventos com data exata no mês/ano informado e eventos
        recorrentes anuais que caem no mesmo mês.

        Inclui eventos com intervalo (`data_fim`) que apenas ATRAVESSAM o mês,
        mesmo que comecem antes e terminem depois dele.

        Args:
            ano: Ano de referência.
            mes: Mês de referência (1–12).

        Returns:
            Lista de GlobalEvent ordenada por data_evento.
        """
        primeiro = date(ano, mes, 1)
        ultimo = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
        ultimo -= timedelta(days=1)

        fim_evento = func.coalesce(GlobalEvent.data_fim, GlobalEvent.data_evento)
        return (
            db.session.query(GlobalEvent)
            .filter(
                or_(
                    # Eventos cujo intervalo cruza o mês/ano informado
                    db.and_(
                        GlobalEvent.data_evento <= ultimo,
                        fim_evento >= primeiro,
                    ),
                    # Eventos recorrentes que tocam este mês
                    db.and_(
                        GlobalEvent.recorrente_anual.is_(True),
                        or_(
                            extract("month", GlobalEvent.data_evento) == mes,
                            extract("month", fim_evento) == mes,
                            db.and_(
                                extract("month", GlobalEvent.data_evento) <= mes,
                                extract("month", fim_evento) >= mes,
                            ),
                        ),
                    ),
                ),
            )
            .order_by(GlobalEvent.data_evento)
            .all()
        )

    @staticmethod
    def eh_alta_temporada(data_inicio: date, data_fim: date) -> bool:
        """Verifica se há pelo menos um evento de alta temporada no intervalo.

        Respeita eventos com intervalo (`data_fim`) e recorrentes anuais,
        inclusive quando o período cruza a virada de ano.

        Args:
            data_inicio: Data inicial do intervalo (inclusive).
            data_fim: Data final do intervalo (inclusive).

        Returns:
            True se algum evento HIGH_SEASON cai dentro do intervalo.
        """
        if data_fim < data_inicio:
            return False

        rows = CalendarService._candidatos(
            data_inicio, data_fim, GlobalEvent.tipo == TipoEvento.HIGH_SEASON
        )
        if not rows:
            return False

        dia = data_inicio
        while dia <= data_fim:
            if any(_cobre(ini, f, dia, rec) for ini, f, rec in rows):
                return True
            dia += timedelta(days=1)
        return False

    @staticmethod
    def editavel_por_reservas(evento: GlobalEvent) -> bool:
        """Indica se o módulo de Reservas pode editar/remover o evento.

        Reservas mantém apenas o que ele mesmo criou e que continua sendo um
        evento puramente de preço. Assim que o super admin promove o evento a
        evento de folha (`afeta_folha=True`), ele passa a ser assunto do
        Calendário Global e fica somente-leitura no módulo.

        Args:
            evento: Evento a avaliar.

        Returns:
            True se o admin do módulo de Reservas pode alterá-lo.
        """
        return evento.origem == GlobalEvent.ORIGEM_RESERVAS and not evento.afeta_folha

    # ------------------------------------------------------------------
    # Validações
    # ------------------------------------------------------------------

    @staticmethod
    def _validar_origem(origem: str) -> None:
        """Garante que a origem é um dos valores conhecidos.

        Raises:
            ValueError: Se a origem não for 'global' nem 'reservas'.
        """
        validas = (GlobalEvent.ORIGEM_GLOBAL, GlobalEvent.ORIGEM_RESERVAS)
        if origem not in validas:
            raise ValueError(f"Origem inválida: {origem!r}. Use uma de {validas}.")

    @staticmethod
    def _validar_intervalo(data_evento: date, data_fim: date | None) -> None:
        """Garante que o intervalo do evento é coerente.

        Raises:
            ValueError: Se `data_fim` for anterior a `data_evento`.
        """
        if data_fim is not None and data_fim < data_evento:
            raise ValueError(
                "A data final do evento não pode ser anterior à data inicial."
            )

    # ------------------------------------------------------------------
    # Mutações (CRUD com auditoria)
    # ------------------------------------------------------------------

    @staticmethod
    def criar(
        titulo: str,
        data_evento: date,
        tipo: TipoEvento,
        afeta_precificacao: bool = False,
        afeta_folha: bool = False,
        recorrente_anual: bool = False,
        data_fim: date | None = None,
        origem: str = GlobalEvent.ORIGEM_GLOBAL,
        ator_id: int | None = None,
    ) -> GlobalEvent:
        """Cria um novo evento global e registra no log de auditoria.

        Args:
            titulo: Nome descritivo do evento.
            data_evento: Data do evento (primeiro dia, se houver intervalo).
            tipo: Classificação do evento (TipoEvento).
            afeta_precificacao: Se True, módulos de precificação consideram o evento.
            afeta_folha: Se True, módulos de folha/ponto consideram o evento.
            recorrente_anual: Se True, o evento se repete todo ano (mês+dia).
            data_fim: Último dia do intervalo (inclusive). None = evento de um dia.
            origem: 'global' ou 'reservas' — quem cadastrou o evento.
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            Instância de GlobalEvent persistida.

        Raises:
            ValueError: Se o título estiver vazio, a origem for inválida ou
                `data_fim` for anterior a `data_evento`.
        """
        BaseService._require(titulo, "titulo")
        CalendarService._validar_origem(origem)
        CalendarService._validar_intervalo(data_evento, data_fim)

        evento = GlobalEvent(
            titulo=titulo.strip(),
            data_evento=data_evento,
            data_fim=data_fim,
            tipo=tipo,
            afeta_precificacao=afeta_precificacao,
            afeta_folha=afeta_folha,
            recorrente_anual=recorrente_anual,
            origem=origem,
        )
        db.session.add(evento)
        db.session.flush()

        AuditService.log_create(
            module=_AUDIT_MODULE,
            entity_id=evento.id,
            new_state=BaseService._snapshot(evento, _AUDIT_FIELDS),
            actor_id=ator_id,
        )

        db.session.commit()
        return evento

    @staticmethod
    def atualizar(
        event_id: int,
        titulo: str | None = None,
        data_evento: date | None = None,
        tipo: TipoEvento | None = None,
        afeta_precificacao: bool | None = None,
        afeta_folha: bool | None = None,
        recorrente_anual: bool | None = None,
        data_fim: date | None = None,
        limpar_data_fim: bool = False,
        ator_id: int | None = None,
    ) -> GlobalEvent:
        """Atualiza um evento existente e registra no log de auditoria.

        Apenas os campos fornecidos (não None) são atualizados. `origem` é
        imutável — é registro de procedência, não estado editável.

        Args:
            event_id: PK do evento a atualizar.
            titulo: Novo título (opcional).
            data_evento: Nova data inicial (opcional).
            tipo: Novo tipo (opcional).
            afeta_precificacao: Novo valor (opcional).
            afeta_folha: Novo valor (opcional).
            recorrente_anual: Novo valor (opcional).
            data_fim: Novo último dia do intervalo (opcional).
            limpar_data_fim: Se True, zera `data_fim` (volta a evento de um dia).
                Necessário porque `data_fim=None` significa "não alterar".
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            Instância de GlobalEvent atualizada.

        Raises:
            ValueError: Se o evento não for encontrado, título vazio ou
                intervalo inválido.
        """
        evento = db.session.get(GlobalEvent, event_id)
        if not evento:
            raise ValueError(f"Evento {event_id} não encontrado.")

        snapshot_antes = BaseService._snapshot(evento, _AUDIT_FIELDS)

        if titulo is not None:
            BaseService._require(titulo, "titulo")
            evento.titulo = titulo.strip()
        if data_evento is not None:
            evento.data_evento = data_evento
        if limpar_data_fim:
            evento.data_fim = None
        elif data_fim is not None:
            evento.data_fim = data_fim
        if tipo is not None:
            evento.tipo = tipo
        if afeta_precificacao is not None:
            evento.afeta_precificacao = afeta_precificacao
        if afeta_folha is not None:
            evento.afeta_folha = afeta_folha
        if recorrente_anual is not None:
            evento.recorrente_anual = recorrente_anual

        CalendarService._validar_intervalo(evento.data_evento, evento.data_fim)

        db.session.flush()

        AuditService.log_update(
            module=_AUDIT_MODULE,
            entity_id=evento.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(evento, _AUDIT_FIELDS),
            actor_id=ator_id,
        )

        db.session.commit()
        return evento

    # ------------------------------------------------------------------
    # Fachada do módulo de Reservas
    #
    # O admin do módulo escreve no MESMO GlobalEvent do Calendário Global —
    # não existe tabela de feriado por módulo. O que o separa do super admin é
    # a política aplicada aqui, não no blueprint: o módulo cria evento só de
    # preço e só mantém o que ele mesmo criou enquanto não for promovido.
    # ------------------------------------------------------------------

    @staticmethod
    def criar_por_reservas(
        titulo: str,
        data_evento: date,
        tipo: TipoEvento,
        data_fim: date | None = None,
        recorrente_anual: bool = False,
        ator_id: int | None = None,
    ) -> GlobalEvent:
        """Cria uma data especial a partir do módulo de Reservas.

        Os flags são impostos, não recebidos: o evento nasce afetando apenas
        precificação. Promovê-lo a evento de folha é prerrogativa do super
        admin, em `/admin/configuracoes/`.

        Args:
            titulo: Nome do evento.
            data_evento: Primeiro dia.
            tipo: Feriado nacional/local ou alta temporada. `MAINTENANCE` é
                recusado — é evento operacional, exclusivo do calendário global.
            data_fim: Último dia do intervalo (inclusive). None = um dia só.
            recorrente_anual: Se repete todo ano (mês+dia).
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            O GlobalEvent criado.

        Raises:
            ValueError: Título vazio, intervalo inválido ou tipo não permitido.
        """
        if tipo == TipoEvento.MAINTENANCE:
            raise ValueError(
                "Manutenção só pode ser cadastrada no Calendário Global."
            )

        return CalendarService.criar(
            titulo=titulo,
            data_evento=data_evento,
            data_fim=data_fim,
            tipo=tipo,
            afeta_precificacao=True,
            afeta_folha=False,
            recorrente_anual=recorrente_anual,
            origem=GlobalEvent.ORIGEM_RESERVAS,
            ator_id=ator_id,
        )

    @staticmethod
    def _exigir_gestao_de_reservas(event_id: int) -> GlobalEvent:
        """Carrega o evento garantindo que o módulo de Reservas pode alterá-lo.

        Raises:
            ValueError: Se o evento não existir ou pertencer ao Calendário Global.
        """
        evento = db.session.get(GlobalEvent, event_id)
        if not evento:
            raise ValueError(f"Evento {event_id} não encontrado.")
        if not CalendarService.editavel_por_reservas(evento):
            raise ValueError(
                "Este evento é gerenciado nas Configurações Globais e não pode "
                "ser alterado aqui."
            )
        return evento

    @staticmethod
    def atualizar_por_reservas(
        event_id: int,
        titulo: str,
        data_evento: date,
        tipo: TipoEvento,
        data_fim: date | None = None,
        recorrente_anual: bool = False,
        ator_id: int | None = None,
    ) -> GlobalEvent:
        """Edita uma data especial sob gestão do módulo de Reservas.

        Args:
            event_id: PK do GlobalEvent.
            titulo: Novo nome.
            data_evento: Nova data inicial.
            tipo: Novo tipo (MAINTENANCE recusado).
            data_fim: Novo último dia; None volta a evento de um dia.
            recorrente_anual: Se repete todo ano.
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            O GlobalEvent atualizado.

        Raises:
            ValueError: Evento inexistente, fora da gestão do módulo, tipo não
                permitido ou intervalo inválido.
        """
        CalendarService._exigir_gestao_de_reservas(event_id)

        if tipo == TipoEvento.MAINTENANCE:
            raise ValueError(
                "Manutenção só pode ser cadastrada no Calendário Global."
            )

        return CalendarService.atualizar(
            event_id=event_id,
            titulo=titulo,
            data_evento=data_evento,
            data_fim=data_fim,
            limpar_data_fim=data_fim is None,
            tipo=tipo,
            recorrente_anual=recorrente_anual,
            ator_id=ator_id,
        )

    @staticmethod
    def deletar_por_reservas(event_id: int, ator_id: int | None = None) -> str:
        """Remove uma data especial sob gestão do módulo de Reservas.

        Args:
            event_id: PK do GlobalEvent.
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            O título do evento removido, para a mensagem de confirmação.

        Raises:
            ValueError: Evento inexistente ou fora da gestão do módulo.
        """
        evento = CalendarService._exigir_gestao_de_reservas(event_id)
        titulo = evento.titulo
        CalendarService.deletar(event_id=event_id, ator_id=ator_id)
        return titulo

    @staticmethod
    def deletar(event_id: int, ator_id: int | None = None) -> None:
        """Remove um evento global e registra no log de auditoria.

        Args:
            event_id: PK do evento a remover.
            ator_id: ID do funcionário que realiza a ação.

        Raises:
            ValueError: Se o evento não for encontrado.
        """
        evento = db.session.get(GlobalEvent, event_id)
        if not evento:
            raise ValueError(f"Evento {event_id} não encontrado.")

        snapshot = BaseService._snapshot(evento, _AUDIT_FIELDS)

        db.session.delete(evento)
        db.session.commit()

        AuditService.log_delete(
            module=_AUDIT_MODULE,
            entity_id=event_id,
            previous_state=snapshot,
            actor_id=ator_id,
        )
