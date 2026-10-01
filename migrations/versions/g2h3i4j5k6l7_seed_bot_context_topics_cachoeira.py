"""seed BotContextTopic inicial para Cachoeira do Girassol (source_id=1)

Converte o conteúdo existente do BotIntelligence (comida, churrasqueira, geral, reserva)
em entradas do catálogo versionado e adiciona placeholders para precos, criancas,
idosos, cancelamento e pagamento.

Revision ID: g2h3i4j5k6l7
Revises: f1g2h3i4j5k6
Create Date: 2026-06-09
"""

revision = "g2h3i4j5k6l7"
down_revision = "f1g2h3i4j5k6"
branch_labels = None
depends_on = None

from alembic import op


_SOURCE_CHECK = "EXISTS (SELECT 1 FROM sistur_reserva_sources WHERE id = 1)"

_ENTRIES = [
    dict(
        slug="geral",
        title="Comportamento Geral do Bot",
        selector_summary=(
            "Diretrizes gerais de comportamento, postura e limites de assunto do bot. "
            "Sempre presente como contexto base em todas as conversas."
        ),
        content=(
            "Limites de Assunto: Não emita opiniões sobre política, religião ou temas polêmicos. "
            "Caso ocorra, mude o assunto gentilmente trazendo a conversa novamente ao foco de reservas"
        ),
        topics='["comportamento", "postura", "limites de assunto", "temas polêmicos", "foco"]',
        negative_topics=None,
        sample_questions='["O bot pode falar sobre política?", "Qual é o foco do atendimento?"]',
        applies_to=None,
        dependencies=None,
        conflicts=None,
        priority="informational",
        source_category="geral",
    ),
    dict(
        slug="comida",
        title="Alimentos e Bebidas Externos",
        selector_summary=(
            "Regras sobre o que os visitantes podem levar ao empreendimento: "
            "alimentos, bebidas e restrições a recipientes de vidro."
        ),
        content=(
            "ALIMENTOS E BEBIDAS EXTERNOS:\n"
            "- É permitido levar alimentos e bebidas para Day Use e Camping.\n"
            "- É proibido entrar com utensílios, garrafas ou recipientes de vidro.\n"
            "- Esta regra trata do que o visitante pode levar. "
            "Para cardápio e compras no restaurante, use o contexto de restaurante."
        ),
        topics='["comida", "alimentos", "bebidas", "lanche", "refeição", "vidro", "utensílios", "caixa de isopor"]',
        negative_topics='["cardápio", "restaurante", "compras no local", "pedido", "prato", "delivery"]',
        sample_questions=(
            '["Posso levar comida?", "Pode levar bebida?", '
            '"Pode trazer lanche?", "Tem restrição de vidro?", "Pode levar isopor?"]'
        ),
        applies_to='["day_use", "camping"]',
        dependencies=None,
        conflicts=None,
        priority="operational",
        source_category="comida",
    ),
    dict(
        slug="churrasqueira",
        title="Uso de Churrasqueiras",
        selector_summary=(
            "Política sobre churrasqueiras — diferença crítica entre levar portátil "
            "(somente camping) e alugar a coberta do local (day use obrigatório). "
            "Sempre explicar as duas opções ao cliente."
        ),
        content=(
            "CHURRASQUEIRAS:\n"
            "- Somente clientes da modalidade Camping podem levar churrasqueira portátil ou própria.\n"
            "- Clientes Day Use NÃO podem levar churrasqueira portátil ou própria.\n"
            "- Para fazer churrasco no Day Use, o cliente deve alugar uma churrasqueira coberta do empreendimento.\n"
            "- Não diga apenas que há churrasqueira disponível: explique obrigatoriamente a diferença entre "
            "levar churrasqueira própria e alugar uma churrasqueira do local.\n"
            "- A disponibilidade e o preço da churrasqueira devem ser consultados antes de confirmar a locação."
        ),
        topics=(
            '["churrasqueira", "churrasco", "grelha", "carvão", "assado", '
            '"churrasqueira portátil", "churrasqueira coberta", "aluguel churrasqueira"]'
        ),
        negative_topics='["cardápio", "restaurante", "piscina", "comida delivery"]',
        sample_questions=(
            '["Pode levar churrasqueira?", "Tem churrasqueira para alugar?", '
            '"Pode fazer churrasco no day use?", "Pode churrasco no camping?", '
            '"Quanto custa a churrasqueira?"]'
        ),
        applies_to='["day_use", "camping"]',
        dependencies='["comida"]',
        conflicts=None,
        priority="critical",
        source_category="churrasqueira",
    ),
    dict(
        slug="reserva",
        title="Processo de Reserva",
        selector_summary=(
            "Fluxo de criação e gestão de reservas: coleta de dados, cotação, "
            "confirmação, cancelamento e modificação."
        ),
        content=(
            "PROCESSO DE RESERVA:\n"
            "-- Conteúdo a ser preenchido no painel administrativo --\n"
            "Use as ferramentas BuscarReservas, CriarReserva e CotarReserva para operações de reserva."
        ),
        topics=(
            '["reserva", "agendamento", "booking", "data", "check-in", "check-out", '
            '"cotação", "confirmação", "cancelamento de reserva"]'
        ),
        negative_topics='["preço geral", "regras gerais", "comida", "churrasqueira"]',
        sample_questions=(
            '["Quero fazer uma reserva", "Quanto custa para o dia X?", '
            '"Posso cancelar minha reserva?", "Como faço para agendar?"]'
        ),
        applies_to=None,
        dependencies=None,
        conflicts=None,
        priority="operational",
        source_category="reserva",
    ),
    dict(
        slug="precos",
        title="Preços e Tarifas",
        selector_summary=(
            "Tabela de preços, tarifas por modalidade (Day Use / Camping) e faixas etárias. "
            "Usar junto com o contexto de reserva ao cotar valores."
        ),
        content=(
            "PREÇOS E TARIFAS:\n"
            "-- Conteúdo a ser preenchido no painel administrativo --\n"
            "Consulte o sistema de cotação (CotarReserva) para valores atualizados."
        ),
        topics=(
            '["preço", "tarifa", "valor", "custo", "quanto custa", "tabela de preços", '
            '"diária", "entrada", "ingresso"]'
        ),
        negative_topics='["reserva específica", "cancelamento", "churrasqueira"]',
        sample_questions=(
            '["Qual o preço da entrada?", "Quanto custa o camping?", '
            '"Tem desconto para grupo?", "Qual a tarifa do day use?"]'
        ),
        applies_to=None,
        dependencies=None,
        conflicts=None,
        priority="critical",
        source_category=None,
    ),
    dict(
        slug="criancas",
        title="Regras para Crianças",
        selector_summary=(
            "Faixas etárias com tarifa reduzida ou gratuidade, regras de acompanhamento "
            "de menores e restrições de idade em atividades."
        ),
        content=(
            "CRIANÇAS:\n"
            "-- Conteúdo a ser preenchido no painel administrativo --"
        ),
        topics=(
            '["criança", "crianças", "bebê", "infantil", "menor", '
            '"meia entrada criança", "gratuidade criança", "idade"]'
        ),
        negative_topics='["idoso", "desconto sênior"]',
        sample_questions=(
            '["Crianças pagam?", "Qual a idade para gratuidade?", '
            '"Pode levar bebê?", "Tem atividade para criança?"]'
        ),
        applies_to=None,
        dependencies='["precos"]',
        conflicts=None,
        priority="operational",
        source_category=None,
    ),
    dict(
        slug="idosos",
        title="Regras para Idosos",
        selector_summary=(
            "Tarifas especiais para idosos (meia entrada / gratuidade) e "
            "documentação exigida na portaria."
        ),
        content=(
            "IDOSOS:\n"
            "-- Conteúdo a ser preenchido no painel administrativo --"
        ),
        topics=(
            '["idoso", "idosos", "sênior", "terceira idade", '
            '"meia entrada idoso", "gratuidade idoso", "65 anos"]'
        ),
        negative_topics='["criança", "desconto infantil"]',
        sample_questions=(
            '["Idoso paga entrada?", "Tem desconto para idoso?", '
            '"Qual a idade para meia entrada?", "Precisa de documento?"]'
        ),
        applies_to=None,
        dependencies='["precos"]',
        conflicts=None,
        priority="operational",
        source_category=None,
    ),
    dict(
        slug="cancelamento",
        title="Política de Cancelamento",
        selector_summary=(
            "Regras de cancelamento: prazo, multa, reembolso e processo para "
            "solicitar cancelamento de reservas."
        ),
        content=(
            "POLÍTICA DE CANCELAMENTO:\n"
            "-- Conteúdo a ser preenchido no painel administrativo --"
        ),
        topics=(
            '["cancelamento", "cancelar", "reembolso", "estorno", "devolução", '
            '"multa cancelamento", "prazo cancelamento"]'
        ),
        negative_topics='["nova reserva", "cotação", "preço"]',
        sample_questions=(
            '["Posso cancelar minha reserva?", "Tem multa para cancelar?", '
            '"Como pedir reembolso?", "Qual o prazo para cancelar?"]'
        ),
        applies_to=None,
        dependencies='["reserva"]',
        conflicts=None,
        priority="critical",
        source_category=None,
    ),
    dict(
        slug="pagamento",
        title="Formas de Pagamento",
        selector_summary=(
            "Meios de pagamento aceitos (PIX, cartão, dinheiro), parcelamento, "
            "momento do pagamento e comprovantes."
        ),
        content=(
            "FORMAS DE PAGAMENTO:\n"
            "-- Conteúdo a ser preenchido no painel administrativo --"
        ),
        topics=(
            '["pagamento", "pagar", "PIX", "cartão", "débito", "crédito", '
            '"dinheiro", "parcelamento", "comprovante"]'
        ),
        negative_topics='["cancelamento", "reembolso"]',
        sample_questions=(
            '["Aceita PIX?", "Pode parcelar?", "Aceita cartão de débito?", '
            '"Quando pago a reserva?"]'
        ),
        applies_to=None,
        dependencies='["reserva", "precos"]',
        conflicts=None,
        priority="critical",
        source_category=None,
    ),
]


def _sql_str(value):
    """Escapes a Python string for MySQL single-quoted literal (None → NULL)."""
    if value is None:
        return "NULL"
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def upgrade():
    for entry in _ENTRIES:
        op.execute(
            f"""
            INSERT INTO sistur_bot_context_topics
                (source_id, slug, title, selector_summary, content,
                 topics_json, negative_topics_json, sample_questions_json,
                 applies_to_json, dependencies_json, conflicts_json,
                 priority, version, is_active, source_category,
                 created_at, updated_at)
            SELECT
                1,
                {_sql_str(entry['slug'])},
                {_sql_str(entry['title'])},
                {_sql_str(entry['selector_summary'])},
                {_sql_str(entry['content'])},
                {_sql_str(entry['topics'])},
                {_sql_str(entry['negative_topics'])},
                {_sql_str(entry['sample_questions'])},
                {_sql_str(entry['applies_to'])},
                {_sql_str(entry['dependencies'])},
                {_sql_str(entry['conflicts'])},
                {_sql_str(entry['priority'])},
                1,
                TRUE,
                {_sql_str(entry['source_category'])},
                NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM sistur_bot_context_topics
                WHERE source_id = 1 AND slug = {_sql_str(entry['slug'])} AND version = 1
            )
              AND {_SOURCE_CHECK}
            """
        )


def downgrade():
    slugs = ", ".join(f"'{e['slug']}'" for e in _ENTRIES)
    op.execute(
        f"""
        DELETE FROM sistur_bot_context_topics
        WHERE source_id = 1
          AND slug IN ({slugs})
          AND version = 1
        """
    )
