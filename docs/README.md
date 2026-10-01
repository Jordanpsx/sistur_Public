# Documentação do SISTUR

Índice da pasta `docs/`. Cada módulo tem dois níveis de documentação:

1. entrada resumida em **`api_catalog.md`** (contrato de todos os endpoints, num arquivo só);
2. descrição completa no **`docs/<módulo>/README.md`** correspondente.

**Pontos de atenção — o README de cada um já deixa isso explícito:** `admin` e `pagamentos`
não têm teste de rota; a integração com o `chatwoot` não tem teste nenhum e duplica lógica
entre o service e `reservas/routes.py`.

---

## Contrato de API

| Arquivo | Conteúdo |
|---|---|
| [`api_catalog.md`](api_catalog.md) | **Catálogo de endpoints** de todos os módulos — auth, params, respostas, erros |

---

## Módulos

### Portal interno / RH

| Módulo | Doc |
|---|---|
| RH (dashboard, folha de ponto, integridade do banco de horas) | [`rh/`](rh/README.md) · [`rh/reprocessamento.md`](rh/reprocessamento.md) |
| Funcionários (CRUD cadastral) | [`funcionarios/`](funcionarios/README.md) |
| Ponto Eletrônico | [`ponto/`](ponto/README.md) |
| Banco de Horas | [`banco_horas/`](banco_horas/README.md) |
| Organização (áreas, funções, equipes, timeline) | [`organizacao/`](organizacao/README.md) |
| Escala (grade mensal, presença) | [`escala/`](escala/README.md) |
| Avisos (notificações, jobs de presença) | [`avisos/`](avisos/README.md) |
| Relatórios (Central de Relatórios, PDF) | [`relatorios/`](relatorios/README.md) |
| Autenticação, sessão e permissões | [`auth/`](auth/README.md) |

### Reservas e vendas

| Módulo | Doc |
|---|---|
| Reservas (core, inventário em 3 camadas, versionamento) | [`reservas/`](reservas/README.md) |
| Reservas — guia para quem integra um frontend | [`reservas/GUIA_FRONTEND.md`](reservas/GUIA_FRONTEND.md) |
| Reservas — arquitetura do inventário em 3 camadas | [`reservas/REFACTORING_3TIER_INVENTORY.md`](reservas/REFACTORING_3TIER_INVENTORY.md) |
| Reservas — serviço de precificação | [`../PRICING_SERVICE_IMPLEMENTATION.md`](../PRICING_SERVICE_IMPLEMENTATION.md) |
| PDV (balcão, turno, offline) | [`pdv/`](pdv/README.md) · [`../PDV_PROJECT_PLAN.md`](../PDV_PROJECT_PLAN.md) |
| Pagamentos (Mercado Pago — webhook, PIX manual, Defesa Ativa) | [`pagamentos/`](pagamentos/README.md) |
| Chatwoot (aba de atendimento em Reservas, comprovante de pagamento) | [`chatwoot/`](chatwoot/README.md) |

### Infraestrutura e administração

| Assunto | Doc |
|---|---|
| Configurações (módulos, marca, perfis, geofence, calendário, informativos, cofre de segredos) | [`configuracoes/`](configuracoes/README.md) |
| Admin (auditoria, logs de integração, consumo de itens) | [`admin/`](admin/README.md) |
| Logs de integração | [`logs/`](logs/README.md) |
| Autenticação do leitor de QR | [`QR_SERVICE_TOKEN_AUTH.md`](QR_SERVICE_TOKEN_AUTH.md) |
| Totem e ponto local (instalação) | [`../kiosk/README.md`](../kiosk/README.md) · [`../externo/README_PRODUCAO.md`](../externo/README_PRODUCAO.md) · [`../externo/README_IMPORTADOR.md`](../externo/README_IMPORTADOR.md) |

---

## Dados herdados do Sistur completo

Bancos que vieram do Sistur completo guardam tabelas de módulos que este código não tem
(bot do WhatsApp, assistente de IA, tarefas, estoque, restaurante, landing e mídia). Elas não
têm model, não são lidas nem alteradas, e o Alembic está configurado para ignorá-las
(`include_object` em `migrations/env.py`) — o `flask db migrate` nunca propõe apagá-las.
