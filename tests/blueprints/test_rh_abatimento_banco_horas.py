# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Fluxo ponta a ponta do abatimento de banco de horas.

Estes testes existem porque a suíte de serviços sozinha não teria pego o defeito
que manteve a função inoperante em produção: os testes de `PontoService` chamavam
o método direto com `"folga"`/`"pagamento"`, enquanto o formulário da folha de
ponto enviava `"ferias"`, `"atestado"` e afins — valores que o enum recusa. A
interseção entre o que a UI oferecia e o que o modelo aceitava era vazia, e
nada na suíte cruzava as duas pontas.

Aqui o caminho testado é o real: POST do formulário → rota → service → banco,
e depois o HTML renderizado, tanto no painel do RH quanto no portal do
colaborador.
"""

from __future__ import annotations

from datetime import date

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.ponto import TimeBankDeduction
from app.models.role import Role


def _criar_admin_e_funcionario(saldo_minutos: int = 1200):
    """Cria um super admin (operador) e um colaborador com saldo no banco."""
    role = Role(nome="super_admin", descricao="Acesso total", is_super_admin=True, ativo=True)
    _db.session.add(role)
    _db.session.flush()

    admin = Funcionario(nome="Admin RH", cpf="52998224725", ativo=True, role_id=role.id)
    colaborador = Funcionario(
        nome="Colaborador Teste",
        cpf="07699220030",
        ativo=True,
        banco_horas_acumulado=saldo_minutos,
    )
    _db.session.add_all([admin, colaborador])
    _db.session.commit()
    return admin, colaborador


def _login(client, funcionario_id: int):
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def _lancar_pagamento(client, colaborador_id: int, **overrides):
    payload = {
        "deduction_type": "pagamento",
        "minutos": "1200",
        "data_registro": "2026-09-30",
        "observacao": "Pagamento das horas extras de setembro/2026",
        "pagamento_valor": "500,00",
        "mes": "9",
        "ano": "2026",
    }
    payload.update(overrides)
    return client.post(
        f"/rh/folha-ponto/{colaborador_id}/deducao",
        data=payload,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Lançamento
# ---------------------------------------------------------------------------

def test_pagamento_lancado_pelo_formulario_chega_ao_banco(app, client, db):
    """O caminho que estava quebrado: formulário → rota → service → linha gravada."""
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    resp = _lancar_pagamento(client, colaborador_id)
    assert resp.status_code == 302

    with app.app_context():
        linha = _db.session.query(TimeBankDeduction).one()
        assert linha.deduction_type.value == "pagamento"
        assert linha.minutos_abatidos == 1200
        # Vírgula decimal do formulário brasileiro precisa chegar como 500.00
        assert float(linha.pagamento_valor) == 500.00
        assert linha.observacao == "Pagamento das horas extras de setembro/2026"
        assert linha.criado_por_id == admin_id
        assert linha.data_registro == date(2026, 9, 30)

        colaborador = _db.session.get(Funcionario, colaborador_id)
        assert colaborador.banco_horas_acumulado == 0


def test_pagamento_sem_valor_nao_grava_nada(app, client, db):
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    resp = _lancar_pagamento(client, colaborador_id, pagamento_valor="")
    assert resp.status_code == 302

    with app.app_context():
        assert _db.session.query(TimeBankDeduction).count() == 0
        colaborador = _db.session.get(Funcionario, colaborador_id)
        assert colaborador.banco_horas_acumulado == 1200


def test_saldo_insuficiente_nao_grava_nada(app, client, db):
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario(saldo_minutos=120)
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    resp = _lancar_pagamento(client, colaborador_id, minutos="1200")
    assert resp.status_code == 302

    with app.app_context():
        assert _db.session.query(TimeBankDeduction).count() == 0
        colaborador = _db.session.get(Funcionario, colaborador_id)
        assert colaborador.banco_horas_acumulado == 120


def test_folga_pelo_formulario_zera_o_dia_sem_informar_minutos(app, client, db):
    """O caso relatado: folga lançada, dia sem batida deixa de contar como falta.

    O modal esconde o campo de minutos na folga, então o POST chega sem ele.
    """
    from app.models.ponto import TimeDay
    from app.services.ponto_service import PontoService

    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario(saldo_minutos=1000)
        admin_id, colaborador_id = admin.id, colaborador.id
        # Dia sem batida: debita a jornada inteira (480 − 10 de tolerância)
        PontoService.processar_dia(colaborador_id, date(2026, 9, 16))
        assert _db.session.get(Funcionario, colaborador_id).banco_horas_acumulado == 530

    _login(client, admin_id)
    resp = client.post(
        f"/rh/folha-ponto/{colaborador_id}/deducao",
        data={
            "deduction_type": "folga",
            "minutos": "",
            "data_registro": "2026-09-16",
            "observacao": "Folga compensatória",
            "mes": "9",
            "ano": "2026",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        dia = _db.session.query(TimeDay).filter_by(
            funcionario_id=colaborador_id, shift_date=date(2026, 9, 16)
        ).one()
        assert dia.saldo_final_minutos == 0
        linha = _db.session.query(TimeBankDeduction).one()
        assert linha.minutos_abatidos == 480
        # 1000 de crédito − 480 da folga, e nada mais
        assert _db.session.get(Funcionario, colaborador_id).banco_horas_acumulado == 520


# ---------------------------------------------------------------------------
# Renderização — o lançamento precisa aparecer para quem lançou e para quem recebeu
# ---------------------------------------------------------------------------

def _lancar_folga_de_16_09(app, client):
    """Colaborador com crédito, dia 16/09 sem batida, folga lançada pelo formulário."""
    from app.services.ponto_service import PontoService

    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario(saldo_minutos=1000)
        admin_id, colaborador_id = admin.id, colaborador.id
        PontoService.processar_dia(colaborador_id, date(2026, 9, 16))

    _login(client, admin_id)
    client.post(
        f"/rh/folha-ponto/{colaborador_id}/deducao",
        data={
            "deduction_type": "folga", "minutos": "", "data_registro": "2026-09-16",
            "observacao": "Folga compensatória", "mes": "9", "ano": "2026",
        },
    )
    return colaborador_id


def test_folha_na_tela_mostra_folga_no_lugar_da_jornada(app, client, db):
    colaborador_id = _lancar_folga_de_16_09(app, client)
    html = client.get(f"/rh/folha-ponto/{colaborador_id}?mes=9&ano=2026").get_data(as_text=True)

    assert 'class="badge-folga"' in html
    assert ">Folga compensatória</span>" in html
    # O desvio do dia já traz a jornada cobrada (480 do cadastro padrão)
    assert "–8h00" in html


def test_pdf_da_folha_mostra_folga_no_lugar_da_jornada(app, client, db):
    """O PDF é o documento assinado: precisa contar a mesma história da tela.

    O HTML do PDF é renderizado a partir dos mesmos dados; testar o template
    evita depender do WeasyPrint para inspecionar o conteúdo.
    """
    from datetime import datetime, timezone

    from flask import render_template

    from app.services.rh_service import RHService

    colaborador_id = _lancar_folga_de_16_09(app, client)
    with app.test_request_context():
        dados = RHService.folha_ponto_mes(colaborador_id, 2026, 9)
        # Mesmos argumentos da rota folha_ponto_pdf
        html = render_template(
            "rh/folha_ponto_pdf.html",
            dados=dados, mes=9, ano=2026,
            empresa_nome="Empresa Teste", empresa_razao_social="Empresa Teste LTDA",
            empresa_cnpj="00.000.000/0001-00", empresa_endereco="Endereço",
            gerado_em=datetime.now(timezone.utc),
        )

    assert '<span class="td-folga">Folga compensatória</span>' in html
    assert "–8h00" in html


def test_lancamento_aparece_na_folha_de_ponto(app, client, db):
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    _lancar_pagamento(client, colaborador_id)

    resp = client.get(f"/rh/folha-ponto/{colaborador_id}?mes=9&ano=2026")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Banco de Horas — Lançamentos" in html
    assert "Pagamento das horas extras de setembro/2026" in html
    assert "R$ 500,00" in html
    assert "Admin RH" in html


def test_colaborador_ve_o_lancamento_no_proprio_portal(app, client, db):
    """Transparência: o saldo não pode cair sem o colaborador ver o motivo e o valor."""
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    _lancar_pagamento(client, colaborador_id)

    # Agora o próprio colaborador abre o portal dele
    _login(client, colaborador_id)
    resp = client.get("/ponto/?mes=9&ano=2026")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Extrato do Banco de Horas" in html
    assert "Pagamento das horas extras de setembro/2026" in html
    assert "R$ 500,00" in html


def test_extrato_do_colaborador_some_quando_nao_ha_lancamento(app, client, db):
    """Sem lançamento no mês, nenhuma seção vazia polui o portal."""
    with app.app_context():
        _, colaborador = _criar_admin_e_funcionario()
        colaborador_id = colaborador.id

    _login(client, colaborador_id)
    resp = client.get("/ponto/?mes=9&ano=2026")
    assert resp.status_code == 200
    assert "Extrato do Banco de Horas" not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Estorno
# ---------------------------------------------------------------------------

def test_estorno_devolve_saldo_e_mantem_as_duas_linhas(app, client, db):
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    _lancar_pagamento(client, colaborador_id)

    with app.app_context():
        original_id = _db.session.query(TimeBankDeduction).one().id

    resp = client.post(
        f"/rh/folha-ponto/{colaborador_id}/deducao/{original_id}/estornar",
        data={"motivo": "Lançado no colaborador errado", "mes": "9", "ano": "2026"},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        linhas = _db.session.query(TimeBankDeduction).order_by(TimeBankDeduction.id).all()
        assert len(linhas) == 2
        assert linhas[1].estorno_de_id == original_id
        assert linhas[1].minutos_abatidos == -1200

        colaborador = _db.session.get(Funcionario, colaborador_id)
        assert colaborador.banco_horas_acumulado == 1200

    # O colaborador vê o par: o lançamento e a correção
    _login(client, colaborador_id)
    html = client.get("/ponto/?mes=9&ano=2026").get_data(as_text=True)
    assert "Pagamento das horas extras de setembro/2026" in html
    assert "Lançado no colaborador errado" in html


def test_estorno_de_lancamento_de_outro_colaborador_e_bloqueado(app, client, db):
    """Guard de IDOR: o id do lançamento precisa bater com o funcionário da URL."""
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        outro = Funcionario(
            nome="Outro Colaborador", cpf="12345678909", ativo=True, banco_horas_acumulado=600,
        )
        _db.session.add(outro)
        _db.session.commit()
        admin_id, colaborador_id, outro_id = admin.id, colaborador.id, outro.id

    _login(client, admin_id)
    _lancar_pagamento(client, colaborador_id)

    with app.app_context():
        original_id = _db.session.query(TimeBankDeduction).one().id

    # POST forjado: lançamento do colaborador, URL apontando para "outro"
    resp = client.post(
        f"/rh/folha-ponto/{outro_id}/deducao/{original_id}/estornar",
        data={"motivo": "Tentativa", "mes": "9", "ano": "2026"},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with app.app_context():
        assert _db.session.query(TimeBankDeduction).count() == 1
        colaborador = _db.session.get(Funcionario, colaborador_id)
        assert colaborador.banco_horas_acumulado == 0


def test_estorno_sem_motivo_nao_devolve_saldo(app, client, db):
    with app.app_context():
        admin, colaborador = _criar_admin_e_funcionario()
        admin_id, colaborador_id = admin.id, colaborador.id

    _login(client, admin_id)
    _lancar_pagamento(client, colaborador_id)

    with app.app_context():
        original_id = _db.session.query(TimeBankDeduction).one().id

    client.post(
        f"/rh/folha-ponto/{colaborador_id}/deducao/{original_id}/estornar",
        data={"motivo": "   ", "mes": "9", "ano": "2026"},
        follow_redirects=False,
    )

    with app.app_context():
        assert _db.session.query(TimeBankDeduction).count() == 1
        colaborador = _db.session.get(Funcionario, colaborador_id)
        assert colaborador.banco_horas_acumulado == 0
