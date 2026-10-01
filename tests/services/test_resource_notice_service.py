# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para o ResourceNoticeService.

Cobre:
  - formatar_texto: data única vs intervalo
  - buscar_avisos_periodo: interseção, soft-delete, escopo global/local
  - avisos_para_api: formato de saída
  - validar_aceite: avisos pendentes vs aceitos
  - registrar_aceite: payload para agreed_notices_json
  - tem_bloqueio_source / tem_bloqueio_items: detecção de bloqueios
  - avisos_ativos_hoje: avisos vigentes na data atual
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.extensions import db as _db
from app.models.reservas import ResourceNotice, ReservaSource
from app.services.resource_notice_service import ResourceNoticeService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def source(db):
    """Cria um ReservaSource para testes de avisos locais."""
    s = ReservaSource(name="Cachoeira Teste")
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def outro_source(db):
    """Segundo source — usado para testar que avisos locais não vazam entre sources."""
    s = ReservaSource(name="Vinhedo Teste")
    db.session.add(s)
    db.session.commit()
    return s


def _notice(db, *, event_name, start_date, end_date, source_id=None,
            blocks_source=False, blocks_items=False, deleted=False,
            recurring_weekday=None):
    """Helper: cria e persiste um ResourceNotice."""
    n = ResourceNotice(
        event_name=event_name,
        start_date=start_date,
        end_date=end_date,
        source_id=source_id,
        blocks_source=blocks_source,
        blocks_items=blocks_items,
        recurring_weekday=recurring_weekday,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
        criado_em=datetime.now(timezone.utc),
    )
    db.session.add(n)
    db.session.commit()
    return n


# ---------------------------------------------------------------------------
# formatar_texto
# ---------------------------------------------------------------------------

class TestFormatarTexto:
    """Testa a geração de texto formatado do aviso."""

    def test_data_unica(self, db):
        """Aviso de um dia deve usar 'em DD/MM/YYYY'."""
        n = _notice(db, event_name="Manutenção", start_date=date(2026, 12, 25),
                    end_date=date(2026, 12, 25))
        assert ResourceNoticeService.formatar_texto(n) == "Manutenção em 25/12/2026"

    def test_intervalo(self, db):
        """Aviso de múltiplos dias deve usar 'de DD/MM/YYYY a DD/MM/YYYY'."""
        n = _notice(db, event_name="Recesso", start_date=date(2026, 12, 25),
                    end_date=date(2026, 12, 31))
        assert ResourceNoticeService.formatar_texto(n) == "Recesso de 25/12/2026 a 31/12/2026"

    def test_nome_com_espacos(self, db):
        """Nome do evento com espaços deve ser preservado literalmente."""
        n = _notice(db, event_name="Feriado Nacional Prolongado",
                    start_date=date(2026, 4, 21), end_date=date(2026, 4, 23))
        texto = ResourceNoticeService.formatar_texto(n)
        assert texto.startswith("Feriado Nacional Prolongado")


# ---------------------------------------------------------------------------
# buscar_avisos_periodo
# ---------------------------------------------------------------------------

class TestBuscarAvisosPeriodo:
    """Testa a consulta de avisos por período com regras de interseção."""

    def test_intersecao_parcial_inicio(self, db):
        """Aviso que começa dentro do período deve ser retornado."""
        _notice(db, event_name="A", start_date=date(2026, 6, 15), end_date=date(2026, 6, 20))
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 10), date(2026, 6, 16)
        )
        assert len(resultado) == 1

    def test_intersecao_parcial_fim(self, db):
        """Aviso que termina dentro do período deve ser retornado."""
        _notice(db, event_name="B", start_date=date(2026, 6, 5), end_date=date(2026, 6, 12))
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 10), date(2026, 6, 20)
        )
        assert len(resultado) == 1

    def test_intersecao_envolvente(self, db):
        """Aviso que engloba todo o período deve ser retornado."""
        _notice(db, event_name="C", start_date=date(2026, 6, 1), end_date=date(2026, 6, 30))
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 10), date(2026, 6, 15)
        )
        assert len(resultado) == 1

    def test_sem_intersecao_antes(self, db):
        """Aviso que termina antes do período não deve ser retornado."""
        _notice(db, event_name="D", start_date=date(2026, 5, 1), end_date=date(2026, 5, 31))
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30)
        )
        assert resultado == []

    def test_sem_intersecao_depois(self, db):
        """Aviso que começa depois do período não deve ser retornado."""
        _notice(db, event_name="E", start_date=date(2026, 7, 1), end_date=date(2026, 7, 15))
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30)
        )
        assert resultado == []

    def test_ignora_soft_deleted(self, db):
        """Avisos com deleted_at preenchido não devem aparecer."""
        _notice(db, event_name="Deletado", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30), deleted=True)
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30)
        )
        assert resultado == []

    def test_escopo_global_retornado_para_source(self, db, source):
        """Aviso global (source_id=None) deve aparecer ao filtrar por source."""
        _notice(db, event_name="Global", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30), source_id=None)
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30), source_id=source.id
        )
        assert len(resultado) == 1

    def test_escopo_local_retornado_para_mesmo_source(self, db, source):
        """Aviso local deve aparecer ao filtrar pelo mesmo source."""
        _notice(db, event_name="Local", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30), source_id=source.id)
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30), source_id=source.id
        )
        assert len(resultado) == 1

    def test_escopo_local_nao_vaza_para_outro_source(self, db, source, outro_source):
        """Aviso local de um source não deve aparecer ao filtrar outro source."""
        _notice(db, event_name="Local Cachoeira", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30), source_id=source.id)
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30), source_id=outro_source.id
        )
        assert resultado == []

    def test_ordenado_por_start_date(self, db):
        """Resultados devem estar ordenados por start_date ascendente."""
        _notice(db, event_name="Z", start_date=date(2026, 6, 20), end_date=date(2026, 6, 25))
        _notice(db, event_name="A", start_date=date(2026, 6, 1),  end_date=date(2026, 6, 5))
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 6, 1), date(2026, 6, 30)
        )
        assert resultado[0].event_name == "A"
        assert resultado[1].event_name == "Z"


# ---------------------------------------------------------------------------
# avisos_para_api
# ---------------------------------------------------------------------------

class TestAvisosParaApi:
    """Testa o formato de saída dos avisos para a API pública."""

    def test_formato_completo(self, db):
        """Cada aviso deve ter id, formatted_text, blocks_source, blocks_items, scope."""
        _notice(db, event_name="Natal", start_date=date(2026, 12, 25),
                end_date=date(2026, 12, 25), blocks_source=True)
        resultado = ResourceNoticeService.avisos_para_api(
            date(2026, 12, 24), date(2026, 12, 26)
        )
        assert len(resultado) == 1
        item = resultado[0]
        assert "id" in item
        assert item["formatted_text"] == "Natal em 25/12/2026"
        assert item["blocks_source"] is True
        assert item["blocks_items"] is False
        assert item["scope"] == "global"

    def test_scope_local(self, db, source):
        """Aviso com source_id deve retornar scope='local'."""
        _notice(db, event_name="Local", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 1), source_id=source.id)
        resultado = ResourceNoticeService.avisos_para_api(
            date(2026, 6, 1), date(2026, 6, 1), source_id=source.id
        )
        assert resultado[0]["scope"] == "local"

    def test_lista_vazia_sem_avisos(self, db):
        """Sem avisos no período deve retornar lista vazia."""
        resultado = ResourceNoticeService.avisos_para_api(
            date(2026, 1, 1), date(2026, 1, 31)
        )
        assert resultado == []


# ---------------------------------------------------------------------------
# validar_aceite
# ---------------------------------------------------------------------------

class TestValidarAceite:
    """Testa a verificação de avisos pendentes de aceite."""

    def test_todos_aceitos_retorna_vazio(self, db):
        """Se todos os avisos foram aceitos, retorna lista vazia."""
        n = _notice(db, event_name="Aviso X", start_date=date(2026, 6, 1),
                    end_date=date(2026, 6, 30))
        pendentes = ResourceNoticeService.validar_aceite(
            [n.id], date(2026, 6, 1), date(2026, 6, 30)
        )
        assert pendentes == []

    def test_aviso_nao_aceito_aparece_como_pendente(self, db):
        """Aviso não incluído na lista de aceitos deve aparecer como pendente."""
        n = _notice(db, event_name="Aviso Y", start_date=date(2026, 6, 1),
                    end_date=date(2026, 6, 30))
        pendentes = ResourceNoticeService.validar_aceite(
            [], date(2026, 6, 1), date(2026, 6, 30)
        )
        assert len(pendentes) == 1
        assert pendentes[0]["id"] == n.id

    def test_aceite_parcial(self, db):
        """Apenas avisos não aceitos devem aparecer como pendentes."""
        n1 = _notice(db, event_name="N1", start_date=date(2026, 6, 1), end_date=date(2026, 6, 10))
        n2 = _notice(db, event_name="N2", start_date=date(2026, 6, 5), end_date=date(2026, 6, 15))
        pendentes = ResourceNoticeService.validar_aceite(
            [n1.id], date(2026, 6, 1), date(2026, 6, 15)
        )
        assert len(pendentes) == 1
        assert pendentes[0]["id"] == n2.id


# ---------------------------------------------------------------------------
# registrar_aceite
# ---------------------------------------------------------------------------

class TestRegistrarAceite:
    """Testa a geração do payload de aceite para agreed_notices_json."""

    def test_payload_completo(self, db):
        """Deve retornar id, text e agreed_at para cada aviso aceito."""
        n = _notice(db, event_name="Regulamento", start_date=date(2026, 6, 1),
                    end_date=date(2026, 6, 30))
        payload = ResourceNoticeService.registrar_aceite(
            [n.id], date(2026, 6, 1), date(2026, 6, 30)
        )
        assert len(payload) == 1
        assert payload[0]["id"] == n.id
        assert "Regulamento" in payload[0]["text"]
        assert "agreed_at" in payload[0]

    def test_id_inexistente_ignorado(self, db):
        """ID de aviso inexistente ou fora do período deve ser silenciosamente ignorado."""
        payload = ResourceNoticeService.registrar_aceite(
            [9999], date(2026, 6, 1), date(2026, 6, 30)
        )
        assert payload == []

    def test_agreed_at_e_iso8601(self, db):
        """O campo agreed_at deve ser uma string ISO 8601 válida."""
        n = _notice(db, event_name="Termos", start_date=date(2026, 6, 1),
                    end_date=date(2026, 6, 30))
        payload = ResourceNoticeService.registrar_aceite(
            [n.id], date(2026, 6, 1), date(2026, 6, 30)
        )
        # datetime.fromisoformat lança ValueError se o formato estiver errado
        datetime.fromisoformat(payload[0]["agreed_at"])


# ---------------------------------------------------------------------------
# tem_bloqueio_source
# ---------------------------------------------------------------------------

class TestTemBloqueioSource:
    """Testa a detecção de bloqueio total de source."""

    def test_sem_aviso_retorna_false(self, db, source):
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 6, 15)) is False

    def test_aviso_blocks_source_retorna_true(self, db, source):
        _notice(db, event_name="Fechamento", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=source.id, blocks_source=True)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 6, 15)) is True

    def test_aviso_global_blocks_source_retorna_true(self, db, source):
        """Aviso global com blocks_source deve bloquear qualquer source."""
        _notice(db, event_name="Feriado Global", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=None, blocks_source=True)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 6, 15)) is True

    def test_aviso_fora_do_dia_retorna_false(self, db, source):
        _notice(db, event_name="Passado", start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 31), source_id=source.id, blocks_source=True)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 6, 15)) is False

    def test_blocks_items_nao_ativa_bloqueio_source(self, db, source):
        """Aviso com blocks_items=True mas blocks_source=False não deve bloquear o source."""
        _notice(db, event_name="Só itens", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=source.id, blocks_items=True)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 6, 15)) is False

    def test_nao_vaza_para_outro_source(self, db, source, outro_source):
        """Bloqueio local de um source não deve afetar outro source."""
        _notice(db, event_name="Local", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=source.id, blocks_source=True)
        assert ResourceNoticeService.tem_bloqueio_source(outro_source.id, date(2026, 6, 15)) is False


# ---------------------------------------------------------------------------
# tem_bloqueio_items
# ---------------------------------------------------------------------------

class TestTemBloqueioItems:
    """Testa a detecção de bloqueio de itens."""

    def test_sem_aviso_retorna_false(self, db, source):
        assert ResourceNoticeService.tem_bloqueio_items(source.id, date(2026, 6, 15)) is False

    def test_aviso_blocks_items_retorna_true(self, db, source):
        _notice(db, event_name="Manutenção", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=source.id, blocks_items=True)
        assert ResourceNoticeService.tem_bloqueio_items(source.id, date(2026, 6, 15)) is True

    def test_aviso_global_blocks_items_retorna_true(self, db, source):
        _notice(db, event_name="Global Items", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=None, blocks_items=True)
        assert ResourceNoticeService.tem_bloqueio_items(source.id, date(2026, 6, 15)) is True

    def test_blocks_source_nao_ativa_bloqueio_items(self, db, source):
        """blocks_source=True sem blocks_items=True não deve bloquear itens."""
        _notice(db, event_name="Só source", start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 20), source_id=source.id, blocks_source=True)
        assert ResourceNoticeService.tem_bloqueio_items(source.id, date(2026, 6, 15)) is False


# ---------------------------------------------------------------------------
# Recorrência semanal (recurring_weekday) — "toda quarta-feira"
# ---------------------------------------------------------------------------

class TestFormatarTextoRecorrente:
    def test_texto_com_dia_de_semana(self, db):
        """quarta-feira usa 'toda' (dia com sufixo -feira, gênero feminino)."""
        n = _notice(db, event_name="Manutenção da piscina",
                    start_date=date(2026, 9, 1), end_date=date(2026, 12, 31),
                    recurring_weekday=2)
        texto = ResourceNoticeService.formatar_texto(n)
        assert texto == "Manutenção da piscina toda quarta-feira, de 01/09/2026 a 31/12/2026"

    def test_texto_sabado_usa_todo(self, db):
        """sábado/domingo usam 'todo' (masculino, sem sufixo -feira)."""
        n = _notice(db, event_name="Limpeza", start_date=date(2026, 9, 1),
                    end_date=date(2026, 12, 31), recurring_weekday=5)
        texto = ResourceNoticeService.formatar_texto(n)
        assert texto == "Limpeza todo sábado, de 01/09/2026 a 31/12/2026"


class TestBuscarAvisosPeriodoRecorrente:
    def test_encontra_quando_janela_contem_o_dia_da_semana(self, db, source):
        """Aviso 'toda quarta-feira' aparece numa consulta que inclui uma quarta."""
        _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        # 01/09 (terça) a 03/09 (quinta) inclui a quarta 02/09.
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 9, 1), date(2026, 9, 3), source_id=source.id
        )
        assert len(resultado) == 1

    def test_nao_encontra_quando_janela_nao_contem_o_dia_da_semana(self, db, source):
        """Reserva de segunda a terça não deve acionar aviso 'toda quarta-feira',
        mesmo que os intervalos brutos se sobreponham — regressão do tipo de bug
        já visto essa semana (bloqueio superagressivo por overlap ingênuo)."""
        _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        # 07/09 (segunda) a 08/09 (terça) — nenhuma quarta no meio.
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 9, 7), date(2026, 9, 8), source_id=source.id
        )
        assert resultado == []

    def test_encontra_segunda_ocorrencia_fora_da_primeira_semana(self, db, source):
        """Consulta que só toca a segunda quarta-feira do intervalo do aviso
        ainda deve encontrar o aviso (a lógica não trava na 1ª ocorrência)."""
        _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2026, 9, 8), date(2026, 9, 9), source_id=source.id
        )
        assert len(resultado) == 1


class TestTemBloqueioSourceRecorrente:
    def test_bloqueia_no_dia_da_semana_certo(self, db, source):
        _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 9, 2)) is True

    def test_nao_bloqueia_em_outro_dia_da_semana(self, db, source):
        """Mesmo dentro do período do aviso, uma quinta-feira não deve ser
        bloqueada por um aviso 'toda quarta-feira'."""
        _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 9, 3)) is False

    def test_nao_bloqueia_fora_do_periodo_mesmo_no_dia_certo(self, db, source):
        """Uma quarta-feira fora de [start_date, end_date] não é bloqueada."""
        _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        # 07/10/2026 é quarta-feira, mas fora do período do aviso (só setembro).
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2026, 10, 7)) is False


class TestCriarEAtualizarRecorrente:
    def test_criar_com_recurring_weekday(self, db):
        n = ResourceNoticeService.criar(
            event_name="Manutenção", start_date=date(2026, 9, 1),
            end_date=date(2026, 12, 31), blocks_source=True, recurring_weekday=2,
        )
        assert n.recurring_weekday == 2

    def test_criar_recurring_weekday_invalido_falha(self, db):
        with pytest.raises(ValueError, match="0.*6|recurring_weekday"):
            ResourceNoticeService.criar(
                event_name="Manutenção", start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31), recurring_weekday=7,
            )

    def test_atualizar_define_recurring_weekday(self, db):
        n = _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                    end_date=date(2026, 12, 31))
        assert n.recurring_weekday is None
        ResourceNoticeService.atualizar(notice_id=n.id, recurring_weekday=3)
        assert n.recurring_weekday == 3

    def test_atualizar_com_clear_remove_recorrencia(self, db):
        """clear_recurring_weekday=True volta o aviso a intervalo contínuo."""
        n = _notice(db, event_name="Manutenção", start_date=date(2026, 9, 1),
                    end_date=date(2026, 12, 31), recurring_weekday=2)
        ResourceNoticeService.atualizar(notice_id=n.id, clear_recurring_weekday=True)
        assert n.recurring_weekday is None


# ---------------------------------------------------------------------------
# Períodos longos — regressão do relato "não consegui criar aviso de mais
# de 1 mês" (não reproduzido manualmente, mas sem cobertura automatizada
# até aqui: nada no arquivo testava período > alguns meses).
# ---------------------------------------------------------------------------

class TestPeriodosLongos:
    def test_criar_aviso_continuo_de_mais_de_um_ano(self, db):
        """criar() não deve ter nenhum limite de duração — aviso de ~14 meses."""
        n = ResourceNoticeService.criar(
            event_name="Obra de longa duração",
            start_date=date(2026, 1, 1),
            end_date=date(2027, 3, 1),  # 424 dias
        )
        assert (n.end_date - n.start_date).days == 424

    def test_criar_aviso_recorrente_de_multiplos_anos(self, db):
        """recurring_weekday também não deve ter limite de duração do período
        em que a recorrência vale — 3 anos, toda quarta-feira."""
        n = ResourceNoticeService.criar(
            event_name="Manutenção plurianual",
            start_date=date(2026, 1, 1),
            end_date=date(2028, 12, 31),  # ~3 anos
            recurring_weekday=2,
        )
        assert (n.end_date - n.start_date).days > 1000
        assert n.recurring_weekday == 2

    def test_buscar_avisos_periodo_encontra_continuo_perto_do_fim_de_periodo_longo(self, db, source):
        """Aviso contínuo de 2 anos deve ser encontrado numa consulta que toca
        só os últimos dias dele, não só o início."""
        _notice(db, event_name="Longo", start_date=date(2026, 1, 1),
                end_date=date(2027, 12, 31), source_id=source.id, blocks_source=True)
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2027, 12, 28), date(2027, 12, 31), source_id=source.id
        )
        assert len(resultado) == 1

    def test_buscar_avisos_periodo_recorrente_encontra_ocorrencia_no_terceiro_ano(self, db, source):
        """Aviso 'toda quarta-feira' de 3 anos deve ser encontrado numa consulta
        de poucos dias bem no fim do período — a lógica não pode depender de
        varrer dia a dia desde o início (custaria caro num período longo)."""
        _notice(db, event_name="Manutenção plurianual", start_date=date(2026, 1, 1),
                end_date=date(2028, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        # 27/12/2028 é quarta-feira, quase 3 anos depois do início do aviso.
        resultado = ResourceNoticeService.buscar_avisos_periodo(
            date(2028, 12, 26), date(2028, 12, 28), source_id=source.id
        )
        assert len(resultado) == 1

    def test_tem_bloqueio_source_recorrente_bloqueia_no_terceiro_ano(self, db, source):
        """tem_bloqueio_source também precisa continuar funcionando bem longe
        do início de um aviso recorrente plurianual."""
        _notice(db, event_name="Manutenção plurianual", start_date=date(2026, 1, 1),
                end_date=date(2028, 12, 31), source_id=source.id,
                blocks_source=True, recurring_weekday=2)
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2028, 12, 27)) is True
        # Quinta-feira na mesma semana, mesmo período longo -> não bloqueia.
        assert ResourceNoticeService.tem_bloqueio_source(source.id, date(2028, 12, 28)) is False
