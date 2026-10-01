# Importador de Ponto — Guia de Uso

`importador_ponto.py` lê o relatório `.xls` exportado pelo relógio de ponto e envia
as batidas para o SISTUR. Roda na máquina que administra o relógio.

**O programa não tem acesso ao banco do SISTUR** e não consulta o cadastro de
funcionários. O vínculo entre cada usuário do relógio e o funcionário do sistema é
declarado à mão, uma vez, num CSV — é por isso que nenhum dado do cadastro passa
por este computador.

---

## Instalação

```bash
cd externo
pip install -r requirements.txt
```

A única dependência nova é `xlrd` (Python puro, sem dependências).

---

## Configuração

```bash
set QR_SERVICE_TOKEN=<mesmo token do totem>
set SISTUR_IMPORT_URL=https://administrativo.cachoeiradogirassol.com.br/api/internal/ponto/importar-lote
```

Opcionais:

| Variável | Default | Papel |
|---|---|---|
| `PONTO_DISPOSITIVO` | `REP-01` | Identificador do relógio, para o dia em que houver um segundo aparelho |
| `PONTO_TIMEZONE` | `America/Sao_Paulo` | Fuso do relógio. O horário vai como hora local e o servidor converte para UTC |
| `PONTO_MAPA` | `funcionarios.csv` | Caminho do arquivo de vínculo |

O caminho do arquivo de vínculo também aceita a flag `--mapa`, que por ser global vem
**antes** do subcomando:

```bash
python importador_ponto.py --mapa equipe_cozinha.csv mapear RegistroPresenca.xls
```

---

## Fluxo

### 1. Exportar o relatório do relógio

No aparelho, exporte o período desejado. **Até 31 dias por arquivo** — acima disso o
mesmo dia do mês apareceria duas vezes na planilha e não haveria como saber a qual
coluna cada batida pertence. O programa recusa arquivos assim.

### 2. Gerar o arquivo de vínculo

```bash
python importador_ponto.py mapear RegistroPresenca.xls
```

Cria `funcionarios.csv` com uma linha por pessoa do relógio:

```csv
nome_no_relogio,id_no_relogio,departamento,dias_com_batida,total_batidas,id_sistur
MARIA EXEMPLO,1,ADMIN,3,4,
joao exemplo,2,admin,10,22,
carlos,7,admin,12,39,
```

### 3. Preencher o `id_sistur` à mão

Abra o CSV no Excel. Para cada pessoa, consulte o ID do funcionário no SISTUR em
`/rh/funcionarios` e escreva na coluna `id_sistur`.

- **Linha em branco não é importada.** É assim que se deixa alguém de fora.
- `dias_com_batida` e `total_batidas` estão ali para você conferir o volume antes de
  aceitar. Um número fora do esperado é sinal de arquivo errado.
- Não mexa em `id_no_relogio` — é a chave que liga as duas pontas.

### 4. Conferir antes de enviar

```bash
python importador_ponto.py conferir RegistroPresenca.xls
```

Não acessa a rede. Mostra exatamente o que seria enviado:

```
  Período ...........: 2026-08-01 a 2026-08-16
  Dispositivo .......: REP-01
  Fuso do relógio ...: America/Sao_Paulo
  Pessoas no relógio : 13
  Batidas no arquivo : 296
  Batidas a enviar ..: 90

  SERÁ ENVIADO
    MARIA EXEMPLO          relógio #1   -> SISTUR #22      4 batidas  (2 dia(s) ímpar(es))
    carlos                 relógio #7   -> SISTUR #17     39 batidas  (5 dia(s) ímpar(es))

  NÃO SERÁ ENVIADO (id_sistur em branco)
    joao exemplo           relógio #2    22 batidas descartadas
```

### 5. Enviar

```bash
python importador_ponto.py enviar RegistroPresenca.xls
```

Mostra o mesmo resumo e pede confirmação. Use `--sim` para pular a pergunta em
execução automatizada.

---

## Nas exportações seguintes

Rode `mapear` de novo apontando para o novo `.xls`. **Os `id_sistur` já preenchidos
são mantidos** — o vínculo é buscado por `id_no_relogio`, e só as pessoas novas
aparecem em branco. O mapeamento é trabalho de uma vez só.

---

## Alerta de troca de titular

Se o `nome_no_relogio` de um `id_no_relogio` conhecido mudar entre uma exportação e
outra, o programa avisa:

```
[WARNING] ID 7 do relógio mudou de nome: 'carlos' -> 'OUTRA PESSOA'.
          Se o aparelho foi recadastrado para outra pessoa, CORRIJA o id_sistur —
          senão as batidas vão para a folha errada.
```

Isso acontece quando alguém apaga um usuário do aparelho e cria outro na mesma vaga.
O vínculo antigo continuaria válido e as horas iriam para a pessoa errada. **Leia esse
aviso quando ele aparecer.**

---

## O que o relatório do relógio não traz

| Fato | Consequência |
|---|---|
| Horários sem segundos (`hh:mm`) | Toda batida importada tem `:00` nos segundos |
| Sem tipo de batida | O SISTUR infere entrada / almoço / retorno / saída pela ordem |
| Sem CPF ou matrícula | Daí o arquivo de vínculo preenchido à mão |
| Célula vazia = sem batida | Não distingue falta de folga justificada |

Dias com número **ímpar** de batidas (alguém bateu a entrada e esqueceu a saída) são
marcados para revisão pelo SISTUR e ficam com saldo zero até serem corrigidos na
Folha de Ponto. O `conferir` conta quantos são antes do envio.

---

## O que o SISTUR faz com o lote

| Situação | O que acontece |
|---|---|
| Batida que já existe | **Pulada.** Reenviar o mesmo arquivo é seguro — nada é duplicado |
| Dia que passaria de 4 batidas | **O dia inteiro é descartado**, nem as 4 primeiras entram, e o RH recebe um aviso |
| Dia com número ímpar de batidas | Importado, mas marcado para revisão com saldo zero |
| `id_sistur` inexistente ou inativo | **O lote inteiro é recusado** e nada é gravado. Corrija o CSV e rode de novo |

O resumo volta no terminal:

```
[INFO] SUCESSO — 38 importadas, 0 ignoradas (já existiam), 6 dias em revisão.
```

Reenviar o mesmo arquivo depois disso mostra `0 importadas, 38 ignoradas` — é assim que
se confirma que a deduplicação está funcionando.

---

## Erros comuns

| Mensagem | Causa | Solução |
|---|---|---|
| `Biblioteca 'xlrd' não instalada` | Dependência faltando | `pip install -r requirements.txt` |
| `Arquivo .xls inválido ou corrompido` | Arquivo não é BIFF, ou está truncado | Reexporte do aparelho |
| `O cabeçalho não declara o período` | Exportação sem a linha "Data de presença" | Reexporte incluindo o cabeçalho |
| `Período de N dias é longo demais` | Mais de 31 dias no mesmo arquivo | Exporte em recortes menores |
| `Nenhum bloco 'IDUsuário:' encontrado` | Layout do relatório mudou | O parser precisa de ajuste |
| `id_sistur 'x' não é um número inteiro` | Digitação no CSV | Corrija a linha indicada |
| `QR_SERVICE_TOKEN não configurado` | Variável de ambiente ausente | Defina antes de rodar `enviar` |
| `Token recusado (401)` | Token errado | Confira contra o do totem |
| `Endpoint não encontrado (404)` | URL errada, ou SISTUR sem a rota | Confira `SISTUR_IMPORT_URL` |

---

**Mantido por:** Jordan Barbosa Machado
**Projeto:** SISTUR — Cachoeira do Girassol
