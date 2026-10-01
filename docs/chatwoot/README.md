# Integração: Chatwoot (dentro do módulo Reservas)

Service: `app/services/chatwoot_service.py` (`ChatwootService`) · Rotas: no blueprint
`reservas` (`app/blueprints/reservas/routes.py`)

Integração com o Chatwoot, o CRM de WhatsApp externo ao Sistur. Não existe um blueprint
próprio: a tela de atendimento fica dentro do módulo de Reservas, e o envio do comprovante de
pagamento é feito pelo `ChatwootService`.

---

## Rotas (blueprint `reservas`)

| Rota | Auth | Propósito |
|---|---|---|
| `GET /reservas/chat` | `reservas.view` | Iframe do painel Chatwoot dentro do layout de Reservas |
| `GET /reservas/chat/abrir-contato` | `reservas.view` | Recebe `tel`/`nome`, busca ou cria o contato e a conversa, e redireciona para o iframe já na conversa certa (ou para o perfil do contato, se não houver conversa) |

Nenhuma das duas é webhook — são telas para a equipe usar a UI padrão do Chatwoot sem sair do
Sistur. Não existe grupo de permissão `"chatwoot"` no catálogo de RBAC.

O helper interno `_chatwoot_inboxes()` popula o `<select>` de inbox nos formulários de
`ReservaSource` (`/reservas/admin/sources/novo` e `/editar`), ligando `ReservaSource.inbox_id`
a um inbox real.

---

## Sem modelo próprio

Não existe `app/models/chatwoot.py`. O único vínculo em modelos é `ReservaSource.inbox_id`
(Integer, unique, nullable) + `.inbox_name`, que liga um venue a um inbox do Chatwoot — é para
esse inbox que o comprovante de pagamento é enviado.

---

## `ChatwootService` — métodos

Pura Python, sem Flask, lê config via `os.environ` diretamente (comentário no código: "para
compatibilidade Cython").

| Método | Papel |
|---|---|
| `_buscar_contato(telefone)` *(privado)* | Busca fuzzy multi-variante — ver "Normalização de telefone", abaixo |
| `_criar_contato(nome, telefone)` *(privado)* | Cria contato com telefone em E.164 |
| `_atualizar_nome_contato(contact_id, nome)` *(privado)* | Corrige nome de contato criado automaticamente pelo WhatsApp sem nome de verdade |
| `_obter_conversa_no_inbox(contact_id, inbox_id)` *(privado)* | Última conversa **aberta** do contato naquele inbox — conversas fechadas não são reabertas |
| `_criar_conversa(contact_id, inbox_id)` *(privado)* | Cria conversa nova se não houver uma aberta |
| `_enviar_mensagem(conversation_id, mensagem)` *(privado)* | Envia mensagem de saída, não-privada |
| `_formatar_comprovante(reserva, source, payment_data)` *(privado)* | Monta o texto do comprovante em markdown do WhatsApp |
| **`enviar_comprovante_pagamento(reserva_id)`** | **Ponto de entrada público.** Orquestra tudo acima — ver fluxo abaixo |

Todas as chamadas HTTP ficam em `try/except Exception` amplo, logam e retornam `None`/`False` —
nunca propagam exceção. `enviar_comprovante_pagamento` tem uma segunda camada de try/except em
volta de tudo, loga em `error` com `exc_info=True` e retorna `False`.

---

## Fluxo de `enviar_comprovante_pagamento`

Chamado por `PaymentService` (`docs/pagamentos/README.md`) quando uma `Reserva` vira `PAID`,
sempre dentro de um `try/except` que só loga — **um Chatwoot fora do ar nunca derruba um
pagamento**.

1. Se `CHATWOOT_API_TOKEN` vazio, sai sem tentar nada.
2. Exige `reserva.source` e `source.inbox_id` configurados — sem isso, aborta com log apontando
   pra tela `/reservas/admin/sources` (onde se configura o inbox do venue).
3. Busca telefone em `reserva.metadata_info` (chaves `telefone`/`phone`/`tel`, nessa ordem).
4. Busca ou cria o contato no Chatwoot pelo telefone.
5. Se o nome atual do contato estiver vazio ou for só dígitos (`re.fullmatch(r"[\d\s\+\-\(\)]+",
   nome)`) — típico de contato que o WhatsApp cria só com o número — sobrescreve com
   `reserva.customer_name`.
6. Busca conversa **aberta** no inbox do venue; se não achar, cria uma nova.
7. Envia o comprovante formatado.

**Não há vínculo local entre contato do WhatsApp e `Funcionario`/cliente interno** — a
correspondência é sempre por telefone, direto contra a API do Chatwoot, a cada chamada.

---

## Normalização de telefone

Números brasileiros aparecem de forma inconsistente entre sistemas (com/sem `55`, com/sem o `9`
extra do celular). `_buscar_contato` gera variantes e tenta cada uma até achar:
`55+local`, `local` sem `55`, e — se o número tiver 11 dígitos com `9` na posição 2 — a variante
sem esse `9`, mais o sufixo dos últimos 8 dígitos como último recurso.

---

## Dois pontos de integração — atenção ao editar

`GET /reservas/chat/abrir-contato` repete **inline** a lógica de normalização, busca e criação
de contato e conversa — não reaproveita `ChatwootService`. Se for corrigir um bug de
contato/conversa, cheque se ele se repete no outro lugar.

**Duas URLs base diferentes:** a pública (`https://atendimento.cachoeiradogirassol.com.br`,
fixa em `reservas/routes.py`, usada só pelo iframe do navegador) e a interna
(`http://chatwoot-rails-1:3000`, rede Docker, evita o 401 que ocorre passando pelo proxy
HTTPS). `ChatwootService._base_url()` lê `CHATWOOT_INTERNAL_URL` com esse default;
`reservas/routes.py` tem a própria constante `_CHATWOOT_API_URL` com o mesmo valor, mas **não**
lê a variável de ambiente.

**Requisito de rede:** o container do Sistur precisa alcançar `chatwoot-rails-1` pela rede
Docker. Sem isso, o comprovante falha em silêncio (só gera log) e a busca de contato não funciona.

---

## Variáveis de ambiente

| Variável | Lida por | Default | Papel |
|---|---|---|---|
| `CHATWOOT_API_TOKEN` | `ChatwootService` (`os.environ`) e `reservas/routes.py` (`current_app.config`) | `""` | Header `api_access_token` em toda chamada à API |
| `CHATWOOT_ACCOUNT_ID` | idem | `"1"` | Segmento de conta nas URLs da API |
| `CHATWOOT_INTERNAL_URL` | só `ChatwootService._base_url()` | `http://chatwoot-rails-1:3000` | Base interna Docker — `reservas/routes.py` tem o mesmo valor hardcoded, não configurável |

**Nota sobre o cofre:** `SecretService.CHATWOOT_API_TOKEN = "chatwoot.api_token"` está
declarado como chave do cofre de segredos, mas **nenhum código lê essa chave** — o token real
sempre vem de `os.environ`/`current_app.config`. É um caminho de migração planejado e nunca
finalizado.

---

## Testes

**Nenhum teste dedicado existe** — nem para `ChatwootService`, nem para as duas rotas de chat.
Gap conhecido.
