# Guia de integração — itens, preços e criação de reserva

Guia prático para quem consome a API de Reservas de fora do Sistur: o site em
Next.js, o PDV, o plugin do WordPress.

O `README.md` deste diretório descreve o modelo. Este documento descreve as
**armadilhas** — as coisas que a assinatura de um endpoint não conta e que
custaram horas de depuração. Cada seção existe porque alguém errou ali.

> Escrito em 2026-08-17, a partir da integração do site novo. Os erros descritos
> aconteceram de verdade nessa integração.

---

## 1. O fluxo mínimo

```
GET  /api/public/reservas/catalogo          → o que existe e quanto custa
POST /reservas/api/public/simular           → quanto custa NESTA data
POST /api/public/reservas/criar             → grava a reserva  (chave de serviço)
```

Só o terceiro exige autenticação. Os dois primeiros são públicos e não escrevem
nada.

---

## 2. `/catalogo` — descobrir os itens

`GET /api/public/reservas/catalogo` devolve `sources` (venues) com suas
`categories`, e uma lista **plana** de `items`.

Campos do item que importam:

| Campo | Para quê |
|---|---|
| `id` | É o que `/simular` e `/criar` recebem |
| `category_id`, `source_id` | Descobrir a que experiência o item pertence |
| `internal_slug` | Referência estável; sobrevive a renomear o item |
| `billing_type` | `FIXED` cobra uma vez; qualquer outro cobra por diária |
| `price` | **Preço base — quase nunca é o cobrado.** Ver §3 |
| `price_weekday` / `price_weekend` / `price_holiday` | Tarifas por tipo de dia |

### ⚠️ `visible_to_web` filtra o que o site oferece

O catálogo só devolve itens com `visible_to_web = true`. É eixo **separado** de
`visible_to_bot`, campo herdado do bot do WhatsApp que continua no cadastro do item mas
não tem mais efeito: um não implica o outro.

### ⚠️ A lista é plana

`items` traz **todos** os itens de **todos** os venues. Filtre por
`category_id`, ou vai oferecer barraca de camping a quem escolheu day use.

`category_id` e `source_id` só existem no payload desde 2026-08-17 — antes não
havia como fazer esse filtro pela API.

### Convenção de nomes: ingresso vs. adicional

Não existe campo que diga "isto é uma entrada". A convenção é o `internal_slug`:

```
dayuse_entrada_inteira      → ingresso
camping_entrada_meia        → ingresso
camping_churrasqueira_a     → adicional
```

Um slug contendo `_entrada_` é ingresso. O resto é adicional. Itens **sem
slug** existem (Tirolesa, Arvorismo) — trate-os como adicionais, que é o lado
seguro: um item não reconhecido aparece como opcional em vez de ser apresentado
como ingresso obrigatório.

### Item gratuito custa R$ 0,01

A faixa "Isento" (criança pequena) é gravada com `price = 0.01`, não zero.
Exibir isso como preço faz o formulário parecer quebrado. Trate `price <= 0.01`
como "sem custo".

---

## 3. ⚠️ Nunca calcule preço no cliente

**A armadilha mais cara deste guia.**

`item.price` é o preço base. O motor aplica, por cima:

1. **Tarifa por tipo de dia** — `price_weekday` / `price_weekend` /
   `price_holiday`;
2. **Desconto por antecedência**, configurado **por categoria**;
3. **Taxa de serviço**, configurada por venue.

Números reais do Day Use:

| | valor |
|---|---|
| `price` (base) | R$ 30,00 |
| `price_weekday` | R$ 30,01 |
| `price_weekend` | R$ 35,00 |
| `price_holiday` | R$ 40,03 |

A Inteira tem as três faixas preenchidas, então **os R$ 30,00 nunca são
cobrados**. Um site que exibisse "a partir de R$ 30,00" estaria errado em todos
os dias do ano.

E não dá para resolver lendo as três colunas: saber se uma data é feriado ou
alta temporada mora no **Calendário Global** (`CalendarService`), não no
catálogo. Nenhuma coluna do item responde isso.

> Pelo CDC Art. 30 a publicidade obriga o fornecedor. Um preço exibido abaixo do
> real vira prejuízo; acima do real espanta o cliente. Os dois são problema.

**Sempre pergunte ao `/simular`.**

---

## 4. `/simular` — o preço autoritativo

`POST /reservas/api/public/simular`, público, não grava nada. Pode ser chamado a
cada tecla digitada.

```json
{
  "source_id": 1,
  "category_id": 1,
  "check_in_date": "2026-09-20",
  "check_out_date": "2026-09-20",
  "items": [{"item_id": 1, "quantity": 2}]
}
```

### ⚠️ `category_id` é opcional e omiti-lo dá o preço errado

É a pegadinha central deste endpoint. `category_id` não é obrigatório, a
requisição passa sem ele, devolve 200 — e o número vem **errado**, porque as
regras de desconto ficam presas à categoria.

```
sem category_id → total 70,00
com category_id → total 49,00   ← é o que será cobrado
```

Trinta por cento de diferença, sem nenhum erro em lugar nenhum. **Sempre mande
`category_id`.**

### Day use manda a mesma data duas vezes

Não existe `check_out` opcional aqui. Para visita de um dia, mande
`check_out_date == check_in_date`; itens `FIXED` são cobrados uma vez.

### Estoque decide como oferecer o recurso

`/disponibilidade` traz `stock` (capacidade total) além de `available` (quantas
sobraram). Os dois não são a mesma pergunta:

| `stock` | O que é | Como oferecer |
|---|---|---|
| 1 | Uma unidade identificável | Card com a foto dela — "Churrasqueira A4" |
| > 1 | Pool intercambiável | Contador de quantidade — "3 colchões de casal" |

Ninguém quer escolher **qual** das oito barracas pequenas; escolhe quantas. Mas
a A4 e a A1 não são a mesma churrasqueira, e a foto é de uma delas.

Decida por **seção inteira**, não tarifa a tarifa: "Itens para Camping" tem
Barraca Pequena com estoque 8 e Barraca Grande com estoque 1, e resolvendo uma a
uma a seção sai com cinco contadores e um card "Selecionar" perdido no meio.

Não use `available` para isso — um pool com uma unidade sobrando é
indistinguível de uma unidade.

### ⚠️ Camping manda hora, e a hora é preço

Itens `DAILY_PRORATED_HOURLY` cobram a **primeira diária cheia mais pró-rata das
horas excedentes**. Duas Inteiras de camping:

| Seleção | Horas | Total |
|---|---|---|
| 14:00 → 14:00 do dia seguinte | 24 | R$ 90,00 |
| 08:00 → 17:00 do dia seguinte | 33 | R$ 123,75 |

São a mesma data de entrada e a mesma de saída. Mandar só `YYYY-MM-DD` faz o
Sistur assumir meia-noite nas duas pontas, devolver 24 horas e você anunciar
R$ 90,00 numa estadia que a portaria cobra R$ 123,75.

Como saber se a modalidade tem horário: a categoria traz `check_in_from`,
`check_in_until`, `check_out_until` e `min_stay_hours` no `/catalogo`. Nulos
(Day use) mande só a data. Preenchidos, peça a hora ao cliente e mande
`YYYY-MM-DDTHH:MM` em `/simular`, `/disponibilidade` e `criar` — **os três**.

Esquecer um deles não dá erro visível. `/disponibilidade` respondendo sobre outro
instante devolve as churrasqueiras erradas; `criar` sobre outro instante é
recusado por divergência de preço, que ao menos falha alto. O caso silencioso é o
pior: durante esta implementação `/disponibilidade` recusava o `T`, devolvia zero
recursos, e o site caía sem avisar no modo antigo — oferecendo a tarifa
"Churrasqueira Grande (A)" em vez da churrasqueira A4 com foto.

Os limites nunca são constante no front. A portaria muda de horário sem ninguém
editar o site; é dado da categoria, editável na tela de administração.

### Descobrindo o preço unitário de cada item

Peça **todos** os itens com `quantity: 1` e leia `unit_price` de cada linha do
`items_breakdown`. Esse valor já traz a tarifa do dia resolvida.

É uma chamada só, e depende **apenas das datas** — não repita quando o cliente
mudar uma quantidade.

### Lendo a resposta

| Campo | Cuidado |
|---|---|
| `items_breakdown[].unit_price` | Já com a tarifa do dia. Em item por diária, é o valor **da diária** |
| `items_breakdown[].item_total` | `unit_price × quantity × diárias`. **Antes** do desconto |
| `subtotal` | Soma dos `item_total` |
| `discount_amount` | Desconto por antecedência etc. |
| `service_fee` | Taxa de serviço |
| `total` | **O que o cliente paga.** É o único número a exibir como total |

`num_days` costuma vir `null` mesmo em item por diária — não confie nele.
Calcule as diárias a partir das datas.

---

## 5. `/criar` — gravar a reserva

`POST /api/public/reservas/criar`, header `X-Web-Api-Key`.

Só o **servidor** do frontend deve chamar: a chave é um segredo compartilhado e
não pode ir para o navegador. É também por isso que dado pessoal viaja aqui, no
corpo de um POST servidor-a-servidor, e nunca numa query string.

```json
{
  "source_id": 1, "category_id": 1,
  "customer_name": "Maria Aparecida Souza",
  "customer_document": "529.982.247-25",
  "email": "maria@exemplo.com.br",
  "telefone": "(64) 99999-1234",
  "check_in_date": "2026-09-20", "check_out_date": "2026-09-20",
  "items": [{"item_id": 1, "quantity": 2, "price_override": 49.00}]
}
```

CPF é obrigatório e validado por dígito verificador. Nome precisa de ao menos
duas palavras. E-mail e telefone vão para `metadata_info`, com as mesmas chaves
que o plugin do WordPress já usa (`email`, `telefone`, `origem_canal`).

### ⚠️ `price_override` soma o TOTAL, não o subtotal

A anti-fraude soma todos os `price_override` e compara com o **total** que ela
mesma recalcula, recusando diferença acima de R$ 0,01.

O erro natural é mandar `items_breakdown[].item_total` direto. Ele é o valor
**antes** do desconto, e a criação é recusada assim que existir qualquer
desconto:

```
Divergência de preço: O valor calculado pelo servidor (49.00)
difere do valor enviado (70.00). Diferença: 21.00.
```

Ratear o total pelas linhas, com a última absorvendo o arredondamento:

```python
linhas = orcamento["items_breakdown"]
base   = sum(l["item_total"] for l in linhas)
cent   = lambda v: round(v * 100)

restante = cent(orcamento["total"])
items = []
for i, l in enumerate(linhas):
    ultimo = i == len(linhas) - 1
    valor  = restante if ultimo else round(cent(orcamento["total"]) * l["item_total"] / base)
    restante -= valor
    items.append({"item_id": l["item_id"], "quantity": l["quantity"],
                  "price_override": valor / 100})
```

Arredondar cada linha de forma independente desvia alguns centavos em carrinho
grande e estoura a tolerância.

### Esta divergência também é um caso legítimo

Não é só defesa contra payload adulterado: a tabela pode ter mudado entre a
cotação e o envio. Trate mostrando o valor novo e pedindo confirmação, não como
erro genérico.

### Respostas

| Código | Quando |
|---|---|
| 201 | Criada. Devolve `group_id`, `reserva_id`, `total`, `expires_at` |
| 400 | Validação, divergência de preço, ou sem recurso disponível. `erro` é exibível ao cliente |
| 401 | Chave ausente ou errada |
| 404 | `source_id`/`category_id` inexistente, ou categoria de outro venue |
| 503 | `WEB_API_KEY` não configurada no servidor |

---

## 6. Recursos físicos — o que confunde

Um item pode exigir vínculo a um **recurso físico** (`requires_resource`), e o
servidor escolhe um livre do grupo automaticamente.

### Pool compartilhado: só um item leva o recurso

"Capacidade Day Use" tem `stock_quantity = NULL` (ilimitado) e é dividido por
Inteira, Meia-Entrada e Isento — é o desenho do modelo de 3 camadas.

Numa reserva com vários ingressos, **o primeiro item fica com o `resource_id` e
os demais ficam `NULL`**:

```
reserva 2380 | Inteira      | qtd 2 | resource_id 1
reserva 2380 | Meia-Entrada | qtd 1 | resource_id NULL
```

Não é bug nem dado faltando. `uq_reserva_resource_link` proíbe
`(reserva_id, resource_id)` repetido, e o Postgres aceita vários `NULL`.

> Até 2026-08-17 esse caso era **recusado** com "Nenhum recurso físico
> disponível", derrubando a reserva mais comum que existe: dois adultos e uma
> criança.

### Estoque finito: overbooking de verdade

Recurso com `stock_quantity` numérico é ativo escasso (uma churrasqueira
específica). Esgotado o grupo, a criação falha com 400:

```
Nenhum recurso físico disponível para este item no período.
```

Isso é resposta legítima, não erro de integração. Trate como "esgotado nesta
data".

### O que segura vaga, e por quanto tempo

| Status | Segura recurso? | Até quando |
|---|---|---|
| `PENDING` | Sim | Enquanto `expires_at` não vencer (15 min) |
| `PAID`, `EM_USO`, `DEPOSIT`, `PARTIALLY_PAID` | Sim | Sempre |
| `CANCELED`, `COMPLETED` | Não | — |

Uma reserva não paga segura o recurso por 15 minutos e o solta sozinha. Não há
rotina de limpeza: a linha permanece no banco e para de contar pelo prazo.

**Consequência prática:** entre cotar e concluir, a capacidade continua
disponível para outros. Se dois clientes disputam a última churrasqueira, o
segundo só descobre no submit. Trate o 400 como resultado esperado do fluxo, não
como falha.

---

## 6a. Oferecer o recurso físico, não a tarifa

`GET /api/public/reservas/disponibilidade?source_id=&category_id=&check_in=&check_out=`

O formulário do site oferece **a churrasqueira** — "Churrasqueira A4" — e não a
tarifa "Churrasqueira Grande (A)". Só assim dá para dizer se **aquela** está
livre na data e mostrar as fotos **dela**.

Cada recurso volta com `item_id` e `price`: a tarifa do grupo a que ele pertence,
na categoria pedida. A4 está em "Churrasqueiras grandes (A)", cujo item de day
use custa R$ 120,00 — o mesmo caminho que a atribuição automática percorre ao
contrário, então site e balcão cobram igual.

Recursos cuja tarifa é `is_entry_ticket` são omitidos: "Capacidade Day Use" é o
pool que as entradas consomem, não um espaço que alguém escolhe.

### ⚠️ Ao criar, mande o `resource_id`

`/simular` fala em tarifa e quantidade — duas churrasqueiras do mesmo grupo são
uma linha de quantidade dois. Mas `/criar` precisa de **uma linha por recurso**:

```json
{"item_id": 7, "quantity": 1, "resource_id": 6, "price_override": 84.00}
```

Sem `resource_id` o Sistur escolhe qualquer recurso livre do grupo, e o cliente
que reservou a A4 recebe a A1. O `uq_reserva_resource_link` é
`(reserva_id, resource_id)`, então duas linhas com o mesmo `item_id` e recursos
diferentes convivem — não existe uma unicidade `(reserva_id, item_id)`.

### ⚠️ Um espaço físico por reserva

`/criar` recusa mais de um `resource_id` distinto no mesmo pedido:

```
É permitida apenas uma churrasqueira por reserva.
```

Ingressos não entram no limite — cinco entradas continuam valendo. A regra é do
servidor, não do formulário: uma URL com dois `r<id>` chegaria à criação sem
passar pela tela.

## 6b. Webhook — para onde o Mercado Pago avisa

O pagamento não termina na resposta do `/process`. Um PIX volta `pending`, e a
reserva só vira `PAID` quando o **webhook** do Mercado Pago chega ao Sistur.

### ⚠️ Sem `MP_NOTIFICATION_URL`, a notificação vai para o ambiente errado

O Mercado Pago usa o webhook cadastrado na conta, que é **um só para a conta
inteira** e aponta para produção. Uma cobrança criada no staging é paga de
verdade, a notificação chega em produção, e lá a transação não existe:

```
WEBHOOK_ORPHAN_TRANSACTION
Pagamento aprovado no Mercado Pago (mp_payment_id=…) referencia a
transação …, que não existe no Sistur.
```

A reserva do staging fica `PENDING` até alguém reprocessar na mão, enquanto o
cliente olha um QR code de algo já pago.

Desde 2026-08-18 o Sistur envia `notification_url` por pagamento, lido de
`MP_NOTIFICATION_URL`. **Todo ambiente que gera cobrança precisa da variável
apontando para o próprio webhook.** Ausente, vale o webhook da conta — o
comportamento antigo.

### O `payer` é quem paga, não quem entra

`/process` recebe o pagador que o **Brick coletou**, não o titular da reserva.
São coisas diferentes: o CPF da reserva é de quem vai entrar, e o `payer` do
Mercado Pago é de quem pagou — uma mãe pagando o day use do filho é o caso
comum.

Trocar um pelo outro faz o token do cartão nascer com um documento e a cobrança
sair com outro, porque a tokenização acontece dentro do Brick com o que o
cliente digitou ali.

O que **nunca** vem do navegador é o `transaction_amount`: ele sai da reserva
gravada. É esse campo que protege a cobrança, e a razão de um `payer` adulterado
não gerar prejuízo — significa apenas pagar com o próprio cartão em nome de
outro.

### Consultar o status

`GET /api/v1/payments/status/<payment_id>` — público, lê o status da **reserva**
(atualizado pelo webhook), não a API do Mercado Pago.

`404` significa que o webhook ainda não chegou. Trate como *pendente*, nunca como
erro: dizer "falhou" para quem acabou de pagar é pior do que esperar.

Vale consultar também a reserva por `group_id`, que é a fonte autoritativa: ela
vira paga por webhook, por reprocessamento manual **ou no balcão**. Depender só
do `payment_id` deixa o cliente esperando por algo já quitado.

---

## 7. Erros que já foram cometidos

Lista literal, para ninguém repetir:

1. **Omitir `category_id` no `/simular`** → preço 30% acima do real, sem erro
   nenhum. Aconteceu duas vezes no mesmo projeto.
2. **Exibir `item.price` como "a partir de"** → número que o motor nunca cobra.
3. **Mandar `item_total` como `price_override`** → recusado por divergência
   assim que existe desconto.
4. **Calcular o total no navegador** → ignora tarifa do dia, desconto e taxa.
5. **Não filtrar `items` por `category_id`** → oferecer barraca em day use.
6. **Exibir R$ 0,01 no item Isento** → parece defeito do site.
7. **Tratar `resource_id: NULL` como dado faltando** → é o desenho do pool
   compartilhado.
8. **Confiar em `num_days`** → costuma vir `null` mesmo em item por diária.
9. **Reaproveitar `visible_to_bot` para esconder algo do site** → são canais
   diferentes; use `visible_to_web`.
10. **Redirecionar ao receber `pending`** → joga fora o QR code do PIX; o cliente
   fica com uma cobrança válida e sem como pagá-la.
11. **Carregar `i<id>` entre passos e esquecer `r<id>`** → a churrasqueira some
    do carrinho e a reserva nasce só com o ingresso.
12. **Criar sem `resource_id`** → o cliente reserva a A4 e recebe a A1.
13. **Esquecer `MP_NOTIFICATION_URL`** → o webhook cai em outro ambiente e a
    reserva nunca vira paga sozinha.

---

## Ver também

- `docs/reservas/README.md` — modelo de dados, endpoints internos, soft lock
- `docs/api_catalog.md` — catálogo de rotas
- `docs/reservas/REFACTORING_3TIER_INVENTORY.md` — modelo de inventário em 3 camadas
- `docs/configuracoes/README.md` — Calendário Global
