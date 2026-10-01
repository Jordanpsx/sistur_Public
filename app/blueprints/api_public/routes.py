# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Rotas de API pública — catálogo e criação de reservas pelo site.
"""

from __future__ import annotations

import logging
import os
from datetime import date as _date, datetime, datetime as _datetime, timezone
from functools import wraps
from typing import Any

from flask import jsonify, request

from app.blueprints.api_public import bp
from app.extensions import db
from app.models.funcionario import validar_cpf
from app.models.reservas import ReservaOrigin
from app.services.reserva_service import ReservaService
from app.services.resource_notice_service import ResourceNoticeService

logger = logging.getLogger(__name__)

# Chave de serviço do site público (Next.js). Segue a mesma convenção do
# X-Bot-Api-Key: o chamador é um SERVIDOR nosso, nunca o navegador do visitante,
# então um segredo compartilhado é adequado. Se algum dia o browser precisar
# chamar isto direto, esta chave deixa de servir e vira captcha + rate limit.
_WEB_API_KEY: str = os.environ.get("WEB_API_KEY", "")

# Quantos espaços físicos uma reserva do site pode levar. Regra de negócio da
# Cachoeira: uma churrasqueira por reserva.
#
# Constante e não configuração porque hoje há um venue e uma regra. Quando um
# segundo precisar de número diferente, isto vira uma chave em ReservaConfig
# (que existe e está vazia) — a checagem já está no lugar certo para receber o
# valor de lá.
#
# Vive no servidor, não só no formulário: o site limita a seleção a uma, mas uma
# URL com dois `r<id>` chegaria aqui sem passar pela tela.
_MAX_RECURSOS_POR_RESERVA = 1


def _parse_data_ou_hora(s: str):
    """Aceita "YYYY-MM-DD" ou "YYYY-MM-DDTHH:MM", devolvendo date ou datetime.

    O ``/simular`` já aceitava as duas formas; este endpoint só aceitava data, e
    a diferença não era cosmética. No camping o preço é pró-rata por hora: uma
    estadia das 08:00 às 17:00 do dia seguinte são 33 horas, não 24. Truncar a
    hora aqui fazia a reserva nascer com um total menor que o simulado — ou o
    cliente veria um preço e a portaria cobraria outro, que é exatamente a
    divergência que a anti-fraude existe para barrar.

    Args:
        s: Data ISO, com ou sem hora.

    Returns:
        ``datetime`` quando vier hora; ``date`` caso contrário.

    Raises:
        ValueError: Se o formato não for ISO.
    """
    return _datetime.fromisoformat(s) if "T" in s else _date.fromisoformat(s)


def _hhmm(t) -> str | None:
    """Formata um ``time`` como "HH:MM", ou None se vazio."""
    return t.strftime("%H:%M") if t else None


def _require_web_api_key(f):
    """Valida o header X-Web-Api-Key antes de prosseguir.

    Returns:
        Wrapper que responde 401 se a chave faltar ou divergir, e 503 se a
        variável WEB_API_KEY não estiver configurada no servidor.
    """
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any):
        if not _WEB_API_KEY:
            logger.error("WEB_API_KEY não configurada no servidor.")
            return jsonify({"erro": "Serviço não configurado."}), 503
        if request.headers.get("X-Web-Api-Key", "") != _WEB_API_KEY:
            logger.warning("api_public: chave web inválida. IP=%s", request.remote_addr)
            return jsonify({"erro": "Não autorizado."}), 401
        return f(*args, **kwargs)
    return wrapper


@bp.route("/reservas/catalogo", methods=["GET"])
def catalogo():
    """Retorna o catálogo aninhado de Fontes -> Categorias -> Itens ativos (Source of Truth).

    Returns:
        JSON detalhado do catálogo para frontends Headless.
    """
    sources = ReservaService.listar_fontes()

    # Pre-fetch all items that don't belong strictly to a category or all items. 
    # For now, items are not nested into categories in schema natively, 
    # but the frontend needs all items available for booking.
    # We will return the sources with categories, and a general list of active items.
    
    from app.models.reservas import (
        ReservaGroup,
        ReservaItem,
        ReservaResource,
        ResourceImage,
    )
    from app.extensions import db

    # `visible_to_web` filtra aqui, não no frontend: o que o site oferece é
    # decisão de operação, e cada consumidor desta API teria de repetir a regra
    # se ela ficasse do outro lado.
    active_items = db.session.query(ReservaItem).filter(
        ReservaItem.deleted_at.is_(None),
        ReservaItem.visible_to_web.is_(True),
    ).all()

    # Mapa categoria -> fonte. O formulário público precisa do source_id para
    # chamar /simular, e ele não está no item — só na categoria acima dele.
    # Grupos que têm unidade física. Uma consulta, não uma por item.
    from app.models.reservas import ReservaResource

    grupos_com_recurso = {
        g
        for (g,) in db.session.query(ReservaResource.group_id)
        .filter(ReservaResource.deleted_at.is_(None), ReservaResource.group_id.isnot(None))
        .distinct()
        .all()
    }

    fonte_da_categoria = {
        c.id: s.id for s in sources for c in s.categories if c.deleted_at is None
    }

    # Árvore de grupos + uma foto e uma descrição representativas de cada um.
    #
    # Os grupos já existem e são mantidos pelo operador ("ÁREA A – DIVERSÃO
    # (Permitido som ambiente)" / "ÁREA B – SOSSEGO (Proibido som)"), e essa
    # distinção é o que mais pesa na escolha de uma churrasqueira. Sem expô-los
    # o site teria de inventar categorias próprias, que divergiriam do admin na
    # primeira vez que alguém criasse um grupo novo.
    #
    # A foto e a descrição vêm do primeiro recurso do grupo: são iguais entre os
    # recursos irmãos (todas as "Churrasqueiras grandes (A)" têm a mesma
    # capacidade), então servem para ilustrar o item que consome aquele grupo.
    grupos = (
        db.session.query(ReservaGroup)
        .filter(ReservaGroup.deleted_at.is_(None))
        .order_by(ReservaGroup.sort_order, ReservaGroup.id)
        .all()
    )

    recursos = (
        db.session.query(ReservaResource)
        .filter(
            ReservaResource.deleted_at.is_(None),
            ReservaResource.group_id.isnot(None),
        )
        .order_by(ReservaResource.name)
        .all()
    )
    amostra_do_grupo: dict[int, ReservaResource] = {}
    for r in recursos:
        amostra_do_grupo.setdefault(r.group_id, r)

    fotos = (
        db.session.query(ResourceImage)
        .filter(ResourceImage.deleted_at.is_(None))
        .order_by(ResourceImage.sort_order, ResourceImage.id)
        .all()
    )
    foto_do_recurso: dict[int, str] = {}
    for f in fotos:
        foto_do_recurso.setdefault(f.resource_id, f.image_url)

    return jsonify({
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                # Deixa o site montar a hierarquia sem uma segunda chamada.
                "parent_id": g.parent_id,
                "sort_order": g.sort_order,
                "description": (
                    amostra_do_grupo[g.id].description
                    if g.id in amostra_do_grupo
                    else None
                ),
                "image_url": (
                    foto_do_recurso.get(amostra_do_grupo[g.id].id)
                    if g.id in amostra_do_grupo
                    else None
                ),
            }
            for g in grupos
        ],
        "sources": [
            {
                "id": s.id,
                "name": s.name,
                "is_active": s.is_active,
                "categories": [
                    {
                        "id": c.id,
                        "name": c.name,
                        # Referência estável para a rota do formulário — renomear
                        # a categoria não pode quebrar o link salvo pelo cliente.
                        "slug": c.internal_slug,
                        "description": c.descricao_curta,
                        # Foto da experiência. Nula = o card do site fica só com
                        # texto, como sempre foi.
                        "image_url": c.imagem_url,
                        # Tema do formulário. Nulo = claro, o padrão.
                        "tema": c.tema,
                        # Parametriza o formulário: data única (day use) versus
                        # intervalo com pernoite (camping).
                        "single_day_only": c.single_day_only,
                        "same_day_cutoff_time": (
                            c.same_day_cutoff_time.strftime("%H:%M")
                            if c.same_day_cutoff_time
                            else None
                        ),
                        # Janela de horário da estadia. Vem nulo onde a hora não
                        # entra no preço (day use); preenchido, o formulário
                        # passa a pedir hora de entrada e saída, porque no
                        # camping 08:00→17:00 do dia seguinte custa mais que 24
                        # horas cheias.
                        "check_in_from": _hhmm(c.check_in_from),
                        "check_in_until": _hhmm(c.check_in_until),
                        "check_out_until": _hhmm(c.check_out_until),
                        "min_stay_hours": c.min_stay_hours,
                    }
                    # Ordem estável por id, que é a de criação. Sem isto o
                    # banco decidia, e o site abria com Camping na frente do Day
                    # use — a ordem mudava sem ninguém ter mexido em nada.
                    for c in sorted(s.categories, key=lambda x: x.id)
                    if c.deleted_at is None
                ],
            }
            for s in sources
        ],
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "internal_slug": i.internal_slug,
                # Sem isto o frontend recebe 35 itens soltos e não sabe quais
                # pertencem ao Day use e quais ao Camping.
                "category_id": i.category_id,
                "source_id": fonte_da_categoria.get(i.category_id),
                # Liga o item ao grupo, que carrega o nome de exibição, a foto e
                # a capacidade.
                "group_id": i.group_id,
                # Campo canônico de "isto é uma entrada". Substitui a convenção
                # de procurar `_entrada_` no internal_slug, que era um remendo
                # sobre nomenclatura e quebraria num item mal batizado.
                "is_entry_ticket": i.is_entry_ticket,
                # Se esta tarifa é atendida por unidades físicas (a Churrasqueira
                # A4, e não "uma churrasqueira grande"). O vínculo é pelo grupo:
                # o recurso pertence a um grupo, e o grupo tem uma tarifa.
                #
                # O site precisa saber disto ANTES de haver data escolhida. Sem
                # data ele não consulta disponibilidade, e sem consultar ele
                # oferecia a tarifa como contador — "2 × Churrasqueira Grande
                # (A)" —, que é o nome do preço e um modo de escolher que não
                # existe para churrasqueira.
                "has_units": i.group_id in grupos_com_recurso,
                # Texto curto editável pelo operador. É onde vive a explicação de
                # quem paga meia e quem não paga.
                "description": i.bot_description,
                "price": float(i.price),
                # Overrides por tipo de dia. São as MESMAS colunas editadas em
                # "Tarifas por tipo de dia" no admin; vazio = usa o preço base.
                # A landing page lê estas colunas em vez de simular uma data,
                # porque é exatamente o número que o operador digitou na tela.
                "price_weekday": float(i.price_weekday) if i.price_weekday is not None else None,
                "price_weekend": float(i.price_weekend) if i.price_weekend is not None else None,
                "price_holiday": float(i.price_holiday) if i.price_holiday is not None else None,
                "billing_type": i.billing_type.value,
                "stock_quantity": None,  # Resources are now decoupled
                "requires_deposit": i.requires_deposit,
            }
            for i in active_items
        ]
    }), 200


@bp.route("/reservas/api/public/notices", methods=["GET"])
def notices():
    """Retorna avisos de recursos ativos que intersectam o período informado.

    Query params:
        start_date (str): Data de início no formato YYYY-MM-DD (obrigatório).
        end_date (str): Data de fim no formato YYYY-MM-DD (obrigatório).
        source_id (int): ID do source para filtrar avisos locais (opcional).

    Returns:
        JSON com lista de avisos (HTTP 200) ou mensagem de erro (HTTP 400).
    """
    start_str = request.args.get("start_date", "").strip()
    end_str = request.args.get("end_date", "").strip()
    source_id_str = request.args.get("source_id", "").strip()

    if not start_str or not end_str:
        return jsonify({"erro": "Os parâmetros start_date e end_date são obrigatórios."}), 400

    try:
        start_date = _date.fromisoformat(start_str)
        end_date = _date.fromisoformat(end_str)
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    if start_date > end_date:
        return jsonify({"erro": "start_date não pode ser posterior a end_date."}), 400

    source_id: int | None = None
    if source_id_str:
        try:
            source_id = int(source_id_str)
        except ValueError:
            return jsonify({"erro": "source_id deve ser um número inteiro."}), 400

    notices_data = ResourceNoticeService.avisos_para_api(start_date, end_date, source_id)
    return jsonify({"notices": notices_data}), 200


@bp.route("/reservas/disponibilidade", methods=["GET"])
def disponibilidade_recursos():
    """Recursos físicos de uma categoria, com disponibilidade e tarifa.

    O formulário do site oferece a **churrasqueira**, não a tarifa. O cliente
    escolhe "Churrasqueira A4", vê se ela está livre naquela data e olha as
    fotos daquela churrasqueira — e não de uma representante do grupo.

    O preço vem do grupo: A4 pertence a "Churrasqueiras grandes (A)", cujo item
    de day use custa R$ 120,00. É o mesmo caminho que a atribuição automática
    percorre ao contrário, então site e balcão cobram o mesmo.

    Query params:
        source_id (int): Fonte/venue. Obrigatório.
        category_id (int): Categoria, que decide qual tarifa vale — o mesmo
            grupo tem preços diferentes em day use e camping. Obrigatório.
        check_in (str): YYYY-MM-DD. Obrigatório.
        check_out (str): YYYY-MM-DD. Obrigatório.

    Returns:
        200 com ``{"resources": [...]}``. Cada recurso traz id, nome, descrição,
        todas as fotos, o grupo, a disponibilidade no período e o
        ``item_id``/``price`` da tarifa que o cobre. Recurso sem tarifa na
        categoria é omitido: não há como vendê-lo ali.
    """
    from app.models.reservas import ReservaItem, ResourceImage

    try:
        source_id = int(request.args.get("source_id", ""))
        category_id = int(request.args.get("category_id", ""))
        # Aceita hora e a descarta: disponibilidade é por dia, mas o camping
        # manda instantes (a hora é preço lá) e o mesmo formulário serve as
        # duas modalidades. Recusar o "T" aqui devolvia zero churrasqueiras e
        # o site caía silenciosamente no modo antigo, oferecendo a tarifa em
        # vez da churrasqueira física.
        check_in = _parse_data_ou_hora(request.args.get("check_in", ""))
        check_out = _parse_data_ou_hora(request.args.get("check_out", ""))
        if isinstance(check_in, _datetime):
            check_in = check_in.date()
        if isinstance(check_out, _datetime):
            check_out = check_out.date()
    except (TypeError, ValueError):
        return jsonify({"erro": "Parâmetros inválidos."}), 400

    if check_out < check_in:
        return jsonify({"erro": "check_out não pode ser anterior a check_in."}), 400

    linhas = ReservaService.verificar_disponibilidade_recursos(
        source_id=source_id, check_in=check_in, check_out=check_out
    )

    # Tarifa por grupo, dentro da categoria pedida.
    itens = (
        db.session.query(ReservaItem)
        .filter(
            ReservaItem.category_id == category_id,
            ReservaItem.deleted_at.is_(None),
            ReservaItem.visible_to_web.is_(True),
            ReservaItem.group_id.isnot(None),
        )
        .all()
    )
    tarifa_do_grupo = {i.group_id: i for i in itens}

    ids = [l["resource_id"] for l in linhas]
    fotos: dict[int, list[str]] = {}
    if ids:
        for f in (
            db.session.query(ResourceImage)
            .filter(
                ResourceImage.resource_id.in_(ids),
                ResourceImage.deleted_at.is_(None),
            )
            .order_by(ResourceImage.sort_order, ResourceImage.id)
            .all()
        ):
            fotos.setdefault(f.resource_id, []).append(f.image_url)

    saida = []
    for l in linhas:
        item = tarifa_do_grupo.get(l["group_id"])
        if item is None:
            # Grupo sem tarifa nesta categoria — nada a vender aqui.
            continue
        if item.is_entry_ticket:
            # "Capacidade Day Use" é o pool que as entradas consomem, não um
            # espaço que alguém escolhe. Ele aparece aqui porque é um recurso
            # como qualquer outro; a tarifa é que diz o que ele representa.
            continue
        saida.append({
            "id": l["resource_id"],
            "name": l["name"],
            "description": l["description"],
            "group_id": l["group_id"],
            "group_name": l["group_name"],
            "images": fotos.get(l["resource_id"], []),
            # `available` é None quando o estoque é infinito.
            "is_available": l["is_available"],
            "available": l["available"],
            # Capacidade total, que é coisa diferente de quantas sobraram — e a
            # diferença decide como o site oferece o recurso. Estoque 1 é uma
            # unidade que a pessoa escolhe pelo nome ("Churrasqueira A4", com a
            # foto dela); estoque maior é um pool intercambiável, e ali ninguém
            # quer escolher QUAL barraca pequena, só QUANTAS. Usar `available`
            # para isso confundiria um pool com uma sobrando com uma unidade.
            "stock": l["stock_quantity"],
            "item_id": item.id,
            "item_name": item.name,
            "price": float(item.price),
        })

    return jsonify({"resources": saida}), 200


# ---------------------------------------------------------------------------
# Criação de reserva pelo site público
# ---------------------------------------------------------------------------


@bp.route("/reservas/criar", methods=["POST"])
@_require_web_api_key
def criar_reserva_web():
    """Cria uma reserva PENDING vinda do formulário do site.

    Chamado pelo servidor do Next.js, nunca pelo navegador — os dados pessoais
    trafegam no corpo de um POST servidor-a-servidor e jamais numa query string.

    A validação de preço é a do ``ReservaService.criar``: cada item traz o total
    esperado em ``price_override`` e o servidor recalcula. Divergência acima de
    R$ 0,01 recusa a criação, então uma tabela alterada entre a cotação e o envio
    é detectada aqui e não vira cobrança errada.

    Request body (JSON):
        source_id (int): Fonte/venue.
        category_id (int): Categoria (day use, camping…).
        customer_name (str): Nome completo.
        customer_document (str): CPF. Obrigatório para reserva web.
        email (str): E-mail de contato.
        telefone (str): Telefone/WhatsApp.
        check_in_date (str): YYYY-MM-DD.
        check_out_date (str): YYYY-MM-DD.
        items (list): [{"item_id", "quantity", "price_override"}].
        observacoes (str): Texto livre opcional.

    Returns:
        201 com {"group_id", "reserva_id", "total", "expires_at"};
        400 quando a validação falha (a mensagem é exibível ao cliente);
        404 se a fonte ou categoria não existir.
    """
    dados = request.get_json(silent=True) or {}

    try:
        nome = str(dados.get("customer_name", "")).strip()
        if len(nome.split()) < 2:
            raise ValueError("Informe o nome completo.")

        # CPF é obrigatório na reserva web: é a chave que o balcão usa no
        # check-in e a que o cliente informa para consultar a própria reserva.
        cpf = validar_cpf(str(dados.get("customer_document", "")))

        email = str(dados.get("email", "")).strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            raise ValueError("Informe um e-mail válido.")

        telefone_digitos = "".join(c for c in str(dados.get("telefone", "")) if c.isdigit())
        if not 10 <= len(telefone_digitos) <= 13:
            raise ValueError("Informe um telefone válido com DDD.")

        try:
            check_in = _parse_data_ou_hora(str(dados.get("check_in_date", "")))
            check_out = _parse_data_ou_hora(str(dados.get("check_out_date", "")))
        except ValueError:
            raise ValueError("Datas inválidas.") from None

        itens = dados.get("items")
        if not isinstance(itens, list) or not itens:
            raise ValueError("Selecione ao menos um item.")

        com_recurso = {
            it.get("resource_id")
            for it in itens
            if isinstance(it, dict) and it.get("resource_id") is not None
        }
        if len(com_recurso) > _MAX_RECURSOS_POR_RESERVA:
            raise ValueError(
                "É permitida apenas uma churrasqueira por reserva. "
                "Faça uma reserva separada para cada espaço."
            )

        fonte, categoria = ReservaService.nomes_de_ids(
            dados.get("source_id"), dados.get("category_id")
        )
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    except LookupError as exc:
        return jsonify({"erro": str(exc)}), 404

    try:
        reserva = ReservaService.criar(
            source_name=fonte,
            category_name=categoria,
            customer_name=nome,
            customer_document=cpf,
            origin=ReservaOrigin.WEB,
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=itens,
            metadata_info={
                # Chaves alinhadas com as que o plugin do WordPress já grava,
                # para os relatórios existentes não precisarem de um segundo caso.
                "email": email,
                "telefone": telefone_digitos,
                "origem_canal": "site",
                "observacoes": str(dados.get("observacoes", "")).strip() or None,
            },
            ator_id=None,
        )
    except ValueError as exc:
        # Inclui a divergência de preço e a falta de disponibilidade. A mensagem
        # do serviço é em pt-BR e voltada ao usuário final.
        return jsonify({"erro": str(exc)}), 400

    return jsonify({
        "group_id": reserva.group_id,
        "reserva_id": reserva.id,
        "total": float(reserva.total_amount or 0),
        "expires_at": reserva.expires_at.isoformat() if reserva.expires_at else None,
    }), 201


@bp.route("/reservas/<string:group_id>/pagamento", methods=["GET"])
@_require_web_api_key
def dados_de_pagamento(group_id: str):
    """Dados que a tela de pagamento do site precisa, para uma reserva.

    Protegido pela chave de serviço mesmo sendo leitura. O ``group_id`` viaja na
    URL da tela de pagamento, então sem a chave qualquer pessoa com o link
    consultaria nome e CPF direto na API — que é exatamente a exposição que a
    migração do WordPress existe para encerrar.

    Quem chama é o **servidor** do Next.js. Ele usa ``payer`` para montar a
    chamada ao Mercado Pago e nunca o entrega ao navegador; o cliente só vê o
    valor e a situação da reserva.

    Args:
        group_id: UUID que agrupa as versões da reserva.

    Returns:
        200 com ``reserva_id``, ``total``, ``status``, ``expires_at``,
        ``expirada`` e ``payer``; 404 se não existir.
    """
    from app.models.reservas import Reserva, ReservaStatus

    reserva = (
        db.session.query(Reserva)
        .filter(
            Reserva.group_id == group_id,
            Reserva.deleted_at.is_(None),
            Reserva.status != ReservaStatus.ARCHIVED_VERSION,
        )
        .order_by(Reserva.version.desc())
        .first()
    )
    if reserva is None:
        return jsonify({"erro": "Reserva não encontrada."}), 404

    meta = reserva.metadata_info or {}
    partes = (reserva.customer_name or "").split()

    agora = datetime.now(timezone.utc).replace(tzinfo=None)
    expirada = reserva.expires_at is not None and reserva.expires_at <= agora

    return jsonify({
        "reserva_id": reserva.id,
        "status": reserva.status.value if hasattr(reserva.status, "value") else str(reserva.status),
        "total": float(reserva.total_amount or 0),
        "expires_at": reserva.expires_at.isoformat() if reserva.expires_at else None,
        # Calculado aqui e não no cliente: o servidor do Next roda UTC, o
        # navegador roda no fuso de quem acessa, e o relógio do visitante pode
        # estar errado.
        "expirada": expirada,
        "payer": {
            "email": meta.get("email"),
            "first_name": partes[0] if partes else "",
            "last_name": " ".join(partes[1:]) if len(partes) > 1 else "",
            "identification": {"type": "CPF", "number": reserva.customer_document},
        },
    }), 200



@bp.route("/reservas/<string:group_id>/cancelar", methods=["POST"])
@_require_web_api_key
def cancelar_reserva_web(group_id: str):
    """Desiste de uma reserva do site que ainda não foi paga.

    Existe por duas razões que acabam sendo a mesma. A primeira é o cliente que
    monta a reserva e muda de ideia: sem isto ele não tem como devolver a
    churrasqueira ao estoque, e o soft lock a segura por 15 minutos à toa. A
    segunda é a suíte de integração do site, que precisa criar reservas de
    verdade para testar o funil — e precisa saber desfazer o que criou, em vez
    de deixar lixo na lista do operador.

    Só cancela ``PENDING`` de origem ``WEB``. Uma reserva paga é caso de
    reembolso, decidido por gente, não por uma chamada de API; e uma reserva de
    balcão não pode ser cancelada pela chave do site, mesmo que o ``group_id``
    vaze. O ``group_id`` é UUID, então não se enumera reservas por tentativa.

    O cancelamento passa pelo ``ReservaService``, que versiona e audita — a
    reserva não some, ganha uma versão ``CANCELED``. É por isso que a taxa de
    conversão sobrevive a este endpoint.

    Args:
        group_id: UUID que agrupa as versões da reserva.

    Returns:
        200 com ``{"cancelada": true}``; 404 se não existir; 409 se já estiver
        paga, cancelada ou não tiver vindo do site.
    """
    from app.models.reservas import Reserva, ReservaOrigin, ReservaStatus
    from app.services.reserva_service import ReservaService

    reserva = (
        db.session.query(Reserva)
        .filter(
            Reserva.group_id == group_id,
            Reserva.deleted_at.is_(None),
            Reserva.status != ReservaStatus.ARCHIVED_VERSION,
        )
        .order_by(Reserva.version.desc())
        .first()
    )
    if reserva is None:
        return jsonify({"erro": "Reserva não encontrada."}), 404

    if reserva.origin != ReservaOrigin.WEB:
        return jsonify({"erro": "Esta reserva não foi feita pelo site."}), 409
    if reserva.status != ReservaStatus.PENDING:
        return jsonify({"erro": "Só é possível cancelar uma reserva pendente."}), 409

    try:
        ReservaService.cancelar(reserva.id, motivo="Cancelada pelo site.")
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 409

    return jsonify({"cancelada": True}), 200
