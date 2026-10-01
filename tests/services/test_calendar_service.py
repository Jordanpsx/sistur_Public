# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes do CalendarService — consultas de feriados, alta temporada e CRUD com auditoria.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.models import AuditAction, AuditLog
from app.models.calendario import GlobalEvent, TipoEvento
from app.services.calendar_service import CalendarService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _criar_evento(db, **kwargs) -> GlobalEvent:
    """Cria e persiste um GlobalEvent com valores padrão."""
    defaults = {
        "titulo": "Natal",
        "data_evento": date(2026, 12, 25),
        "tipo": TipoEvento.NATIONAL_HOLIDAY,
        "afeta_precificacao": False,
        "afeta_folha": True,
        "recorrente_anual": True,
    }
    defaults.update(kwargs)
    evento = GlobalEvent(**defaults)
    db.session.add(evento)
    db.session.commit()
    return evento


# ---------------------------------------------------------------------------
# eh_feriado
# ---------------------------------------------------------------------------

class TestEhFeriado:

    def test_data_exata_retorna_true(self, app, db):
        """Feriado com data exata deve ser encontrado."""
        with app.app_context():
            _criar_evento(db, data_evento=date(2026, 9, 7), recorrente_anual=False,
                          titulo="Independência do Brasil")
            assert CalendarService.eh_feriado(date(2026, 9, 7)) is True

    def test_data_sem_evento_retorna_false(self, app, db):
        """Data sem evento cadastrado retorna False."""
        with app.app_context():
            assert CalendarService.eh_feriado(date(2026, 3, 15)) is False

    def test_recorrente_anual_match_outro_ano(self, app, db):
        """Evento recorrente cadastrado em 2026 deve casar com 2027 pelo mês+dia."""
        with app.app_context():
            _criar_evento(db, data_evento=date(2026, 12, 25), recorrente_anual=True)
            assert CalendarService.eh_feriado(date(2027, 12, 25)) is True
            assert CalendarService.eh_feriado(date(2028, 12, 25)) is True

    def test_ignora_maintenance(self, app, db):
        """Eventos tipo MAINTENANCE não são considerados feriados."""
        with app.app_context():
            _criar_evento(db, tipo=TipoEvento.MAINTENANCE, titulo="Manutenção",
                          data_evento=date(2026, 6, 15), recorrente_anual=False)
            assert CalendarService.eh_feriado(date(2026, 6, 15)) is False

    def test_ignora_high_season(self, app, db):
        """Eventos tipo HIGH_SEASON não são considerados feriados."""
        with app.app_context():
            _criar_evento(db, tipo=TipoEvento.HIGH_SEASON, titulo="Verão",
                          data_evento=date(2026, 1, 15), recorrente_anual=False)
            assert CalendarService.eh_feriado(date(2026, 1, 15)) is False

    def test_feriado_local_retorna_true(self, app, db):
        """Feriados locais também devem ser reconhecidos."""
        with app.app_context():
            _criar_evento(db, tipo=TipoEvento.LOCAL_HOLIDAY, titulo="Aniversário da Cidade",
                          data_evento=date(2026, 4, 10), recorrente_anual=False)
            assert CalendarService.eh_feriado(date(2026, 4, 10)) is True


# ---------------------------------------------------------------------------
# obter_eventos_mes
# ---------------------------------------------------------------------------

class TestObterEventosMes:

    def test_retorna_eventos_do_mes(self, app, db):
        """Deve retornar todos os eventos do mês informado."""
        with app.app_context():
            _criar_evento(db, data_evento=date(2026, 12, 25), titulo="Natal")
            _criar_evento(db, data_evento=date(2026, 12, 31), titulo="Réveillon",
                          recorrente_anual=False)
            _criar_evento(db, data_evento=date(2026, 1, 1), titulo="Ano Novo",
                          recorrente_anual=False)

            eventos = CalendarService.obter_eventos_mes(2026, 12)
            titulos = [e.titulo for e in eventos]
            assert "Natal" in titulos
            assert "Réveillon" in titulos
            assert "Ano Novo" not in titulos

    def test_inclui_recorrentes_de_outros_anos(self, app, db):
        """Eventos recorrentes de outros anos devem aparecer no mês correspondente."""
        with app.app_context():
            _criar_evento(db, data_evento=date(2020, 12, 25), titulo="Natal",
                          recorrente_anual=True)

            eventos = CalendarService.obter_eventos_mes(2030, 12)
            assert len(eventos) == 1
            assert eventos[0].titulo == "Natal"


# ---------------------------------------------------------------------------
# eh_alta_temporada
# ---------------------------------------------------------------------------

class TestEhAltaTemporada:

    def test_alta_temporada_no_intervalo(self, app, db):
        """HIGH_SEASON dentro do intervalo retorna True."""
        with app.app_context():
            _criar_evento(db, tipo=TipoEvento.HIGH_SEASON, titulo="Verão",
                          data_evento=date(2026, 1, 15), recorrente_anual=False)
            assert CalendarService.eh_alta_temporada(date(2026, 1, 1), date(2026, 1, 31)) is True

    def test_fora_do_intervalo(self, app, db):
        """HIGH_SEASON fora do intervalo retorna False."""
        with app.app_context():
            _criar_evento(db, tipo=TipoEvento.HIGH_SEASON, titulo="Verão",
                          data_evento=date(2026, 1, 15), recorrente_anual=False)
            assert CalendarService.eh_alta_temporada(date(2026, 6, 1), date(2026, 6, 30)) is False


# ---------------------------------------------------------------------------
# CRUD — criar
# ---------------------------------------------------------------------------

class TestCriar:

    def test_criar_persiste_evento(self, app, db):
        """Criação persiste o evento no banco."""
        with app.app_context():
            evento = CalendarService.criar(
                titulo="Carnaval",
                data_evento=date(2026, 2, 17),
                tipo=TipoEvento.NATIONAL_HOLIDAY,
                afeta_folha=True,
                ator_id=None,
            )
            assert evento.id is not None
            assert evento.titulo == "Carnaval"

    def test_criar_gera_audit_log(self, app, db):
        """Criação deve gerar exatamente um AuditLog com action=create."""
        with app.app_context():
            evento = CalendarService.criar(
                titulo="Tiradentes",
                data_evento=date(2026, 4, 21),
                tipo=TipoEvento.NATIONAL_HOLIDAY,
                ator_id=None,
            )
            count = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.create, module="calendario", entity_id=evento.id)
                .count()
            )
            assert count == 1

    def test_criar_titulo_vazio_levanta_erro(self, app, db):
        """Título vazio deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="titulo"):
                CalendarService.criar(
                    titulo="",
                    data_evento=date(2026, 1, 1),
                    tipo=TipoEvento.NATIONAL_HOLIDAY,
                )


# ---------------------------------------------------------------------------
# CRUD — atualizar
# ---------------------------------------------------------------------------

class TestAtualizar:

    def test_atualizar_gera_audit_log(self, app, db):
        """Atualização deve gerar AuditLog com previous_state e new_state."""
        with app.app_context():
            evento = CalendarService.criar(
                titulo="Natal",
                data_evento=date(2026, 12, 25),
                tipo=TipoEvento.NATIONAL_HOLIDAY,
                ator_id=None,
            )
            CalendarService.atualizar(
                event_id=evento.id,
                titulo="Natal - Atualizado",
                ator_id=None,
            )
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, module="calendario", entity_id=evento.id)
                .first()
            )
            assert log is not None
            assert log.previous_state["titulo"] == "Natal"
            assert log.new_state["titulo"] == "Natal - Atualizado"

    def test_atualizar_evento_inexistente_levanta_erro(self, app, db):
        """Atualizar ID inexistente deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                CalendarService.atualizar(event_id=9999, titulo="X")


# ---------------------------------------------------------------------------
# CRUD — deletar
# ---------------------------------------------------------------------------

class TestDeletar:

    def test_deletar_gera_audit_log(self, app, db):
        """Deleção deve gerar AuditLog com action=delete e previous_state."""
        with app.app_context():
            evento = CalendarService.criar(
                titulo="Evento Temporário",
                data_evento=date(2026, 8, 1),
                tipo=TipoEvento.MAINTENANCE,
                ator_id=None,
            )
            eid = evento.id
            CalendarService.deletar(event_id=eid, ator_id=None)

            assert db.session.get(GlobalEvent, eid) is None

            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.delete, module="calendario", entity_id=eid)
                .first()
            )
            assert log is not None
            assert log.previous_state["titulo"] == "Evento Temporário"

    def test_deletar_evento_inexistente_levanta_erro(self, app, db):
        """Deletar ID inexistente deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                CalendarService.deletar(event_id=9999)


# ---------------------------------------------------------------------------
# Separação de calendários — intervalo (data_fim), origem e efeito em preço
# ---------------------------------------------------------------------------

class TestIntervaloDataFim:

    def test_eh_feriado_cobre_dias_intermediarios(self, app, db):
        """Feriado com data_fim vale para todo o intervalo, não só o primeiro dia."""
        with app.app_context():
            _criar_evento(
                db, titulo="Carnaval", data_evento=date(2027, 2, 8),
                data_fim=date(2027, 2, 10), recorrente_anual=False,
                tipo=TipoEvento.NATIONAL_HOLIDAY,
            )
            assert CalendarService.eh_feriado(date(2027, 2, 8)) is True
            assert CalendarService.eh_feriado(date(2027, 2, 9)) is True
            assert CalendarService.eh_feriado(date(2027, 2, 10)) is True
            assert CalendarService.eh_feriado(date(2027, 2, 11)) is False

    def test_intervalo_recorrente_cruzando_virada_de_ano(self, app, db):
        """Intervalo 20/12→05/01 recorrente cobre dezembro E janeiro.

        Regressão do bug antigo de eh_alta_temporada, que comparava mês inicial
        <= mês <= mês final e falhava quando o período cruzava o ano.
        """
        with app.app_context():
            _criar_evento(
                db, titulo="Alta Temporada Verão", data_evento=date(2026, 12, 20),
                data_fim=date(2027, 1, 5), recorrente_anual=True,
                tipo=TipoEvento.HIGH_SEASON, afeta_precificacao=True,
                afeta_folha=False,
            )
            assert CalendarService.eh_alta_temporada(
                date(2030, 12, 28), date(2030, 12, 28)) is True
            assert CalendarService.eh_alta_temporada(
                date(2031, 1, 3), date(2031, 1, 3)) is True
            assert CalendarService.eh_alta_temporada(
                date(2031, 6, 15), date(2031, 6, 15)) is False

    def test_obter_eventos_mes_inclui_evento_que_atravessa(self, app, db):
        """Evento que começa antes e termina depois do mês deve aparecer nele."""
        with app.app_context():
            _criar_evento(
                db, titulo="Temporada Longa", data_evento=date(2027, 6, 20),
                data_fim=date(2027, 8, 10), recorrente_anual=False,
                tipo=TipoEvento.HIGH_SEASON,
            )
            titulos = [e.titulo for e in CalendarService.obter_eventos_mes(2027, 7)]
            assert "Temporada Longa" in titulos

    def test_data_fim_anterior_ao_inicio_falha(self, app, db):
        """Intervalo invertido é rejeitado com erro em pt-BR."""
        with app.app_context():
            with pytest.raises(ValueError, match="anterior"):
                CalendarService.criar(
                    titulo="Inválido", data_evento=date(2027, 5, 10),
                    data_fim=date(2027, 5, 1), tipo=TipoEvento.HIGH_SEASON,
                    ator_id=1,
                )


class TestDatasQueAfetamPreco:

    def test_flag_manda_e_nao_o_tipo(self, app, db):
        """Alta temporada com afeta_precificacao=True conta como data especial."""
        with app.app_context():
            _criar_evento(
                db, titulo="Alta Temporada Julho", data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31), recorrente_anual=False,
                tipo=TipoEvento.HIGH_SEASON, afeta_precificacao=True,
                afeta_folha=False,
            )
            datas = CalendarService.datas_que_afetam_preco(
                date(2027, 7, 10), date(2027, 7, 13))
            assert datas == {date(2027, 7, 10), date(2027, 7, 11), date(2027, 7, 12)}

    def test_evento_so_de_folha_nao_afeta_preco(self, app, db):
        """Feriado com afeta_precificacao=False fica fora do cálculo de preço."""
        with app.app_context():
            _criar_evento(
                db, titulo="Feriado só de folha", data_evento=date(2027, 5, 1),
                recorrente_anual=False, afeta_precificacao=False, afeta_folha=True,
            )
            assert CalendarService.datas_que_afetam_preco(
                date(2027, 5, 1), date(2027, 5, 2)) == set()

    def test_alta_temporada_nao_vira_feriado_de_folha(self, app, db):
        """Proteção da folha: HIGH_SEASON nunca deve ser lido como feriado.

        eh_feriado alimenta ponto/banco de horas (dia sem expediente, hora em
        dobro). Alta temporada muda preço, não jornada.
        """
        with app.app_context():
            _criar_evento(
                db, titulo="Alta Temporada", data_evento=date(2027, 7, 15),
                recorrente_anual=False, tipo=TipoEvento.HIGH_SEASON,
                afeta_precificacao=True, afeta_folha=False,
            )
            assert CalendarService.eh_feriado(date(2027, 7, 15)) is False

    def test_intervalo_vazio_retorna_conjunto_vazio(self, app, db):
        """check_out <= check_in não tem diária a precificar."""
        with app.app_context():
            assert CalendarService.datas_que_afetam_preco(
                date(2027, 7, 10), date(2027, 7, 10)) == set()


class TestOrigemEPolitica:

    def test_origem_padrao_e_global(self, app, db):
        """Evento criado sem origem explícita pertence ao Calendário Global."""
        with app.app_context():
            ev = CalendarService.criar(
                titulo="Natal", data_evento=date(2027, 12, 25),
                tipo=TipoEvento.NATIONAL_HOLIDAY, ator_id=1,
            )
            assert ev.origem == GlobalEvent.ORIGEM_GLOBAL
            assert CalendarService.editavel_por_reservas(ev) is False

    def test_evento_de_reservas_e_editavel_pelo_modulo(self, app, db):
        with app.app_context():
            ev = CalendarService.criar(
                titulo="Alta Temporada", data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31), tipo=TipoEvento.HIGH_SEASON,
                afeta_precificacao=True, afeta_folha=False,
                origem=GlobalEvent.ORIGEM_RESERVAS, ator_id=1,
            )
            assert CalendarService.editavel_por_reservas(ev) is True

    def test_promocao_a_folha_tira_evento_do_alcance_de_reservas(self, app, db):
        """Marcar afeta_folha entrega a gestão ao Calendário Global."""
        with app.app_context():
            ev = CalendarService.criar(
                titulo="Feriado Municipal", data_evento=date(2027, 8, 15),
                tipo=TipoEvento.LOCAL_HOLIDAY, afeta_precificacao=True,
                origem=GlobalEvent.ORIGEM_RESERVAS, ator_id=1,
            )
            assert CalendarService.editavel_por_reservas(ev) is True

            CalendarService.atualizar(event_id=ev.id, afeta_folha=True, ator_id=1)
            assert CalendarService.editavel_por_reservas(ev) is False

    def test_origem_invalida_falha(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="Origem inválida"):
                CalendarService.criar(
                    titulo="X", data_evento=date(2027, 1, 1),
                    tipo=TipoEvento.HIGH_SEASON, origem="financeiro", ator_id=1,
                )

    def test_auditoria_registra_origem_e_data_fim(self, app, db):
        """Os campos novos entram no snapshot de auditoria."""
        with app.app_context():
            ev = CalendarService.criar(
                titulo="Alta Temporada", data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31), tipo=TipoEvento.HIGH_SEASON,
                origem=GlobalEvent.ORIGEM_RESERVAS, ator_id=1,
            )
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.create, entity_id=ev.id)
                .one()
            )
            assert log.new_state["origem"] == "reservas"
            assert log.new_state["data_fim"] == "2027-07-31"

    def test_limpar_data_fim_volta_para_evento_de_um_dia(self, app, db):
        with app.app_context():
            ev = CalendarService.criar(
                titulo="Temporada", data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31), tipo=TipoEvento.HIGH_SEASON, ator_id=1,
            )
            CalendarService.atualizar(
                event_id=ev.id, limpar_data_fim=True, ator_id=1)
            assert ev.data_fim is None


# ---------------------------------------------------------------------------
# Fachada do módulo de Reservas
# ---------------------------------------------------------------------------

class TestFachadaReservas:

    def test_criar_impoe_flags_de_preco(self, app, db):
        with app.app_context():
            ev = CalendarService.criar_por_reservas(
                titulo="Alta Temporada Julho",
                data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31),
                tipo=TipoEvento.HIGH_SEASON,
                ator_id=1,
            )
            assert ev.afeta_precificacao is True
            assert ev.afeta_folha is False
            assert ev.origem == GlobalEvent.ORIGEM_RESERVAS

    def test_criar_recusa_manutencao(self, app, db):
        """Manutenção é operacional — não é assunto do módulo de Reservas."""
        with app.app_context():
            with pytest.raises(ValueError, match="Calendário Global"):
                CalendarService.criar_por_reservas(
                    titulo="Parada técnica", data_evento=date(2027, 7, 1),
                    tipo=TipoEvento.MAINTENANCE, ator_id=1,
                )

    def test_atualizar_recusa_evento_global(self, app, db):
        with app.app_context():
            ev = CalendarService.criar(
                titulo="Natal", data_evento=date(2027, 12, 25),
                tipo=TipoEvento.NATIONAL_HOLIDAY, ator_id=1,
            )
            with pytest.raises(ValueError, match="Configurações Globais"):
                CalendarService.atualizar_por_reservas(
                    event_id=ev.id, titulo="Sequestrado",
                    data_evento=date(2027, 12, 25),
                    tipo=TipoEvento.NATIONAL_HOLIDAY, ator_id=1,
                )

    def test_deletar_recusa_evento_promovido(self, app, db):
        with app.app_context():
            ev = CalendarService.criar_por_reservas(
                titulo="Feriado Municipal", data_evento=date(2027, 8, 15),
                tipo=TipoEvento.LOCAL_HOLIDAY, ator_id=1,
            )
            CalendarService.atualizar(event_id=ev.id, afeta_folha=True, ator_id=1)

            with pytest.raises(ValueError, match="Configurações Globais"):
                CalendarService.deletar_por_reservas(event_id=ev.id, ator_id=1)

    def test_deletar_evento_inexistente(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                CalendarService.deletar_por_reservas(event_id=99999, ator_id=1)

    def test_deletar_devolve_titulo(self, app, db):
        with app.app_context():
            ev = CalendarService.criar_por_reservas(
                titulo="Alta Temporada", data_evento=date(2027, 7, 1),
                tipo=TipoEvento.HIGH_SEASON, ator_id=1,
            )
            assert CalendarService.deletar_por_reservas(
                event_id=ev.id, ator_id=1) == "Alta Temporada"
