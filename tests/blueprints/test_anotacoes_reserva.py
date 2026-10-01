# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Anotações da Reserva — a do cliente e a do operador, no mesmo cartão.

O cliente escreve uma observação no formulário do site e ela ia para
``metadata_info.observacoes`` sem aparecer em tela nenhuma. Não se perdia, mas
ninguém a via.

A separação importa: a nota do cliente é renderizada como **texto**, e a do
operador é um ``<textarea>``. O operador acrescenta a dele e não tem como apagar
a do cliente — ela sequer chega ao formulário que ele envia. É por isso que o
teste central aqui verifica onde o texto do cliente **não** está.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.role import Role
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import Reserva, ReservaOrigin, ReservaStatus

OBS = "Chegamos por volta das 10h, somos 4 pessoas"


@pytest.fixture
def reserva(app, db):
    with app.app_context():
        r = Reserva(
            group_id=str(uuid.uuid4()),
            version=1,
            status=ReservaStatus.PENDING,
            origin=ReservaOrigin.WEB,
            customer_name="Maria Aparecida Souza",
            total_amount=Decimal("100.00"),
            check_in_date=date.today() + timedelta(days=10),
            check_out_date=date.today() + timedelta(days=10),
            metadata_info={"observacoes": OBS, "email": "m@e.com"},
            notas_operador="Confirmado por telefone.",
        )
        _db.session.add(r)
        _db.session.commit()
        yield r.id


@pytest.fixture
def operador(app, db, client):
    """Autentica a sessão como super admin — a tela exige permissão de reservas."""
    with app.app_context():
        role = Role(nome="super_admin", is_super_admin=True, ativo=True)
        _db.session.add(role)
        _db.session.flush()
        f = Funcionario(nome="Operador Teste", cpf="52998224725", role_id=role.id, ativo=True)
        _db.session.add(f)
        _db.session.commit()
        fid = f.id
    with client.session_transaction() as sess:
        sess["funcionario_id"] = fid
    return client


def _pagina(operador, reserva_id: int) -> str:
    r = operador.get(f"/reservas/{reserva_id}")
    assert r.status_code == 200, f"detalhe respondeu {r.status_code}"
    return r.get_data(as_text=True)


class TestAnotacoes:
    def test_a_observacao_do_cliente_aparece(self, app, db, operador, reserva):
        assert OBS in _pagina(operador, reserva)

    def test_traz_o_nome_de_quem_escreveu(self, app, db, operador, reserva):
        assert "Maria Aparecida Souza" in _pagina(operador, reserva)

    def test_o_operador_nao_pode_apagar_a_do_cliente(self, app, db, operador, reserva):
        """O texto do cliente não pode estar dentro do textarea do operador.

        É a garantia inteira: o que não é enviado no formulário não pode ser
        sobrescrito por ele.
        """
        html = _pagina(operador, reserva)
        inicio = html.find('id="notas-textarea"')
        dentro = html[inicio : html.find("</textarea>", inicio)]
        assert OBS not in dentro
        # E a do operador continua editável.
        assert "Confirmado por telefone." in dentro

    def test_sem_observacao_o_bloco_nao_aparece(self, app, db, operador):
        """Reserva de balcão não tem nota de cliente; não renderizar caixa vazia."""
        with app.app_context():
            r = Reserva(
                group_id=str(uuid.uuid4()),
                version=1,
                status=ReservaStatus.PENDING,
                origin=ReservaOrigin.BALCAO,
                customer_name="Sem Observacao",
                total_amount=Decimal("50.00"),
                check_in_date=date.today() + timedelta(days=5),
                check_out_date=date.today() + timedelta(days=5),
            )
            _db.session.add(r)
            _db.session.commit()
            rid = r.id
        assert 'class="nota-cliente"' not in _pagina(operador, rid)

    def test_metadata_sem_a_chave_nao_quebra(self, app, db, operador):
        with app.app_context():
            r = Reserva(
                group_id=str(uuid.uuid4()),
                version=1,
                status=ReservaStatus.PENDING,
                origin=ReservaOrigin.WEB,
                customer_name="Outra Chave",
                total_amount=Decimal("50.00"),
                check_in_date=date.today() + timedelta(days=5),
                check_out_date=date.today() + timedelta(days=5),
                metadata_info={"email": "x@y.com"},
            )
            _db.session.add(r)
            _db.session.commit()
            rid = r.id
        assert 'class="nota-cliente"' not in _pagina(operador, rid)

    def test_texto_do_cliente_e_escapado(self, app, db, operador):
        """A observação vem de campo público — não pode virar markup."""
        with app.app_context():
            r = Reserva(
                group_id=str(uuid.uuid4()),
                version=1,
                status=ReservaStatus.PENDING,
                origin=ReservaOrigin.WEB,
                customer_name="Injecao Teste",
                total_amount=Decimal("50.00"),
                check_in_date=date.today() + timedelta(days=5),
                check_out_date=date.today() + timedelta(days=5),
                metadata_info={"observacoes": "<script>alert(1)</script>"},
            )
            _db.session.add(r)
            _db.session.commit()
            rid = r.id
        html = _pagina(operador, rid)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_o_titulo_mudou(self, app, db, operador, reserva):
        html = _pagina(operador, reserva)
        assert "Anotações da Reserva" in html
        assert "Anotações do Operador" not in html


class TestMarcadorNaListagem:
    """O caderninho na fila de reservas.

    Ele só olhava ``notas_operador``, então uma observação escrita pelo cliente
    passava sem sinal nenhum — quem varre a listagem não tinha como saber que
    havia algo escrito ali.
    """

    def _reserva(self, *, cliente=None, operador=None) -> int:
        r = Reserva(
            group_id=str(uuid.uuid4()),
            version=1,
            status=ReservaStatus.PENDING,
            origin=ReservaOrigin.WEB,
            customer_name="Marcador Teste",
            total_amount=Decimal("50.00"),
            check_in_date=date.today() + timedelta(days=3),
            check_out_date=date.today() + timedelta(days=3),
            metadata_info={"observacoes": cliente} if cliente else None,
            notas_operador=operador,
        )
        _db.session.add(r)
        _db.session.commit()
        return r.id

    def test_so_do_cliente_marca(self, app, db):
        """O caso que faltava."""
        with app.app_context():
            r = _db.session.get(Reserva, self._reserva(cliente="Chego às 10h"))
            assert r.tem_anotacao is True

    def test_so_do_operador_marca(self, app, db):
        with app.app_context():
            r = _db.session.get(Reserva, self._reserva(operador="Ligou confirmando"))
            assert r.tem_anotacao is True

    def test_das_duas_marca(self, app, db):
        with app.app_context():
            r = _db.session.get(
                Reserva, self._reserva(cliente="Chego às 10h", operador="Confirmado")
            )
            assert r.tem_anotacao is True

    def test_sem_nenhuma_nao_marca(self, app, db):
        with app.app_context():
            r = _db.session.get(Reserva, self._reserva())
            assert r.tem_anotacao is False

    @pytest.mark.parametrize("vazio", ["", "   ", "\n\t "])
    def test_texto_em_branco_nao_marca(self, app, db, vazio):
        """Um textarea salvo em branco não é anotação."""
        with app.app_context():
            r = _db.session.get(Reserva, self._reserva(cliente=vazio, operador=vazio))
            assert r.tem_anotacao is False
            assert r.observacao_cliente is None

    def test_metadata_com_outras_chaves_nao_marca(self, app, db):
        with app.app_context():
            r = Reserva(
                group_id=str(uuid.uuid4()),
                version=1,
                status=ReservaStatus.PENDING,
                origin=ReservaOrigin.WEB,
                customer_name="Sem Obs",
                total_amount=Decimal("50.00"),
                check_in_date=date.today() + timedelta(days=3),
                check_out_date=date.today() + timedelta(days=3),
                metadata_info={"email": "a@b.com", "telefone": "64999"},
            )
            _db.session.add(r)
            _db.session.commit()
            assert r.tem_anotacao is False

    def test_observacao_nao_textual_e_ignorada(self, app, db):
        """metadata_info é JSON livre; um número ali não pode quebrar a listagem."""
        with app.app_context():
            r = Reserva(
                group_id=str(uuid.uuid4()),
                version=1,
                status=ReservaStatus.PENDING,
                origin=ReservaOrigin.WEB,
                customer_name="Tipo Errado",
                total_amount=Decimal("50.00"),
                check_in_date=date.today() + timedelta(days=3),
                check_out_date=date.today() + timedelta(days=3),
                metadata_info={"observacoes": 42},
            )
            _db.session.add(r)
            _db.session.commit()
            assert r.observacao_cliente is None
            assert r.tem_anotacao is False
