# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes de cobertura para OrganizacaoService.

Cobre Funcao (criar, atualizar, desativar, listar) e Equipe (idem) e
EquipeMembro (adicionar, remover, atualizar função).
Fixtures: `app` + `db` via conftest.py (SQLite in-memory, esquema recriado por teste).
"""

from __future__ import annotations

import pytest

from app.core.models import Area, AuditLog, AuditAction
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.organizacao import Equipe, EquipeMembro, Funcao
from app.services.organizacao_service import OrganizacaoService


def _func(nome: str = "Colaborador", cpf: str = "52998224725") -> Funcionario:
    f = Funcionario(nome=nome, cpf=cpf, ativo=True)
    _db.session.add(f)
    _db.session.flush()
    return f


# ---------------------------------------------------------------------------
# Funcao — CRUD
# ---------------------------------------------------------------------------

class TestCriarFuncao:

    def test_happy_path(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Recepção", ator_id=None)
            assert f.id is not None
            assert f.nome == "Recepção"
            assert f.is_active is True

    def test_audit_log_criado(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Bar", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=f.id, module="funcoes"
            ).one()
            assert log.new_state["nome"] == "Bar"

    def test_nome_vazio_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError):
                OrganizacaoService.criar_funcao("", ator_id=None)

    def test_nome_duplicado_na_area_levanta(self, app, db):
        with app.app_context():
            OrganizacaoService.criar_funcao("Limpeza", area_id=None, ator_id=None)
            with pytest.raises(ValueError, match="Limpeza"):
                OrganizacaoService.criar_funcao("Limpeza", area_id=None, ator_id=None)

    def test_mesmo_nome_areas_distintas_ok(self, app, db):
        with app.app_context():
            f1 = OrganizacaoService.criar_funcao("Garçom", area_id=None, ator_id=None)
            f2 = OrganizacaoService.criar_funcao("Garçom", area_id=1, ator_id=None)
            assert f1.id != f2.id


class TestAtualizarFuncao:

    def test_altera_nome(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Cozinha", ator_id=None)
            f = OrganizacaoService.atualizar_funcao(f.id, "Cozinheiro", ator_id=None)
            assert f.nome == "Cozinheiro"

    def test_audit_update_criado(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Ante", ator_id=None)
            OrganizacaoService.atualizar_funcao(f.id, "Depois", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=f.id, module="funcoes"
            ).one()
            assert log.previous_state["nome"] == "Ante"
            assert log.new_state["nome"] == "Depois"

    def test_nao_encontrada_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.atualizar_funcao(99999, "X", ator_id=None)


class TestDesativarFuncao:

    def test_desativa(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Guarda", ator_id=None)
            f = OrganizacaoService.desativar_funcao(f.id, ator_id=None)
            assert f.is_active is False

    def test_audit_criado(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Vigia", ator_id=None)
            OrganizacaoService.desativar_funcao(f.id, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=f.id, module="funcoes"
            ).one()
            assert log.new_state["is_active"] is False

    def test_ja_inativa_levanta(self, app, db):
        with app.app_context():
            f = OrganizacaoService.criar_funcao("Segurança", ator_id=None)
            OrganizacaoService.desativar_funcao(f.id, ator_id=None)
            with pytest.raises(ValueError, match="já está inativa"):
                OrganizacaoService.desativar_funcao(f.id, ator_id=None)

    def test_nao_encontrada_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.desativar_funcao(99999, ator_id=None)


class TestListarFuncoes:

    def test_retorna_apenas_ativas_por_padrao(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_funcao("Ativa", ator_id=None)
            i = OrganizacaoService.criar_funcao("Inativa", ator_id=None)
            a_id, i_id = a.id, i.id
            OrganizacaoService.desativar_funcao(i_id, ator_id=None)
            ids = [f.id for f in OrganizacaoService.listar_funcoes()]
        assert a_id in ids
        assert i_id not in ids

    def test_retorna_todas_quando_apenas_ativas_false(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_funcao("F1", ator_id=None)
            i = OrganizacaoService.criar_funcao("F2", ator_id=None)
            OrganizacaoService.desativar_funcao(i.id, ator_id=None)
            resultado = OrganizacaoService.listar_funcoes(apenas_ativas=False)
        ids = [f.id for f in resultado]
        assert a.id in ids and i.id in ids

    def test_ordenado_por_nome(self, app, db):
        with app.app_context():
            OrganizacaoService.criar_funcao("Zelador", ator_id=None)
            OrganizacaoService.criar_funcao("Atendente", ator_id=None)
            resultado = OrganizacaoService.listar_funcoes()
        nomes = [f.nome for f in resultado]
        assert nomes == sorted(nomes)


# ---------------------------------------------------------------------------
# Equipe — CRUD
# ---------------------------------------------------------------------------

class TestCriarEquipe:

    def test_happy_path(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Manhã", ator_id=None)
            assert e.id is not None
            assert e.is_active is True

    def test_audit_log_criado(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Tarde", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=e.id, module="equipes"
            ).one()
            assert log.new_state["nome"] == "Tarde"

    def test_nome_vazio_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError):
                OrganizacaoService.criar_equipe("", ator_id=None)

    def test_nome_duplicado_levanta(self, app, db):
        with app.app_context():
            OrganizacaoService.criar_equipe("Noturno", ator_id=None)
            with pytest.raises(ValueError, match="Noturno"):
                OrganizacaoService.criar_equipe("Noturno", ator_id=None)


class TestDesativarEquipe:

    def test_desativa(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Alpha", ator_id=None)
            e = OrganizacaoService.desativar_equipe(e.id, ator_id=None)
            assert e.is_active is False

    def test_ja_inativa_levanta(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Beta", ator_id=None)
            OrganizacaoService.desativar_equipe(e.id, ator_id=None)
            with pytest.raises(ValueError, match="já está inativa"):
                OrganizacaoService.desativar_equipe(e.id, ator_id=None)

    def test_nao_encontrada_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.desativar_equipe(99999, ator_id=None)


# ---------------------------------------------------------------------------
# EquipeMembro — adicionar, remover, atualizar função
# ---------------------------------------------------------------------------

class TestAdicionarMembro:

    def test_happy_path(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Turno A", ator_id=None)
            f = _func()
            m = OrganizacaoService.adicionar_membro(e.id, f.id, ator_id=None)
            assert m.id is not None
            assert m.equipe_id == e.id
            assert m.funcionario_id == f.id
            assert m.funcao_id is None

    def test_com_funcao(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Turno B", ator_id=None)
            fn = OrganizacaoService.criar_funcao("Caixa", ator_id=None)
            f = _func()
            m = OrganizacaoService.adicionar_membro(e.id, f.id, funcao_id=fn.id, ator_id=None)
            assert m.funcao_id == fn.id

    def test_audit_log_criado(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Turno C", ator_id=None)
            f = _func()
            m = OrganizacaoService.adicionar_membro(e.id, f.id, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=m.id, module="equipe_membros"
            ).one()
            assert log.new_state["equipe_id"] == e.id

    def test_duplicado_levanta(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Turno D", ator_id=None)
            f = _func()
            OrganizacaoService.adicionar_membro(e.id, f.id, ator_id=None)
            with pytest.raises(ValueError, match="já é membro"):
                OrganizacaoService.adicionar_membro(e.id, f.id, ator_id=None)

    def test_equipe_nao_encontrada_levanta(self, app, db):
        with app.app_context():
            f = _func()
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.adicionar_membro(99999, f.id, ator_id=None)


class TestRemoverMembro:

    def test_remove(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Remove", ator_id=None)
            f = _func()
            OrganizacaoService.adicionar_membro(e.id, f.id, ator_id=None)
            OrganizacaoService.remover_membro(e.id, f.id, ator_id=None)
            assert _db.session.query(EquipeMembro).filter_by(
                equipe_id=e.id, funcionario_id=f.id
            ).first() is None

    def test_audit_delete_criado(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Remove Audit", ator_id=None)
            f = _func()
            m = OrganizacaoService.adicionar_membro(e.id, f.id, ator_id=None)
            mid = m.id
            OrganizacaoService.remover_membro(e.id, f.id, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.delete, entity_id=mid, module="equipe_membros"
            ).one()
            assert log.previous_state["equipe_id"] == e.id

    def test_nao_membro_levanta(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Sem membro", ator_id=None)
            f = _func()
            with pytest.raises(ValueError, match="não é membro"):
                OrganizacaoService.remover_membro(e.id, f.id, ator_id=None)


class TestAtualizarFuncaoMembro:

    def test_troca_funcao(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Troca", ator_id=None)
            fn1 = OrganizacaoService.criar_funcao("F1", ator_id=None)
            fn2 = OrganizacaoService.criar_funcao("F2", ator_id=None)
            f = _func()
            OrganizacaoService.adicionar_membro(e.id, f.id, funcao_id=fn1.id, ator_id=None)
            m = OrganizacaoService.atualizar_funcao_membro(e.id, f.id, fn2.id, ator_id=None)
            assert m.funcao_id == fn2.id

    def test_remove_funcao_com_none(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Sem Func", ator_id=None)
            fn = OrganizacaoService.criar_funcao("Aux", ator_id=None)
            f = _func()
            OrganizacaoService.adicionar_membro(e.id, f.id, funcao_id=fn.id, ator_id=None)
            m = OrganizacaoService.atualizar_funcao_membro(e.id, f.id, None, ator_id=None)
            assert m.funcao_id is None

    def test_audit_update_criado(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Audit Func", ator_id=None)
            fn1 = OrganizacaoService.criar_funcao("A", ator_id=None)
            fn2 = OrganizacaoService.criar_funcao("B", ator_id=None)
            f = _func()
            m = OrganizacaoService.adicionar_membro(e.id, f.id, funcao_id=fn1.id, ator_id=None)
            OrganizacaoService.atualizar_funcao_membro(e.id, f.id, fn2.id, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=m.id, module="equipe_membros"
            ).one()
            assert log.previous_state["funcao_id"] == fn1.id
            assert log.new_state["funcao_id"] == fn2.id

    def test_nao_membro_levanta(self, app, db):
        with app.app_context():
            e = OrganizacaoService.criar_equipe("Nop", ator_id=None)
            f = _func()
            with pytest.raises(ValueError, match="não é membro"):
                OrganizacaoService.atualizar_funcao_membro(e.id, f.id, None, ator_id=None)


# ---------------------------------------------------------------------------
# Area — CRUD
# ---------------------------------------------------------------------------

class TestCriarArea:

    def test_cria_com_slug_automatico(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Recepção", ator_id=None)
            assert a.id is not None
            assert a.name == "Recepção"
            assert a.slug == "recepcao"
            assert a.is_active is True

    def test_audit_create(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Bar", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=a.id, module="areas"
            ).one()
            assert log.new_state["name"] == "Bar"

    def test_nome_vazio_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError):
                OrganizacaoService.criar_area("", ator_id=None)

    def test_nome_duplicado_levanta_erro(self, app, db):
        with app.app_context():
            OrganizacaoService.criar_area("Limpeza", ator_id=None)
            with pytest.raises(ValueError, match="Já existe"):
                OrganizacaoService.criar_area("Limpeza", ator_id=None)

    def test_slug_com_sufixo_numerico_em_colisao(self, app, db):
        """Se o slug já existe (por inserção direta), incrementa sufixo."""
        with app.app_context():
            area_existente = Area(name="Outro", slug="recepcao", is_active=True)
            _db.session.add(area_existente)
            _db.session.flush()
            a = OrganizacaoService.criar_area("Recepção", ator_id=None)
            assert a.slug == "recepcao-1"

    def test_descricao_opcional(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Salva-vidas", descricao="Área da piscina", ator_id=None)
            assert a.description == "Área da piscina"


class TestDesativarArea:

    def test_desativa_area(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Manutenção", ator_id=None)
            OrganizacaoService.desativar_area(a.id, ator_id=None)
            atualizada = _db.session.get(Area, a.id)
            assert atualizada.is_active is False

    def test_audit_update(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Cozinha", ator_id=None)
            OrganizacaoService.desativar_area(a.id, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=a.id, module="areas"
            ).one()
            assert log.previous_state["is_active"] is True
            assert log.new_state["is_active"] is False

    def test_nao_encontrada_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.desativar_area(99999, ator_id=None)


class TestListarAreas:

    def test_retorna_apenas_ativas_por_padrao(self, app, db):
        with app.app_context():
            a1 = OrganizacaoService.criar_area("Ativa", ator_id=None)
            a2 = OrganizacaoService.criar_area("Inativa", ator_id=None)
            a1_id, a2_id = a1.id, a2.id
            OrganizacaoService.desativar_area(a2_id, ator_id=None)
            ids_ativas = [a.id for a in OrganizacaoService.listar_areas()]
        assert a1_id in ids_ativas
        assert a2_id not in ids_ativas

    def test_retorna_todas_quando_apenas_ativas_false(self, app, db):
        with app.app_context():
            a1 = OrganizacaoService.criar_area("A1", ator_id=None)
            a2 = OrganizacaoService.criar_area("A2", ator_id=None)
            OrganizacaoService.desativar_area(a2.id, ator_id=None)
            todas = OrganizacaoService.listar_areas(apenas_ativas=False)
        ids = [a.id for a in todas]
        assert a1.id in ids
        assert a2.id in ids


class TestAtualizarArea:

    def test_atualiza_nome_mantem_slug(self, app, db):
        """Mudar o nome não altera o slug — slug é identificador estável."""
        with app.app_context():
            a = OrganizacaoService.criar_area("Recepcao", ator_id=None)
            a_id = a.id
            slug_original = a.slug
            OrganizacaoService.atualizar_area(a_id, "Bar e Bebidas", ator_id=None)
            atualizada = _db.session.get(Area, a_id)
            assert atualizada.name == "Bar e Bebidas"
            assert atualizada.slug == slug_original

    def test_audit_update(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Antes", ator_id=None)
            a_id = a.id
            OrganizacaoService.atualizar_area(a_id, "Depois", ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=a_id, module="areas"
            ).one()
            assert log.previous_state["name"] == "Antes"
            assert log.new_state["name"] == "Depois"

    def test_nome_vazio_levanta_erro(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Teste", ator_id=None)
            with pytest.raises(ValueError):
                OrganizacaoService.atualizar_area(a.id, "", ator_id=None)

    def test_nome_duplicado_levanta_erro(self, app, db):
        with app.app_context():
            OrganizacaoService.criar_area("Limpeza", ator_id=None)
            a2 = OrganizacaoService.criar_area("Cozinha", ator_id=None)
            with pytest.raises(ValueError, match="Já existe"):
                OrganizacaoService.atualizar_area(a2.id, "Limpeza", ator_id=None)

    def test_nao_encontrada_levanta_erro(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.atualizar_area(99999, "X", ator_id=None)

    def test_mesmo_nome_nao_gera_conflito(self, app, db):
        """Salvar sem alterar o nome não deve acusar duplicidade."""
        with app.app_context():
            a = OrganizacaoService.criar_area("Recepção", ator_id=None)
            a_id = a.id
            OrganizacaoService.atualizar_area(a_id, "Recepção", descricao="Nova desc", ator_id=None)
            atualizada = _db.session.get(Area, a_id)
            assert atualizada.description == "Nova desc"


# ---------------------------------------------------------------------------
# Equipes do dia (2.9k) — read-only
# ---------------------------------------------------------------------------

class TestEquipesDoDia:

    def _setup_equipe(self):
        """Equipe ativa com dois membros ativos; retorna (equipe, f1, f2)."""
        f1 = _func("Ana Silva", "52998224725")
        f2 = _func("Bruno Costa", "11144477735")
        eq = OrganizacaoService.criar_equipe("Limpeza Manhã", ator_id=None)
        OrganizacaoService.adicionar_membro(eq.id, f1.id, ator_id=None)
        OrganizacaoService.adicionar_membro(eq.id, f2.id, ator_id=None)
        return eq, f1, f2

    def test_membro_de_folga_marcado_com_motivo(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f1, f2 = self._setup_equipe()
            folgas = [{"funcionario": f1, "motivo": "Atestado", "fonte": "ausencia"}]
            resultado = OrganizacaoService.equipes_do_dia(date(2026, 7, 2), folgas=folgas)
            assert len(resultado) == 1
            item = resultado[0]
            assert item["total"] == 2
            assert item["disponiveis"] == 1
            por_id = {m["funcionario"].id: m for m in item["membros"]}
            assert por_id[f1.id]["folga_motivo"] == "Atestado"
            assert por_id[f2.id]["folga_motivo"] is None

    def test_todos_disponiveis_sem_folgas(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f1, f2 = self._setup_equipe()
            resultado = OrganizacaoService.equipes_do_dia(date(2026, 7, 2), folgas=[])
            assert resultado[0]["disponiveis"] == 2
            assert resultado[0]["total"] == 2

    def test_equipe_inativa_excluida(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f1, f2 = self._setup_equipe()
            OrganizacaoService.desativar_equipe(eq.id, ator_id=None)
            resultado = OrganizacaoService.equipes_do_dia(date(2026, 7, 2), folgas=[])
            assert resultado == []

    def test_membro_inativo_excluido(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f1, f2 = self._setup_equipe()
            f2.ativo = False
            _db.session.flush()
            resultado = OrganizacaoService.equipes_do_dia(date(2026, 7, 2), folgas=[])
            assert resultado[0]["total"] == 1
            assert resultado[0]["membros"][0]["funcionario"].id == f1.id

    def test_equipe_sem_membros_aparece_vazia(self, app, db):
        from datetime import date
        with app.app_context():
            OrganizacaoService.criar_equipe("Equipe Nova", ator_id=None)
            resultado = OrganizacaoService.equipes_do_dia(date(2026, 7, 2), folgas=[])
            assert resultado[0]["total"] == 0
            assert resultado[0]["membros"] == []

    def test_calcula_folgas_internamente_quando_none(self, app, db):
        """Sem folgas pré-calculadas, o método consulta RHService sozinho."""
        from datetime import date
        with app.app_context():
            eq, f1, f2 = self._setup_equipe()
            resultado = OrganizacaoService.equipes_do_dia(date(2026, 7, 2))
            assert len(resultado) == 1
            assert resultado[0]["total"] == 2


# ---------------------------------------------------------------------------
# Alertas de disponibilidade (2.9l) — read-only
# ---------------------------------------------------------------------------

class TestAlertasDisponibilidade:

    def test_membro_de_outra_equipe_sinalizado(self, app, db):
        from datetime import date
        with app.app_context():
            f = _func("Carlos Souza", "52998224725")
            eq = OrganizacaoService.criar_equipe("Equipe A", ator_id=None)
            OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
            alertas = OrganizacaoService.alertas_disponibilidade(date(2026, 7, 2))
            assert any("Equipe A" in a for a in alertas.get(f.id, []))

    def test_propria_equipe_nao_sinaliza(self, app, db):
        from datetime import date
        with app.app_context():
            f = _func("Carlos Souza", "52998224725")
            eq = OrganizacaoService.criar_equipe("Equipe A", ator_id=None)
            OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
            alertas = OrganizacaoService.alertas_disponibilidade(date(2026, 7, 2), equipe_id=eq.id)
            assert not any(a.startswith("Já em") for a in alertas.get(f.id, []))

    def test_equipe_inativa_nao_sinaliza(self, app, db):
        from datetime import date
        with app.app_context():
            f = _func("Carlos Souza", "52998224725")
            eq = OrganizacaoService.criar_equipe("Equipe A", ator_id=None)
            OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
            OrganizacaoService.desativar_equipe(eq.id, ator_id=None)
            alertas = OrganizacaoService.alertas_disponibilidade(date(2026, 7, 2))
            assert not any(a.startswith("Já em") for a in alertas.get(f.id, []))

    def test_diarista_sem_diaria_sinalizado(self, app, db):
        from datetime import date
        with app.app_context():
            f = _func("Diarista Um", "52998224725")
            f.tipo_contrato = "DIARISTA"
            f.valor_diaria = None
            _db.session.flush()
            alertas = OrganizacaoService.alertas_disponibilidade(date(2026, 7, 2))
            assert "Diarista sem diária cadastrada" in alertas.get(f.id, [])

    def test_diarista_com_diaria_nao_sinaliza(self, app, db):
        from datetime import date
        from decimal import Decimal
        with app.app_context():
            f = _func("Diarista Dois", "52998224725")
            f.tipo_contrato = "DIARISTA"
            f.valor_diaria = Decimal("150.00")
            _db.session.flush()
            alertas = OrganizacaoService.alertas_disponibilidade(date(2026, 7, 2))
            assert "Diarista sem diária cadastrada" not in alertas.get(f.id, [])

    def test_ausencia_aprovada_sinalizada(self, app, db):
        from datetime import date
        from app.models.avisos import AusenciaJustificada, TipoAusencia
        with app.app_context():
            f = _func("Ausente Hoje", "52998224725")
            dia = date(2026, 7, 2)
            _db.session.add(AusenciaJustificada(
                funcionario_id=f.id, data=dia,
                tipo=TipoAusencia.ATESTADO, aprovado=True,
            ))
            _db.session.flush()
            alertas = OrganizacaoService.alertas_disponibilidade(dia)
            assert any("Atestado" in a for a in alertas.get(f.id, []))


# ---------------------------------------------------------------------------
# Cor da área (color_index)
# ---------------------------------------------------------------------------

class TestCorArea:

    def test_criar_com_cor(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Recepção", cor=3, ator_id=None)
            assert a.color_index == 3
            assert a.cor_indice == 3

    def test_criar_sem_cor_usa_derivada_do_id(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Bar", ator_id=None)
            assert a.color_index is None
            assert a.cor_indice == a.id % 8

    def test_criar_cor_invalida_levanta(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="Cor inválida"):
                OrganizacaoService.criar_area("Cozinha", cor=8, ator_id=None)
            with pytest.raises(ValueError, match="Cor inválida"):
                OrganizacaoService.criar_area("Cozinha", cor="abc", ator_id=None)

    def test_atualizar_cor(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Limpeza", cor=1, ator_id=None)
            a_id = a.id
            OrganizacaoService.atualizar_area(a_id, "Limpeza", cor=5, ator_id=None)
            atualizada = _db.session.get(Area, a_id)
            assert atualizada.color_index == 5

    def test_atualizar_cor_vazia_volta_para_automatica(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Piscina", cor=2, ator_id=None)
            a_id = a.id
            OrganizacaoService.atualizar_area(a_id, "Piscina", cor=None, ator_id=None)
            atualizada = _db.session.get(Area, a_id)
            assert atualizada.color_index is None
            assert atualizada.cor_indice == a_id % 8

    def test_cor_auditada_no_update(self, app, db):
        with app.app_context():
            a = OrganizacaoService.criar_area("Portaria", cor=0, ator_id=None)
            a_id = a.id
            OrganizacaoService.atualizar_area(a_id, "Portaria", cor=7, ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=a_id, module="areas"
            ).one()
            assert log.previous_state["color_index"] == 0
            assert log.new_state["color_index"] == 7

    def test_cor_como_string_do_form_aceita(self, app, db):
        """O form envia '3' (string) — o service normaliza para int."""
        with app.app_context():
            a = OrganizacaoService.criar_area("Estacionamento", cor="3", ator_id=None)
            assert a.color_index == 3


# ---------------------------------------------------------------------------
# EquipeAgendamento (2.1b) — equipes dinâmicas
# ---------------------------------------------------------------------------

class TestAgendarEquipe:

    def _equipe_com_membro(self, nome="Equipe Semana", cpf="52998224725"):
        f = _func("Membro Um", cpf)
        eq = OrganizacaoService.criar_equipe(nome, ator_id=None)
        OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
        return eq, f

    def test_happy_path(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f = self._equipe_com_membro()
            ag = OrganizacaoService.agendar_equipe(
                eq.id, date(2026, 7, 6), date(2026, 7, 12), observacao="Semana cheia", ator_id=None
            )
            assert ag.id is not None
            assert ag.data_inicio == date(2026, 7, 6)
            assert ag.data_fim == date(2026, 7, 12)
            assert ag.observacao == "Semana cheia"
            assert ag.deleted_at is None

    def test_audit_create(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f = self._equipe_com_membro()
            ag = OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.create, entity_id=ag.id, module="equipe_agendamentos"
            ).one()
            assert log.new_state["equipe_id"] == eq.id

    def test_membros_notificados(self, app, db):
        from datetime import date
        from app.models.avisos import Aviso
        with app.app_context():
            eq, f = self._equipe_com_membro()
            OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None)
            avisos = _db.session.query(Aviso).filter_by(destinatario_id=f.id).all()
            assert len(avisos) == 1
            assert "06/07/2026" in avisos[0].mensagem

    def test_periodo_invertido_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f = self._equipe_com_membro()
            with pytest.raises(ValueError, match="início"):
                OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 12), date(2026, 7, 6), ator_id=None)

    def test_sobreposicao_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f = self._equipe_com_membro()
            OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None)
            with pytest.raises(ValueError, match="sobrepõe"):
                OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 10), date(2026, 7, 15), ator_id=None)

    def test_periodos_adjacentes_ok(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f = self._equipe_com_membro()
            OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None)
            ag2 = OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 13), date(2026, 7, 19), ator_id=None)
            assert ag2.id is not None

    def test_equipe_inativa_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            eq, f = self._equipe_com_membro()
            OrganizacaoService.desativar_equipe(eq.id, ator_id=None)
            with pytest.raises(ValueError, match="inativa"):
                OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None)

    def test_equipe_nao_encontrada_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.agendar_equipe(99999, date(2026, 7, 6), date(2026, 7, 12), ator_id=None)


class TestAtualizarCancelarAgendamento:

    def _agendamento(self):
        from datetime import date
        f = _func("Membro Um", "52998224725")
        eq = OrganizacaoService.criar_equipe("Equipe X", ator_id=None)
        OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
        ag = OrganizacaoService.agendar_equipe(
            eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False
        )
        return ag, eq, f

    def test_atualizar_periodo(self, app, db):
        from datetime import date
        from app.models.organizacao import EquipeAgendamento
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.atualizar_agendamento(ag.id, date(2026, 7, 7), date(2026, 7, 13), ator_id=None)
            atualizado = _db.session.get(EquipeAgendamento, ag.id)
            assert atualizado.data_inicio == date(2026, 7, 7)
            assert atualizado.data_fim == date(2026, 7, 13)

    def test_atualizar_audit(self, app, db):
        from datetime import date
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.atualizar_agendamento(ag.id, date(2026, 7, 7), date(2026, 7, 13), ator_id=None)
            log = _db.session.query(AuditLog).filter_by(
                action=AuditAction.update, entity_id=ag.id, module="equipe_agendamentos"
            ).one()
            assert log.previous_state["data_inicio"] != log.new_state["data_inicio"]

    def test_atualizar_sobreposicao_com_outro_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.agendar_equipe(
                eq.id, date(2026, 7, 13), date(2026, 7, 19), ator_id=None, notificar=False
            )
            with pytest.raises(ValueError, match="sobrepõe"):
                OrganizacaoService.atualizar_agendamento(ag.id, date(2026, 7, 6), date(2026, 7, 14), ator_id=None)

    def test_atualizar_proprio_periodo_ok(self, app, db):
        """Encolher/mover o próprio período não conflita consigo mesmo."""
        from datetime import date
        with app.app_context():
            ag, eq, f = self._agendamento()
            atualizado = OrganizacaoService.atualizar_agendamento(
                ag.id, date(2026, 7, 6), date(2026, 7, 10), ator_id=None
            )
            assert atualizado.data_fim == date(2026, 7, 10)

    def test_cancelar(self, app, db):
        from app.models.organizacao import EquipeAgendamento
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.cancelar_agendamento(ag.id, ator_id=None)
            cancelado = _db.session.get(EquipeAgendamento, ag.id)
            assert cancelado.deleted_at is not None

    def test_cancelar_notifica_membros(self, app, db):
        from app.models.avisos import Aviso
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.cancelar_agendamento(ag.id, ator_id=None)
            avisos = _db.session.query(Aviso).filter_by(destinatario_id=f.id).all()
            assert any("cancelado" in a.mensagem for a in avisos)

    def test_cancelar_ja_cancelado_levanta(self, app, db):
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.cancelar_agendamento(ag.id, ator_id=None)
            with pytest.raises(ValueError, match="não encontrado"):
                OrganizacaoService.cancelar_agendamento(ag.id, ator_id=None)

    def test_cancelado_libera_periodo(self, app, db):
        from datetime import date
        with app.app_context():
            ag, eq, f = self._agendamento()
            OrganizacaoService.cancelar_agendamento(ag.id, ator_id=None)
            novo = OrganizacaoService.agendar_equipe(
                eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False
            )
            assert novo.id != ag.id


class TestListarAgendamentos:

    def test_intersecao_com_intervalo(self, app, db):
        from datetime import date
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe A", ator_id=None)
            OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False)
            OrganizacaoService.agendar_equipe(eq.id, date(2026, 8, 1), date(2026, 8, 7), ator_id=None, notificar=False)
            julho = OrganizacaoService.listar_agendamentos(date(2026, 7, 1), date(2026, 7, 31))
            assert len(julho) == 1
            assert julho[0].data_inicio == date(2026, 7, 6)

    def test_cancelados_nao_aparecem(self, app, db):
        from datetime import date
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe B", ator_id=None)
            ag = OrganizacaoService.agendar_equipe(eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False)
            OrganizacaoService.cancelar_agendamento(ag.id, ator_id=None)
            assert OrganizacaoService.listar_agendamentos(date(2026, 7, 1), date(2026, 7, 31)) == []

    def test_filtro_por_equipe(self, app, db):
        from datetime import date
        with app.app_context():
            eq1 = OrganizacaoService.criar_equipe("Equipe C", ator_id=None)
            eq2 = OrganizacaoService.criar_equipe("Equipe D", ator_id=None)
            OrganizacaoService.agendar_equipe(eq1.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False)
            OrganizacaoService.agendar_equipe(eq2.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False)
            so_eq1 = OrganizacaoService.listar_agendamentos(date(2026, 7, 1), date(2026, 7, 31), equipe_id=eq1.id)
            assert len(so_eq1) == 1
            assert so_eq1[0].equipe_id == eq1.id


class TestComposicaoNoPeriodo:

    def test_membro_com_ausencia_marcado(self, app, db):
        from datetime import date
        from app.models.avisos import AusenciaJustificada, TipoAusencia
        with app.app_context():
            f = _func("Ana Silva", "52998224725")
            eq = OrganizacaoService.criar_equipe("Equipe Sem Jornada", ator_id=None)
            OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
            dia_ausente = date(2026, 7, 8)
            _db.session.add(AusenciaJustificada(
                funcionario_id=f.id, data=dia_ausente,
                tipo=TipoAusencia.ATESTADO, aprovado=True,
            ))
            _db.session.flush()
            resultado = OrganizacaoService.composicao_no_periodo(eq.id, date(2026, 7, 6), date(2026, 7, 12))
            assert len(resultado["dias"]) == 7
            membro = resultado["membros"][0]
            assert dia_ausente in membro["indisponibilidades"]
            assert "Atestado" in membro["indisponibilidades"][dia_ausente]

    def test_periodo_acima_do_limite_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe Longa", ator_id=None)
            with pytest.raises(ValueError, match="máximo"):
                OrganizacaoService.composicao_no_periodo(eq.id, date(2026, 1, 1), date(2026, 12, 31))

    def test_equipe_nao_encontrada_levanta(self, app, db):
        from datetime import date
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                OrganizacaoService.composicao_no_periodo(99999, date(2026, 7, 6), date(2026, 7, 12))
