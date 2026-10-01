# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Importador de ponto — lê o relatório .xls do relógio e envia as batidas ao SISTUR.

Roda na máquina que administra o relógio de ponto. Não tem acesso ao banco do
SISTUR e não consulta o cadastro de funcionários: o vínculo entre o usuário do
relógio e o funcionário do sistema é declarado à mão, uma vez, num CSV.

Uso
---
    python importador_ponto.py mapear RegistroPresenca.xls
        Lê o relatório e cria (ou atualiza) funcionarios.csv, com uma linha por
        pessoa do relógio e a coluna id_sistur em branco.

    -> O operador abre funcionarios.csv e preenche id_sistur.

    python importador_ponto.py conferir RegistroPresenca.xls
        Mostra exatamente o que seria enviado. Não acessa a rede.

    python importador_ponto.py enviar RegistroPresenca.xls
        Envia as batidas dos funcionários mapeados para o SISTUR.

Variáveis de ambiente
---------------------
    QR_SERVICE_TOKEN    Token Bearer da API interna (o mesmo do totem).
    SISTUR_IMPORT_URL   Endpoint de importação em lote.
    PONTO_DISPOSITIVO   Identificador do relógio (default: REP-01).
    PONTO_TIMEZONE      Fuso do relógio (default: America/Sao_Paulo).
    PONTO_MAPA          Caminho do CSV de vínculo (default: funcionarios.csv).

Formato do relatório do relógio
-------------------------------
Cabeçalho com o período, seguido de um bloco de 3 linhas por funcionário:

    linha 2   | col Z: "Data de presença:01/08/2026~16/08/2026"
    linha N   | B:"IDUsuário:" D:<id> K:"Nome:" L:<nome> R:"Dep.:" S:<depto>
    linha N+1 | B..Q: dia do mês de cada coluna (1, 2, 3 ...)
    linha N+2 | B..Q: batidas em "HH:MM", várias por célula separadas por \\n
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------

QR_SERVICE_TOKEN = os.environ.get("QR_SERVICE_TOKEN", "")
SISTUR_IMPORT_URL = os.environ.get(
    "SISTUR_IMPORT_URL",
    "https://administrativo.cachoeiradogirassol.com.br/api/internal/ponto/importar-lote",
)
DISPOSITIVO = os.environ.get("PONTO_DISPOSITIVO", "REP-01")
TIMEZONE_RELOGIO = os.environ.get("PONTO_TIMEZONE", "America/Sao_Paulo")
ARQUIVO_MAPA = os.environ.get("PONTO_MAPA", "funcionarios.csv")

REQUEST_TIMEOUT = 60  # segundos — lote é maior que uma batida avulsa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("importador_ponto")

# Colunas fixas do relatório (base 0), conforme layout do aparelho.
_COL_ROTULO_ID = 1
_COL_ID = 3
_COL_NOME = 11
_COL_DEP = 18
_COL_DIA_INICIO = 1
_COL_DIA_FIM = 16

_RE_PERIODO = re.compile(r"(\d{2}/\d{2}/\d{4})\s*~\s*(\d{2}/\d{2}/\d{4})")
_RE_HORA = re.compile(r"^(\d{1,2}):(\d{2})$")

# Cabeçalho do CSV de vínculo. O nome vem primeiro porque é o que o operador lê;
# id_no_relogio é a chave real, já que o nome pode ser reeditado no aparelho.
CSV_COLUNAS = [
    "nome_no_relogio",
    "id_no_relogio",
    "departamento",
    "dias_com_batida",
    "total_batidas",
    "id_sistur",
]

CSV_INSTRUCOES = [
    "Vinculo entre os usuarios do relogio de ponto e os funcionarios do SISTUR.",
    "Preencha a coluna id_sistur com o ID do funcionario no SISTUR.",
    "Linha com id_sistur em branco NAO e importada.",
    "Rode 'mapear' de novo a cada exportacao: os IDs ja preenchidos sao mantidos.",
]


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------

@dataclass
class UsuarioRelogio:
    """Um usuário cadastrado no relógio, com as batidas que ele produziu."""

    id_no_relogio: str
    nome: str
    departamento: str
    batidas: list[datetime] = field(default_factory=list)

    @property
    def total_batidas(self) -> int:
        return len(self.batidas)

    @property
    def dias_com_batida(self) -> int:
        return len({b.date() for b in self.batidas})

    @property
    def dias_impares(self) -> int:
        """Dias com número ímpar de batidas — o SISTUR marca para revisão."""
        contagem: dict[date, int] = {}
        for batida in self.batidas:
            contagem[batida.date()] = contagem.get(batida.date(), 0) + 1
        return sum(1 for n in contagem.values() if n % 2)


@dataclass
class Relatorio:
    """Conteúdo completo de um relatório exportado pelo relógio."""

    periodo_inicio: date
    periodo_fim: date
    emitido_em: datetime | None
    usuarios: list[UsuarioRelogio] = field(default_factory=list)

    @property
    def total_batidas(self) -> int:
        return sum(u.total_batidas for u in self.usuarios)


# ---------------------------------------------------------------------------
# Leitura do .xls
# ---------------------------------------------------------------------------

def _sem_acento(texto: str) -> str:
    """Minúsculas sem acento — usado para achar rótulos na planilha."""
    normalizado = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in normalizado if not unicodedata.combining(c)).lower()


def _texto(valor) -> str:
    """Normaliza uma célula para texto, sem o '.0' que o xlrd põe em números."""
    if valor is None:
        return ""
    if isinstance(valor, float):
        return str(int(valor)) if valor.is_integer() else str(valor)
    return str(valor).strip()


def _extrair_periodo(grade: list[list[str]]) -> tuple[date, date]:
    """Localiza 'Data de presença:DD/MM/AAAA~DD/MM/AAAA' em qualquer célula."""
    for linha in grade:
        for celula in linha:
            if "presen" in celula.lower():
                achado = _RE_PERIODO.search(celula)
                if achado:
                    return (
                        datetime.strptime(achado.group(1), "%d/%m/%Y").date(),
                        datetime.strptime(achado.group(2), "%d/%m/%Y").date(),
                    )
    raise ValueError(
        "O cabeçalho não declara o período "
        "('Data de presença:DD/MM/AAAA~DD/MM/AAAA'). Sem ele não dá para saber "
        "a que mês cada coluna pertence."
    )


def _extrair_emissao(grade: list[list[str]]) -> datetime | None:
    """Lê a data de emissão do relatório, quando presente."""
    for linha in grade:
        for celula in linha:
            if "apresenta" in celula.lower():
                achado = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})", celula)
                if achado:
                    return datetime.strptime(
                        f"{achado.group(1)} {achado.group(2)}", "%d/%m/%Y %H:%M:%S"
                    )
    return None


def _mapa_dias(inicio: date, fim: date) -> dict[int, date]:
    """
    Resolve o dia do mês de cada coluna para uma data completa.

    A linha de dias do relatório traz apenas o dia (1, 2, ... 31). Caminhando
    pelo período do cabeçalho, cada dia do mês aparece uma única vez — inclusive
    quando o período atravessa a virada do mês (... 30, 31, 1, 2 ...).

    Raises:
        ValueError: Se o período passar de 31 dias, caso em que o mesmo dia do
            mês apareceria duas vezes e a coluna ficaria ambígua.
    """
    if (fim - inicio).days > 30:
        raise ValueError(
            f"Período de {(fim - inicio).days + 1} dias ({inicio} a {fim}) é longo "
            "demais: o mesmo dia do mês apareceria duas vezes e não há como saber "
            "a qual coluna cada batida pertence. Exporte em recortes de até 31 dias."
        )

    mapa: dict[int, date] = {}
    atual = inicio
    while atual <= fim:
        mapa.setdefault(atual.day, atual)
        atual += timedelta(days=1)
    return mapa


def _extrair_batidas(
    linha_dias: list[str], linha_batidas: list[str], mapa_dias: dict[int, date]
) -> list[datetime]:
    """Combina a linha de dias com a linha de horários numa lista de datetimes."""
    batidas: list[datetime] = []

    for coluna in range(_COL_DIA_INICIO, _COL_DIA_FIM + 1):
        dia_txt = linha_dias[coluna].strip() if coluna < len(linha_dias) else ""
        celula = linha_batidas[coluna].strip() if coluna < len(linha_batidas) else ""
        if not dia_txt or not celula:
            continue

        try:
            dia = int(float(dia_txt))
        except ValueError:
            continue

        data_do_dia = mapa_dias.get(dia)
        if data_do_dia is None:
            continue

        # Uma célula guarda todas as batidas do dia, uma por linha.
        for pedaco in celula.replace("\r", "\n").split("\n"):
            achado = _RE_HORA.match(pedaco.strip())
            if not achado:
                continue
            hora, minuto = int(achado.group(1)), int(achado.group(2))
            if hora > 23 or minuto > 59:
                continue
            batidas.append(
                datetime(data_do_dia.year, data_do_dia.month, data_do_dia.day, hora, minuto)
            )

    return batidas


def ler_relatorio(caminho: str) -> Relatorio:
    """
    Lê o relatório .xls exportado pelo relógio de ponto.

    Args:
        caminho: Caminho do arquivo .xls (Excel 97-2003 / BIFF).

    Returns:
        Relatorio com o período, os usuários do relógio e as batidas de cada um,
        ordenadas cronologicamente.

    Raises:
        ValueError: Se o arquivo não for um .xls legível, se o cabeçalho não
            declarar o período, ou se nenhum bloco de funcionário for encontrado.
    """
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError(
            "Biblioteca 'xlrd' não instalada. Rode: pip install -r requirements.txt"
        ) from exc

    try:
        workbook = xlrd.open_workbook(caminho)
    except Exception as exc:
        raise ValueError(f"Arquivo .xls inválido ou corrompido: {exc}") from exc

    if workbook.nsheets == 0:
        raise ValueError("O arquivo .xls não contém nenhuma planilha.")

    sheet = workbook.sheet_by_index(0)
    grade = [
        [_texto(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
        for r in range(sheet.nrows)
    ]

    inicio, fim = _extrair_periodo(grade)
    mapa = _mapa_dias(inicio, fim)
    usuarios: list[UsuarioRelogio] = []

    indice = 0
    while indice < len(grade):
        linha = grade[indice]
        rotulo = linha[_COL_ROTULO_ID] if len(linha) > _COL_ROTULO_ID else ""

        if "idusu" not in _sem_acento(rotulo):
            indice += 1
            continue

        id_no_relogio = linha[_COL_ID] if len(linha) > _COL_ID else ""
        nome = linha[_COL_NOME] if len(linha) > _COL_NOME else ""
        depto = linha[_COL_DEP] if len(linha) > _COL_DEP else ""

        linha_dias = grade[indice + 1] if indice + 1 < len(grade) else []
        linha_batidas = grade[indice + 2] if indice + 2 < len(grade) else []

        if id_no_relogio:
            usuarios.append(
                UsuarioRelogio(
                    id_no_relogio=id_no_relogio,
                    nome=nome,
                    departamento=depto,
                    batidas=sorted(_extrair_batidas(linha_dias, linha_batidas, mapa)),
                )
            )

        indice += 3

    if not usuarios:
        raise ValueError(
            "Nenhum bloco 'IDUsuário:' encontrado — o layout do relatório não "
            "corresponde ao esperado."
        )

    return Relatorio(
        periodo_inicio=inicio,
        periodo_fim=fim,
        emitido_em=_extrair_emissao(grade),
        usuarios=usuarios,
    )


# ---------------------------------------------------------------------------
# Arquivo de vínculo
# ---------------------------------------------------------------------------

@dataclass
class Mapa:
    """Vínculos lidos do CSV, com os problemas encontrados na leitura."""

    vinculos: dict[str, int] = field(default_factory=dict)
    nomes_anteriores: dict[str, str] = field(default_factory=dict)
    erros: list[str] = field(default_factory=list)

    @property
    def valido(self) -> bool:
        return not self.erros


def ler_mapa(caminho: str) -> Mapa:
    """
    Lê o CSV de vínculo preenchido pelo operador.

    Só as colunas `id_no_relogio` e `id_sistur` viram vínculo. Linha com
    `id_sistur` em branco é ignorada de propósito — é assim que se deixa alguém
    de fora da importação.

    Args:
        caminho: Caminho do CSV. Se não existir, devolve um Mapa vazio.

    Returns:
        Mapa com os vínculos, os nomes registrados na última geração (para
        detectar troca de titular de um ID) e os erros de formato.
    """
    mapa = Mapa()
    if not os.path.exists(caminho):
        return mapa

    with open(caminho, encoding="utf-8-sig", newline="") as arquivo:
        linhas = [
            linha
            for linha in arquivo.read().splitlines()
            if linha.strip() and not linha.lstrip().startswith("#")
        ]

    if not linhas:
        return mapa

    reader = csv.DictReader(linhas)
    colunas = set(reader.fieldnames or [])
    for obrigatoria in ("id_no_relogio", "id_sistur"):
        if obrigatoria not in colunas:
            mapa.erros.append(
                f"Coluna '{obrigatoria}' ausente em {caminho}. "
                "Apague o arquivo e rode 'mapear' de novo."
            )
    if mapa.erros:
        return mapa

    for numero, linha in enumerate(reader, start=2):
        id_relogio = (linha.get("id_no_relogio") or "").strip()
        if not id_relogio:
            continue

        mapa.nomes_anteriores[id_relogio] = (linha.get("nome_no_relogio") or "").strip()

        bruto = (linha.get("id_sistur") or "").strip()
        if not bruto:
            continue

        try:
            mapa.vinculos[id_relogio] = int(bruto)
        except ValueError:
            mapa.erros.append(
                f"Linha {numero}: id_sistur '{bruto}' não é um número inteiro."
            )

    duplicados: dict[int, list[str]] = {}
    for id_relogio, id_sistur in mapa.vinculos.items():
        duplicados.setdefault(id_sistur, []).append(id_relogio)
    for id_sistur, ids in duplicados.items():
        if len(ids) > 1:
            logger.warning(
                "IDs %s do relógio apontam para o mesmo funcionário (%s) — "
                "as batidas serão somadas.",
                ", ".join(sorted(ids)),
                id_sistur,
            )

    return mapa


def escrever_mapa(caminho: str, relatorio: Relatorio, mapa: Mapa) -> tuple[int, int]:
    """
    Grava o CSV de vínculo, preservando os IDs já preenchidos à mão.

    O vínculo é buscado por `id_no_relogio`, não por nome: o nome pode ser
    reeditado no aparelho sem que a pessoa mude.

    Args:
        caminho:   Caminho do CSV a gravar.
        relatorio: Relatório recém-lido do .xls.
        mapa:      Vínculos já existentes, vindos de uma geração anterior.

    Returns:
        Tupla (quantos já vinculados, quantos aguardando preenchimento).
    """
    preenchidos = 0

    with open(caminho, "w", encoding="utf-8-sig", newline="") as arquivo:
        for instrucao in CSV_INSTRUCOES:
            arquivo.write(f"# {instrucao}\n")

        writer = csv.DictWriter(arquivo, fieldnames=CSV_COLUNAS, lineterminator="\n")
        writer.writeheader()

        for usuario in relatorio.usuarios:
            id_sistur = mapa.vinculos.get(usuario.id_no_relogio)
            if id_sistur is not None:
                preenchidos += 1

            nome_anterior = mapa.nomes_anteriores.get(usuario.id_no_relogio)
            if nome_anterior and nome_anterior != usuario.nome:
                logger.warning(
                    "ID %s do relógio mudou de nome: '%s' -> '%s'. Se o aparelho "
                    "foi recadastrado para outra pessoa, CORRIJA o id_sistur — "
                    "senão as batidas vão para a folha errada.",
                    usuario.id_no_relogio,
                    nome_anterior,
                    usuario.nome,
                )

            writer.writerow(
                {
                    "nome_no_relogio": usuario.nome,
                    "id_no_relogio": usuario.id_no_relogio,
                    "departamento": usuario.departamento,
                    "dias_com_batida": usuario.dias_com_batida,
                    "total_batidas": usuario.total_batidas,
                    "id_sistur": id_sistur if id_sistur is not None else "",
                }
            )

    return preenchidos, len(relatorio.usuarios) - preenchidos


# ---------------------------------------------------------------------------
# Montagem e envio do lote
# ---------------------------------------------------------------------------

def montar_lote(relatorio: Relatorio, mapa: Mapa) -> tuple[list[dict], list[UsuarioRelogio]]:
    """
    Monta a lista de batidas a enviar, separando quem ficou sem vínculo.

    Args:
        relatorio: Relatório lido do .xls.
        mapa:      Vínculos lidos do CSV.

    Returns:
        Tupla (batidas prontas para envio, usuários sem vínculo).
    """
    batidas: list[dict] = []
    sem_vinculo: list[UsuarioRelogio] = []

    for usuario in relatorio.usuarios:
        id_sistur = mapa.vinculos.get(usuario.id_no_relogio)
        if id_sistur is None:
            if usuario.total_batidas:
                sem_vinculo.append(usuario)
            continue

        for batida in usuario.batidas:
            batidas.append(
                {
                    "funcionario_id": id_sistur,
                    "punch_time": batida.isoformat(timespec="seconds"),
                }
            )

    return batidas, sem_vinculo


def enviar_lote(relatorio: Relatorio, batidas: list[dict]) -> bool:
    """
    Envia o lote de batidas para o SISTUR.

    Os horários vão como hora local do relógio, acompanhados do fuso declarado —
    a conversão para UTC é feita no servidor, que é quem manda no relógio do
    sistema.

    Args:
        relatorio: Relatório lido, usado para declarar o período do lote.
        batidas:   Lista de {funcionario_id, punch_time} já resolvida.

    Returns:
        True se o servidor aceitou o lote.
    """
    try:
        import certifi
        import requests
    except ImportError:
        logger.error(
            "Bibliotecas 'requests' e 'certifi' não instaladas. "
            "Rode: pip install -r requirements.txt"
        )
        return False

    if not QR_SERVICE_TOKEN:
        logger.error(
            "QR_SERVICE_TOKEN não configurado. Defina a variável de ambiente "
            "antes de enviar."
        )
        return False

    corpo = {
        "dispositivo": DISPOSITIVO,
        "periodo_inicio": relatorio.periodo_inicio.isoformat(),
        "periodo_fim": relatorio.periodo_fim.isoformat(),
        "timezone": TIMEZONE_RELOGIO,
        "batidas": batidas,
    }

    logger.info("Enviando %d batidas para %s", len(batidas), SISTUR_IMPORT_URL)

    try:
        resposta = requests.post(
            SISTUR_IMPORT_URL,
            json=corpo,
            headers={"Authorization": f"Bearer {QR_SERVICE_TOKEN}"},
            timeout=REQUEST_TIMEOUT,
            verify=certifi.where(),
        )
    except requests.ConnectionError:
        logger.error("Sem conexão com o SISTUR (%s).", SISTUR_IMPORT_URL)
        return False
    except requests.Timeout:
        logger.error("Timeout ao enviar o lote — nada foi confirmado pelo servidor.")
        return False
    except requests.exceptions.SSLError as exc:
        logger.error("Erro de SSL/TLS — certificado inválido ou não confiável: %s", exc)
        return False
    except requests.RequestException as exc:
        logger.error("Erro na requisição: %s", exc)
        return False

    if resposta.status_code == 404:
        logger.error(
            "Endpoint não encontrado (404): %s. O SISTUR ainda não expõe a "
            "importação em lote — confirme a URL em SISTUR_IMPORT_URL.",
            SISTUR_IMPORT_URL,
        )
        return False

    if resposta.status_code == 401:
        logger.error("Token recusado (401). Confira o QR_SERVICE_TOKEN.")
        return False

    if resposta.status_code not in (200, 201):
        logger.error("SISTUR retornou %s: %s", resposta.status_code, resposta.text[:500])
        return False

    try:
        dados = resposta.json()
    except ValueError:
        logger.info("Lote aceito (HTTP %s).", resposta.status_code)
        return True

    logger.info(
        "SUCESSO — %s importadas, %s ignoradas (já existiam), %s dias em revisão.",
        dados.get("importadas", "?"),
        dados.get("duplicadas", "?"),
        dados.get("dias_em_revisao", "?"),
    )
    return True


# ---------------------------------------------------------------------------
# Relatório em tela
# ---------------------------------------------------------------------------

def _resumir(relatorio: Relatorio, mapa: Mapa) -> tuple[list[dict], list[UsuarioRelogio]]:
    """Imprime o resumo do que seria enviado e devolve o lote montado."""
    batidas, sem_vinculo = montar_lote(relatorio, mapa)

    print()
    print(f"  Período ...........: {relatorio.periodo_inicio} a {relatorio.periodo_fim}")
    print(f"  Dispositivo .......: {DISPOSITIVO}")
    print(f"  Fuso do relógio ...: {TIMEZONE_RELOGIO}")
    print(f"  Pessoas no relógio : {len(relatorio.usuarios)}")
    print(f"  Batidas no arquivo : {relatorio.total_batidas}")
    print(f"  Batidas a enviar ..: {len(batidas)}")
    print()

    vinculados = [u for u in relatorio.usuarios if u.id_no_relogio in mapa.vinculos]
    if vinculados:
        print("  SERÁ ENVIADO")
        for usuario in vinculados:
            marca = f"  ({usuario.dias_impares} dia(s) ímpar(es))" if usuario.dias_impares else ""
            print(
                f"    {usuario.nome:<22} relógio #{usuario.id_no_relogio:<3} "
                f"-> SISTUR #{mapa.vinculos[usuario.id_no_relogio]:<5} "
                f"{usuario.total_batidas:>3} batidas{marca}"
            )
        print()

    if sem_vinculo:
        print("  NÃO SERÁ ENVIADO (id_sistur em branco)")
        for usuario in sem_vinculo:
            print(
                f"    {usuario.nome:<22} relógio #{usuario.id_no_relogio:<3} "
                f"{usuario.total_batidas:>3} batidas descartadas"
            )
        print()

    impares = sum(u.dias_impares for u in vinculados)
    if impares:
        print(
            f"  Atenção: {impares} dia(s) com número ímpar de batidas entre os "
            "enviados.\n           O SISTUR marca esses dias para revisão e "
            "congela o saldo em zero\n           até alguém corrigir na Folha de Ponto."
        )
        print()

    return batidas, sem_vinculo


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def comando_mapear(args) -> int:
    """Lê o .xls e cria ou atualiza o CSV de vínculo."""
    relatorio = ler_relatorio(args.xls)
    mapa = ler_mapa(args.mapa)
    if not mapa.valido:
        for erro in mapa.erros:
            logger.error(erro)
        return 1

    preenchidos, pendentes = escrever_mapa(args.mapa, relatorio, mapa)

    logger.info("Período: %s a %s", relatorio.periodo_inicio, relatorio.periodo_fim)
    logger.info(
        "%d pessoas no relógio, %d batidas.",
        len(relatorio.usuarios),
        relatorio.total_batidas,
    )
    logger.info("Arquivo de vínculo: %s", os.path.abspath(args.mapa))
    logger.info("%d já vinculados, %d aguardando o id_sistur.", preenchidos, pendentes)

    if pendentes:
        print()
        print(f"  Abra {args.mapa} e preencha a coluna id_sistur das linhas em branco.")
        print("  Consulte o ID de cada funcionário no SISTUR, em /rh/funcionarios.")
        print("  Deixe em branco quem não deve ser importado.")
        print()
    return 0


def comando_conferir(args) -> int:
    """Mostra exatamente o que seria enviado, sem acessar a rede."""
    relatorio = ler_relatorio(args.xls)
    mapa = ler_mapa(args.mapa)
    if not mapa.valido:
        for erro in mapa.erros:
            logger.error(erro)
        return 1

    if not mapa.vinculos:
        logger.error(
            "Nenhum id_sistur preenchido em %s — não há o que enviar. "
            "Rode 'mapear' e preencha o arquivo.",
            args.mapa,
        )
        return 1

    batidas, _ = _resumir(relatorio, mapa)
    if not batidas:
        logger.error("Nenhuma batida a enviar.")
        return 1

    print("  Nada foi enviado. Para enviar de verdade:")
    print(f"    python {os.path.basename(__file__)} enviar {args.xls}")
    print()
    return 0


def comando_enviar(args) -> int:
    """Envia as batidas dos funcionários vinculados."""
    relatorio = ler_relatorio(args.xls)
    mapa = ler_mapa(args.mapa)
    if not mapa.valido:
        for erro in mapa.erros:
            logger.error(erro)
        return 1

    if not mapa.vinculos:
        logger.error(
            "Nenhum id_sistur preenchido em %s — não há o que enviar. "
            "Rode 'mapear' e preencha o arquivo.",
            args.mapa,
        )
        return 1

    batidas, _ = _resumir(relatorio, mapa)
    if not batidas:
        logger.error("Nenhuma batida a enviar.")
        return 1

    if not args.sim:
        resposta = input("  Confirma o envio? [s/N] ").strip().lower()
        if resposta not in ("s", "sim"):
            logger.info("Cancelado — nada foi enviado.")
            return 1

    return 0 if enviar_lote(relatorio, batidas) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa as batidas do relógio de ponto para o SISTUR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Fluxo:\n"
            "  1. mapear   -> cria funcionarios.csv com id_sistur em branco\n"
            "  2. (o operador preenche o id_sistur à mão)\n"
            "  3. conferir -> mostra o que será enviado\n"
            "  4. enviar   -> envia as batidas\n"
        ),
    )
    parser.add_argument(
        "--mapa",
        default=ARQUIVO_MAPA,
        help=f"CSV de vínculo (default: {ARQUIVO_MAPA}).",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_mapear = sub.add_parser("mapear", help="Cria ou atualiza o CSV de vínculo.")
    p_mapear.add_argument("xls", help="Relatório .xls exportado do relógio.")
    p_mapear.set_defaults(func=comando_mapear)

    p_conferir = sub.add_parser("conferir", help="Mostra o que seria enviado.")
    p_conferir.add_argument("xls", help="Relatório .xls exportado do relógio.")
    p_conferir.set_defaults(func=comando_conferir)

    p_enviar = sub.add_parser("enviar", help="Envia as batidas para o SISTUR.")
    p_enviar.add_argument("xls", help="Relatório .xls exportado do relógio.")
    p_enviar.add_argument(
        "--sim", action="store_true", help="Não pedir confirmação interativa."
    )
    p_enviar.set_defaults(func=comando_enviar)

    args = parser.parse_args()

    try:
        return args.func(args)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    except FileNotFoundError as exc:
        logger.error("Arquivo não encontrado: %s", exc.filename)
        return 1


if __name__ == "__main__":
    sys.exit(main())
