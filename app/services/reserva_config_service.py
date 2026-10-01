# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
ReservaConfigService — leitura e escrita das configurações de preço do módulo.

As chaves vivem em `ReservaConfig` (`sistur_reserva_config`), um key-value JSON
que aceita valor global (`source_id=NULL`) ou por fonte. Até aqui só existia
leitura, dentro do `ReservaPricingService`; toda alteração exigia INSERT manual
no banco. Este serviço dá à tela de Ajustes de Preço um caminho auditado.

Duas configurações, guardadas de formas DIFERENTES — é preciso respeitar o
formato que o `ReservaPricingService` já lê, senão a tela grava num lugar que
o cálculo nunca consulta:

    service_fee_percent     → chave de topo, valor escalar
                              ReservaConfig(config_key="service_fee_percent",
                                            config_value=8.1)

    flat_discount_percent   → ANINHADA dentro do JSON `pricing_rules`
                              ReservaConfig(config_key="pricing_rules",
                                            config_value={"flat_discount_percent": 5})

`pricing_rules` também carrega chaves legadas de early bird
(`early_bird_enabled`, `early_bird_days`, `early_bird_discount_percent`), que
NÃO são geridas aqui — elas duplicariam as regras Early Bird em tabela
(`AdvanceDiscountRule`), que são a fonte de verdade da tela. A escrita preserva
o que já estiver no JSON.

Granularidade da herança (a tela expõe isso como placeholder):
    - service_fee_percent herda por chave.
    - flat_discount_percent herda junto com o objeto `pricing_rules` inteiro:
      se a fonte tem `pricing_rules` próprio, ele substitui o global por
      completo, não faz merge — é assim que `_get_config_value` funciona.

Regras de arquitetura:
    - Sem importações Flask — puro Python, compatível com Cython.
    - Toda mutação chama AuditService.
    - Recebe ator_id: int | None, nunca um objeto User/Funcionario.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.core.audit import AuditService
from app.extensions import db
from app.models.reservas import ReservaConfig, ReservaSource
from app.services.base import BaseService

_AUDIT_MODULE = "reserva_config"

CHAVE_TAXA_SERVICO = "service_fee_percent"
CHAVE_DESCONTO_LINEAR = "flat_discount_percent"
CHAVE_PRICING_RULES = "pricing_rules"

_ROTULOS = {
    CHAVE_TAXA_SERVICO: "Taxa de serviço",
    CHAVE_DESCONTO_LINEAR: "Desconto linear",
}


class ReservaConfigService(BaseService):
    """CRUD auditado das configurações de precificação por fonte."""

    # ------------------------------------------------------------------
    # Acesso bruto ao ReservaConfig
    # ------------------------------------------------------------------

    @staticmethod
    def _linha(source_id: int | None, chave: str) -> ReservaConfig | None:
        """Retorna a linha de config de uma fonte (ou global, com source_id=None)."""
        query = db.session.query(ReservaConfig).filter(
            ReservaConfig.config_key == chave
        )
        if source_id is None:
            query = query.filter(ReservaConfig.source_id.is_(None))
        else:
            query = query.filter(ReservaConfig.source_id == source_id)
        return query.first()

    @staticmethod
    def _regras(source_id: int | None) -> dict:
        """Retorna o JSON `pricing_rules` de uma camada, ou {} se não existir."""
        linha = ReservaConfigService._linha(source_id, CHAVE_PRICING_RULES)
        valor = linha.config_value if linha else None
        return dict(valor) if isinstance(valor, dict) else {}

    # ------------------------------------------------------------------
    # Consultas (read-only)
    # ------------------------------------------------------------------

    @staticmethod
    def obter_pricing_config(source_id: int) -> dict:
        """Retorna as configurações de preço de uma fonte, com herança do global.

        Para cada campo devolve o valor efetivo (o que o cálculo de preço usa
        de fato), se ele é próprio da fonte ou herdado, e qual é o valor global
        — a tela precisa dos três para mostrar o herdado como placeholder.

        Args:
            source_id: PK da ReservaSource.

        Returns:
            Dict no formato::

                {
                  "service_fee_percent":   {"valor": 8.1, "proprio": False, "global": 8.1},
                  "flat_discount_percent": {"valor": 5.0, "proprio": True,  "global": None},
                }
        """
        # Taxa de serviço — chave de topo, herança por chave.
        taxa_propria = ReservaConfigService._linha(source_id, CHAVE_TAXA_SERVICO)
        taxa_global = ReservaConfigService._linha(None, CHAVE_TAXA_SERVICO)
        valor_global_taxa = taxa_global.config_value if taxa_global else None

        # Desconto linear — aninhado; herda junto com o objeto pricing_rules.
        linha_regras = ReservaConfigService._linha(source_id, CHAVE_PRICING_RULES)
        regras_efetivas = (
            ReservaConfigService._regras(source_id)
            if linha_regras
            else ReservaConfigService._regras(None)
        )
        desconto_global = ReservaConfigService._regras(None).get(CHAVE_DESCONTO_LINEAR)

        return {
            CHAVE_TAXA_SERVICO: {
                "valor": (
                    taxa_propria.config_value if taxa_propria else valor_global_taxa
                ),
                "proprio": taxa_propria is not None,
                "global": valor_global_taxa,
            },
            CHAVE_DESCONTO_LINEAR: {
                "valor": regras_efetivas.get(CHAVE_DESCONTO_LINEAR),
                "proprio": (
                    linha_regras is not None
                    and CHAVE_DESCONTO_LINEAR in ReservaConfigService._regras(source_id)
                ),
                "global": desconto_global,
            },
        }

    # ------------------------------------------------------------------
    # Validações
    # ------------------------------------------------------------------

    @staticmethod
    def _normalizar_percentual(valor, rotulo: str) -> float | None:
        """Converte a entrada do formulário em percentual válido.

        Aceita vírgula decimal (pt-BR). String vazia ou None significa "sem
        override nesta fonte" e devolve None.

        Args:
            valor: Valor cru vindo do formulário.
            rotulo: Nome do campo, para a mensagem de erro.

        Returns:
            Percentual como float, ou None para remover o override.

        Raises:
            ValueError: Se não for numérico ou estiver fora de 0–100.
        """
        if valor is None:
            return None
        if isinstance(valor, str):
            valor = valor.strip().replace(",", ".")
            if not valor:
                return None

        try:
            numero = Decimal(str(valor))
        except (InvalidOperation, TypeError):
            raise ValueError(f"{rotulo}: informe um número válido.")

        if numero < 0 or numero > 100:
            raise ValueError(f"{rotulo}: use um percentual entre 0 e 100.")

        return float(numero)

    # ------------------------------------------------------------------
    # Mutações (com auditoria)
    # ------------------------------------------------------------------

    @staticmethod
    def _gravar_taxa(source_id: int, valor: float | None) -> None:
        """Grava (ou remove) a chave de topo `service_fee_percent` da fonte."""
        linha = ReservaConfigService._linha(source_id, CHAVE_TAXA_SERVICO)

        if valor is None:
            if linha:
                db.session.delete(linha)
            return

        if linha:
            linha.config_value = valor
        else:
            db.session.add(
                ReservaConfig(
                    source_id=source_id,
                    config_key=CHAVE_TAXA_SERVICO,
                    config_value=valor,
                    description=_ROTULOS[CHAVE_TAXA_SERVICO],
                )
            )

    @staticmethod
    def _gravar_desconto_linear(source_id: int, valor: float | None) -> None:
        """Grava (ou remove) `flat_discount_percent` dentro do JSON pricing_rules.

        Preserva as demais chaves do JSON (early bird legado) e remove a linha
        inteira quando o objeto fica vazio, evitando lixo no banco.
        """
        linha = ReservaConfigService._linha(source_id, CHAVE_PRICING_RULES)
        regras = ReservaConfigService._regras(source_id)

        if valor is None:
            regras.pop(CHAVE_DESCONTO_LINEAR, None)
        else:
            regras[CHAVE_DESCONTO_LINEAR] = valor

        if not regras:
            if linha:
                db.session.delete(linha)
            return

        if linha:
            # Reatribuição obrigatória: mutação in-place de coluna JSON não é
            # detectada pelo SQLAlchemy sem MutableDict.
            linha.config_value = regras
        else:
            db.session.add(
                ReservaConfig(
                    source_id=source_id,
                    config_key=CHAVE_PRICING_RULES,
                    config_value=regras,
                    description="Regras dinâmicas de preço",
                )
            )

    @staticmethod
    def salvar_pricing_config(
        source_id: int,
        service_fee_percent=None,
        flat_discount_percent=None,
        ator_id: int | None = None,
    ) -> dict:
        """Grava taxa de serviço e desconto linear de uma fonte.

        Campo vazio REMOVE o override daquela fonte, fazendo o valor voltar a
        herdar da configuração global — é como a tela expressa "sem regra
        própria aqui".

        Args:
            source_id: PK da ReservaSource.
            service_fee_percent: Novo percentual de taxa, ou vazio para herdar.
            flat_discount_percent: Novo desconto linear, ou vazio para herdar.
            ator_id: ID do funcionário que realiza a ação.

        Returns:
            O mesmo formato de `obter_pricing_config`, já atualizado.

        Raises:
            ValueError: Se a fonte não for encontrada ou um percentual for
                inválido (não numérico ou fora de 0–100).
        """
        fonte = db.session.get(ReservaSource, source_id)
        if not fonte or fonte.deleted_at is not None:
            raise ValueError(f"Fonte {source_id} não encontrada.")

        novos = {
            CHAVE_TAXA_SERVICO: ReservaConfigService._normalizar_percentual(
                service_fee_percent, _ROTULOS[CHAVE_TAXA_SERVICO]
            ),
            CHAVE_DESCONTO_LINEAR: ReservaConfigService._normalizar_percentual(
                flat_discount_percent, _ROTULOS[CHAVE_DESCONTO_LINEAR]
            ),
        }

        anterior = {
            chave: dados["valor"] if dados["proprio"] else None
            for chave, dados in ReservaConfigService.obter_pricing_config(
                source_id
            ).items()
        }

        ReservaConfigService._gravar_taxa(source_id, novos[CHAVE_TAXA_SERVICO])
        ReservaConfigService._gravar_desconto_linear(
            source_id, novos[CHAVE_DESCONTO_LINEAR]
        )

        db.session.flush()

        AuditService.log_update(
            module=_AUDIT_MODULE,
            entity_id=source_id,
            previous_state=anterior,
            new_state=novos,
            actor_id=ator_id,
        )

        db.session.commit()
        return ReservaConfigService.obter_pricing_config(source_id)
