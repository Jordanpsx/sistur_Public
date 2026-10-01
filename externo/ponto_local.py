"""
Leitor de QR code para totem/kiosk local — modo interativo com janela de vídeo.

Adaptado de qr_service/qr_reader.py para uso em máquinas locais (Windows/Linux)
com suporte a câmera IP via RTSP, impressora térmica e sons de feedback.

Variáveis de ambiente:
    FLASK_API_URL      URL do endpoint (default: https://administrativo.cachoeiradogirassol.com.br/api/internal/ponto/qr-scan)
    QR_SERVICE_TOKEN   Token Bearer para autenticação na API interna
    RTSP_URL           URL do stream RTSP da câmera IP (inclui usuário e senha da câmera)
    CAMERA_INDEX       Índice da câmera USB (default: 0) — ignorado se usar RTSP
    DEBOUNCE_SECONDS   Intervalo mínimo entre leituras do mesmo QR (default: 5)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import certifi
import cv2
import pygame
import requests
import socketio as sio_lib
from pyzbar.pyzbar import decode as pyzbar_decode

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------

# URL da API — em produção, use apenas HTTPS com domínio público
# Para usar URL customizada, defina a variável de ambiente ANTES de iniciar o script:
#   set FLASK_API_URL=https://seu-dominio.com/api/internal/ponto/qr-scan
FLASK_API_URL = os.environ.get(
    "FLASK_API_URL",
    "https://administrativo.cachoeiradogirassol.com.br/api/internal/ponto/qr-scan",
)
QR_SERVICE_TOKEN = os.environ.get("QR_SERVICE_TOKEN", "")
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "5"))

# --- Câmera IP via RTSP ---
# Ex.: rtsp://<ip-da-camera>:554/user=<usuario>_password=<senha>_channel=0_stream=0&onvif=0.sdp?real_stream
RTSP_URL = os.environ.get("RTSP_URL", "")
# Resolução após recorte (câmera exibe duas imagens empilhadas verticalmente)
RESOLUCAO_PROCESSAMENTO = (960, 360)

# --- Impressora térmica ---
# Nome da impressora no Windows (ex: "EPSON TM-T20"). None = detectar automaticamente.
PRINTER_NAME = None
PAPER_WIDTH = 48  # 80mm ≈ 48 caracteres

THERMAL_PRINTER_KEYWORDS = [
    'pos', 'thermal', 'termica', 'receipt', 'ticket', 'cupom',
    'tm-t', 'tm-u', 'epson', 'elgin', 'bematech', 'daruma',
    'sweda', 'tanca', 'diebold', 'gertec', '80mm', '58mm',
    'usb', 'print', 'printer',
]

REQUEST_TIMEOUT = 10  # segundos

# Timezone padrão: Brasília (GMT-3)
TIMEZONE_BRASILIA = timezone(timedelta(hours=-3))

# --- WebSocket / Saudação de voz ---
SOCKETIO_SERVER_URL = "https://administrativo.cachoeiradogirassol.com.br"
TTS_VOICE = "pt-BR-AntonioNeural"
# Áudio gerado em memória — nenhum arquivo é salvo no disco

# Mapeamento de punch_type (vindo da API) para categoria de variações
PUNCH_TYPE_MAP: dict[str, str] = {
    "clock_in": "chegada",
    "lunch_start": "almoco_ini",
    "lunch_end": "almoco_fim",
    "clock_out": "saida",
    "extra": "chegada",  # extra fora de sequência → trata como chegada
}

# 20 variações por tipo de evento — {nome} é substituído em runtime
VARIACOES: dict[str, list[str]] = {
    "chegada": [
        "Bem-vindo, {nome}!", "Bom dia, {nome}! Bom trabalho hoje.", "Que bom te ver, {nome}! Vamos com tudo.",
        "Prontos para começar, {nome}?", "Olá, {nome}! Tenha um excelente turno.", "Bom ver você de novo, {nome}.",
        "Tenha um dia produtivo, {nome}!", "Bem-vindo ao posto, {nome}.", "Tudo certo para começar, {nome}? Bom dia!",
        "É um prazer ter você aqui hoje, {nome}.", "Vamos fazer um dia incrível, {nome}!", "Vamos lá, {nome}! Bom início de serviço.",
        "Bom dia! A casa é sua, {nome}.", "Começando com energia, {nome}?", "Alegria em te ver por aqui, {nome}.",
        "Preparado para o dia, {nome}?", "Mais um dia de sucesso para nós, {nome}!", "Olá, {nome}! Bom início de jornada.",
        "Seja bem-vindo, {nome}. Bom serviço!", "Que o seu dia seja excelente, {nome}!",
    ],
    "almoco_ini": [
        "Bom apetite, {nome}!", "Hora do descanso, {nome}. Aproveite!", "Bom almoço, {nome}!",
        "Aproveite bem o seu intervalo, {nome}.", "Hora de recarregar as energias, {nome}!", "Descanse bem, {nome}. Até já!",
        "Pausa merecida, {nome}. Bom almoço.", "Bom intervalo, {nome}!", "Hora da boia, {nome}! Aproveite.",
        "Recarregue as baterias, {nome}.", "Aproveite sua pausa de descanso, {nome}.", "Bom repouso e bom almoço, {nome}!",
        "Nos vemos daqui a pouco, {nome}.", "Bom apetite e bom descanso, {nome}.", "Desligue um pouco, {nome}. Bom almoço!",
        "Hora de comer algo bom, {nome}!", "Aproveite cada minuto do seu descanso, {nome}.", "Intervalo liberado, {nome}. Bom apetite!",
        "Bom almoço, {nome}! Volte renovado.", "Descanse e aproveite a refeição, {nome}.",
    ],
    "almoco_fim": [
        "Bem-vindo de volta, {nome}!", "Almoço foi bom? Bom retorno, {nome}!", "Recarregado, {nome}? Vamos nessa!",
        "De volta ao posto, {nome}. Bom trabalho.", "Pronto para a segunda etapa, {nome}?", "Bom te ver de volta à ativa, {nome}.",
        "Energias renovadas, {nome}? Bom serviço.", "Vamos concluir o dia com tudo, {nome}!", "Olá novamente, {nome}! Bom retorno.",
        "De volta à luta, {nome}!", "Vamos em frente, {nome}. Bom trabalho!", "Bem-vindo de volta ao time, {nome}.",
        "Pronto para mais um round, {nome}?", "Que bom que você voltou, {nome}. Vamos lá!", "Fim de pausa! Bom retorno ao posto, {nome}.",
        "Segunda metade do dia começando, {nome}!", "Tudo pronto para continuar, {nome}?", "Olá, {nome}! Vamos manter o ritmo.",
        "De volta ao trabalho, {nome}. Boa sorte!", "Bem-vindo! Vamos finalizar o dia bem, {nome}.",
    ],
    "saida": [
        "Bom descanso, {nome}!", "Até logo, {nome}! Valeu por hoje.", "Missão cumprida, {nome}. Bom descanso!",
        "Tenha uma ótima noite, {nome}.", "Valeu, {nome}! Até amanhã.", "Dia encerrado! Aproveite sua casa, {nome}.",
        "Obrigado pelo empenho de hoje, {nome}!", "Bom descanso e até a próxima, {nome}.", "Vá com cuidado e descanse, {nome}!",
        "Trabalho finalizado, {nome}. Até mais!", "Bom lazer e bom repouso, {nome}.", "Até amanhã, {nome}. Bom descanso!",
        "Valeu demais por hoje, {nome}!", "Horário batido! Bom descanso, {nome}.", "Aproveite sua folga, {nome}. Até logo!",
        "Você merece o descanso após hoje, {nome}.", "Fim de jornada, {nome}. Tudo de bom!", "Até breve, {nome}. Descanse bastante.",
        "Valeu pelo dia, {nome}. Vá em paz!", "Excelente descanso para você, {nome}!",
    ],
}

# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def _converter_para_brasilia(hora_str: str) -> str:
    """Converte hora de UTC para GMT-3 (Brasília).

    Args:
        hora_str: String com hora em formato HH:MM:SS (assumindo UTC da API).

    Returns:
        String com hora convertida para GMT-3 (Brasília).
    """
    try:
        # Parse da hora em UTC
        hora_utc = datetime.strptime(hora_str, "%H:%M:%S").replace(tzinfo=timezone.utc)

        # Converte para GMT-3 (Brasília)
        hora_brasilia = hora_utc.astimezone(timezone(timedelta(hours=-3)))

        # Retorna no mesmo formato
        return hora_brasilia.strftime("%H:%M:%S")
    except Exception as exc:
        logger.warning("Erro ao converter timezone: %s. Usando hora original.", exc)
        return hora_str

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ponto_local")

# ---------------------------------------------------------------------------
# Áudio — pygame não-bloqueante
# ---------------------------------------------------------------------------

def _inicializar_pygame() -> None:
    """Inicializa o mixer do pygame para reprodução de áudio não-bloqueante."""
    try:
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        logger.info("pygame.mixer inicializado.")
    except Exception as exc:
        logger.warning("Falha ao inicializar pygame.mixer: %s", exc)


def _reproduzir_buffer(buffer: io.BytesIO) -> None:
    """Carrega e reproduz MP3 de um buffer em memória sem bloquear.

    Args:
        buffer: Buffer BytesIO com dados MP3 gerados pelo edge-tts.
    """
    try:
        buffer.seek(0)
        pygame.mixer.music.load(buffer, "mp3")
        pygame.mixer.music.play()
    except Exception as exc:
        logger.warning("Erro ao reproduzir áudio da memória: %s", exc)


# ---------------------------------------------------------------------------
# TTS — edge-tts em memória (sem disco)
# ---------------------------------------------------------------------------

def _selecionar_frase(primeiro_nome: str, punch_type: str) -> str:
    """Seleciona aleatoriamente uma frase de saudação para o evento.

    Args:
        primeiro_nome: Primeiro nome do funcionário.
        punch_type: Tipo da batida vindo da API (ex: 'clock_in', 'lunch_start').

    Returns:
        Frase formatada pronta para TTS.
    """
    categoria = PUNCH_TYPE_MAP.get(punch_type, "chegada")
    template = random.choice(VARIACOES[categoria])
    return template.format(nome=primeiro_nome)


async def _gerar_tts_memoria(texto: str) -> io.BytesIO:
    """Gera áudio TTS em memória via edge-tts — nenhum arquivo é salvo em disco.

    Coleta os chunks de áudio do stream do edge-tts e retorna um BytesIO pronto
    para ser carregado diretamente pelo pygame.mixer.

    Args:
        texto: Texto a ser sintetizado.

    Returns:
        BytesIO com os bytes MP3 gerados.
    """
    import edge_tts
    buffer = io.BytesIO()
    communicate = edge_tts.Communicate(texto, voice=TTS_VOICE)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    buffer.seek(0)
    return buffer


def _processar_saudacao(primeiro_nome: str, punch_type: str) -> None:
    """Seleciona variação, gera TTS em memória e reproduz sem salvar nada em disco.

    Roda em thread separada para não bloquear o loop principal.

    Args:
        primeiro_nome: Primeiro nome do funcionário.
        punch_type: Tipo da batida (clock_in, lunch_start, lunch_end, clock_out).
    """
    frase = _selecionar_frase(primeiro_nome, punch_type)
    try:
        logger.info("TTS: gerando em memória — '%s'", frase)
        buffer = asyncio.run(_gerar_tts_memoria(frase))
        _reproduzir_buffer(buffer)
    except Exception as exc:
        logger.warning("Erro na saudação TTS para '%s': %s", primeiro_nome, exc)


# ---------------------------------------------------------------------------
# SocketIO — cliente WebSocket conectado ao servidor Flask
# ---------------------------------------------------------------------------

_sio = sio_lib.Client(logger=False, engineio_logger=False, reconnection=True, reconnection_attempts=0)


@_sio.event
def connect() -> None:
    """Callback: conexão WebSocket estabelecida com o servidor."""
    logger.info("SocketIO: conectado ao servidor VPS.")


@_sio.event
def disconnect() -> None:
    """Callback: conexão WebSocket encerrada."""
    logger.info("SocketIO: desconectado do servidor VPS.")


@_sio.on("employee_clocked_in")
def on_employee_clocked_in(data: dict) -> None:
    """Recebe evento de batida registrada e dispara saudação de voz.

    Args:
        data: Payload com 'first_name' e 'punch_type'.
    """
    primeiro_nome = data.get("first_name", "").strip()
    punch_type = data.get("punch_type", "clock_in").strip()
    if not primeiro_nome:
        return
    logger.info(
        "SocketIO: batida recebida — '%s' (%s). Iniciando saudação...",
        primeiro_nome, punch_type,
    )
    threading.Thread(
        target=_processar_saudacao,
        args=(primeiro_nome, punch_type),
        daemon=True,
        name=f"tts-{primeiro_nome}",
    ).start()


def _loop_socketio() -> None:
    """Mantém a conexão SocketIO ativa com reconexão automática.

    Roda em thread de background — nunca bloqueia o loop principal.
    """
    while True:
        try:
            logger.info("SocketIO: conectando a %s...", SOCKETIO_SERVER_URL)
            _sio.connect(
                SOCKETIO_SERVER_URL,
                transports=["polling", "websocket"],  # polling funciona sem config nginx
                wait_timeout=10,
            )
            _sio.wait()
        except Exception as exc:
            logger.warning("SocketIO: erro de conexão: %s. Tentando novamente em 10s...", exc)
            time.sleep(10)


# ---------------------------------------------------------------------------
# Sons de feedback (Windows: winsound — opcional)
# ---------------------------------------------------------------------------

def _tocar_beep_leitura() -> None:
    """Beep imediato ao detectar QR code."""
    try:
        import winsound
        winsound.MessageBeep(-1)
    except Exception:
        pass


def _tocar_beep_sucesso() -> None:
    """Beep de confirmação após registro com sucesso."""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def _tocar_beep_erro() -> None:
    """Beep de erro após falha no registro."""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Detecção e impressão térmica (Windows: win32print + Pillow — opcional)
# ---------------------------------------------------------------------------

def _listar_impressoras() -> list[dict]:
    """Lista impressoras instaladas e identifica as USB/térmicas.

    Returns:
        Lista de dicts com nome, porta, driver, is_usb, is_termica.
    """
    try:
        import win32print
        impressoras = []
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for printer in win32print.EnumPrinters(flags):
            nome = printer[2]
            try:
                handle = win32print.OpenPrinter(nome)
                info = win32print.GetPrinter(handle, 2)
                win32print.ClosePrinter(handle)
                porta = info.get("pPortName", "")
                driver = info.get("pDriverName", "")
            except Exception:
                porta = driver = ""
            impressoras.append({
                "nome": nome,
                "porta": porta,
                "driver": driver,
                "is_usb": "usb" in porta.lower() or "usb" in nome.lower(),
                "is_termica": any(
                    kw in nome.lower() or kw in driver.lower()
                    for kw in THERMAL_PRINTER_KEYWORDS
                ),
            })
        return impressoras
    except ImportError:
        return []
    except Exception:
        return []


def _detectar_impressora() -> str | None:
    """Detecta automaticamente uma impressora térmica USB.

    Prioridade: USB+térmica > térmica > USB > padrão do Windows.

    Returns:
        Nome da impressora selecionada, ou None se não encontrada.
    """
    if PRINTER_NAME:
        return PRINTER_NAME

    impressoras = _listar_impressoras()
    for prioridade in [
        lambda i: i["is_usb"] and i["is_termica"],
        lambda i: i["is_termica"],
        lambda i: i["is_usb"],
    ]:
        for imp in impressoras:
            if prioridade(imp):
                logger.info("Impressora detectada: %s", imp["nome"])
                return imp["nome"]

    try:
        import win32print
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def _imprimir_comprovante(dados: dict) -> None:
    """Imprime comprovante gráfico em impressora térmica 80mm via PIL.

    Tenta impressão gráfica (win32ui + Pillow); fallback para texto simples.

    Args:
        dados: dict com chaves nome, hora, tipo, data_completa.
    """
    nome = dados.get("nome", "Funcionário")
    hora = dados.get("hora", "--:--")
    tipo = dados.get("tipo", "registro")
    data_completa = dados.get("data_completa", "")

    try:
        import win32ui
        from PIL import Image, ImageDraw, ImageFont, ImageWin

        dpi = 203
        width_px = int(80 * dpi / 25.4)
        height_px = 450
        img = Image.new("1", (width_px, height_px), color=1)
        draw = ImageDraw.Draw(img)

        try:
            f_title = ImageFont.truetype("arial.ttf", 24)
            f_sub   = ImageFont.truetype("arial.ttf", 18)
            f_name  = ImageFont.truetype("arialbd.ttf", 28)
            f_time  = ImageFont.truetype("arialbd.ttf", 36)
            f_type  = ImageFont.truetype("arial.ttf", 22)
            f_foot  = ImageFont.truetype("arial.ttf", 16)
        except Exception:
            f_title = f_sub = f_name = f_time = f_type = f_foot = ImageFont.load_default()

        cx = width_px // 2
        y = 10

        def _centro(texto, fonte):
            bb = draw.textbbox((0, 0), texto, font=fonte)
            return cx - (bb[2] - bb[0]) // 2

        draw.line([(10, y), (width_px - 10, y)], fill=0, width=2); y += 15
        draw.text((_centro("CACHOEIRA DO GIRASSOL", f_title), y), "CACHOEIRA DO GIRASSOL", font=f_title, fill=0); y += 30
        draw.text((_centro("Controle de Ponto", f_sub), y), "Controle de Ponto", font=f_sub, fill=0); y += 35
        draw.line([(10, y), (width_px - 10, y)], fill=0, width=2); y += 25
        draw.text((_centro(nome, f_name), y), nome, font=f_name, fill=0); y += 50
        draw.text((_centro(hora, f_time), y), hora, font=f_time, fill=0); y += 50
        draw.text((_centro(tipo, f_type), y), tipo, font=f_type, fill=0); y += 40
        msg = "Batida registrada com sucesso!"
        draw.text((_centro(msg, f_foot), y), msg, font=f_foot, fill=0); y += 30
        draw.text((_centro(data_completa, f_foot), y), data_completa, font=f_foot, fill=0); y += 25
        draw.line([(10, y), (width_px - 10, y)], fill=0, width=2)
        img = img.crop((0, 0, width_px, y + 20))

        impressora = _detectar_impressora()
        if not impressora:
            logger.warning("Nenhuma impressora encontrada para impressão gráfica.")
            return

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(impressora)
        hdc.StartDoc("Comprovante de Ponto")
        hdc.StartPage()
        ImageWin.Dib(img).draw(hdc.GetHandleOutput(), (0, 0, img.width, img.height))
        hdc.EndPage()
        hdc.EndDoc()
        hdc.DeleteDC()
        logger.info("Comprovante impresso com sucesso (gráfico).")

    except ImportError:
        _imprimir_texto(dados)
    except Exception as exc:
        logger.warning("Erro na impressão gráfica: %s. Tentando texto simples.", exc)
        _imprimir_texto(dados)


def _imprimir_texto(dados: dict) -> None:
    """Fallback: imprime comprovante em modo texto RAW.

    Args:
        dados: dict com chaves nome, hora, tipo, data_completa.
    """
    try:
        import win32print

        nome = dados.get("nome", "Funcionário")
        hora = dados.get("hora", "--:--")
        tipo = dados.get("tipo", "registro")
        data_completa = dados.get("data_completa", "")
        linha = "=" * PAPER_WIDTH

        texto = (
            f"\n{linha}\n"
            f"{('CACHOEIRA DO GIRASSOL').center(PAPER_WIDTH)}\n"
            f"{'Controle de Ponto'.center(PAPER_WIDTH)}\n"
            f"{linha}\n\n"
            f"{nome.center(PAPER_WIDTH)}\n\n"
            f"{hora.center(PAPER_WIDTH)}\n"
            f"{tipo.center(PAPER_WIDTH)}\n\n"
            f"{'Batida registrada com sucesso!'.center(PAPER_WIDTH)}\n\n"
            f"{data_completa.center(PAPER_WIDTH)}\n"
            f"{linha}\n\n\n"
        )

        impressora = _detectar_impressora()
        if not impressora:
            logger.warning("Nenhuma impressora encontrada para impressão de texto.")
            return

        hprinter = win32print.OpenPrinter(impressora)
        try:
            job = win32print.StartDocPrinter(hprinter, 1, ("Comprovante Ponto", None, "RAW"))
            try:
                win32print.StartPagePrinter(hprinter)
                win32print.WritePrinter(hprinter, texto.encode("cp850"))
                win32print.EndPagePrinter(hprinter)
            finally:
                win32print.EndDocPrinter(hprinter)
        finally:
            win32print.ClosePrinter(hprinter)

        logger.info("Comprovante impresso com sucesso (texto).")

    except ImportError:
        logger.warning("win32print não instalado. Impressão ignorada.")
    except Exception as exc:
        logger.warning("Erro na impressão de texto: %s", exc)


# ---------------------------------------------------------------------------
# Comunicação com a API Flask
# ---------------------------------------------------------------------------

def _enviar_para_api(qr_payload: str) -> None:
    """Envia o payload do QR code para a API interna do Flask.

    Autentica via Bearer token (QR_SERVICE_TOKEN). Em caso de sucesso (HTTP 201),
    emite beep e imprime comprovante com os dados retornados pela API.

    Args:
        qr_payload: String Fernet criptografada lida do QR code.
    """
    headers = {"Authorization": f"Bearer {QR_SERVICE_TOKEN}"}
    body = {"qr_payload": qr_payload}

    logger.info("Enviando para API: %s", FLASK_API_URL)

    try:
        resp = requests.post(
            FLASK_API_URL,
            json=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            verify=certifi.where(),  # Validação de certificado SSL em produção
        )
    except requests.ConnectionError:
        logger.error("Sem conexão com a API Flask (%s).", FLASK_API_URL)
        _tocar_beep_erro()
        return
    except requests.Timeout:
        logger.error("Timeout ao conectar com a API Flask.")
        _tocar_beep_erro()
        return
    except requests.exceptions.SSLError as exc:
        logger.error("Erro de SSL/TLS — certificado inválido ou não confiável: %s", exc)
        _tocar_beep_erro()
        return
    except requests.RequestException as exc:
        logger.error("Erro na requisição: %s", exc)
        _tocar_beep_erro()
        return

    if resp.status_code == 201:
        data = resp.json()

        # Converte hora da API (UTC) para GMT-3 (Brasília)
        if "hora" in data:
            data["hora"] = _converter_para_brasilia(data["hora"])
        if "impressao" in data and "data_completa" in data["impressao"]:
            # Tenta converter data_completa também (formato DD/MM/YYYY HH:MM:SS)
            try:
                parts = data["impressao"]["data_completa"].split()
                if len(parts) == 2:
                    data_parte, hora_parte = parts
                    hora_convertida = _converter_para_brasilia(hora_parte)
                    data["impressao"]["data_completa"] = f"{data_parte} {hora_convertida}"
            except Exception:
                pass  # Se falhar, mantém original

        logger.info(
            "SUCESSO — %s | %s | %s",
            data.get("nome", "?"),
            data.get("hora", "?"),
            data.get("tipo_batida", "?"),
        )
        _tocar_beep_sucesso()
        # Impressão desativada por enquanto (sem impressora real)
        # if "impressao" in data:
        #     _imprimir_comprovante(data["impressao"])
    else:
        try:
            erro = resp.json().get("erro", resp.text)
        except Exception:
            erro = resp.text
        logger.warning("API retornou %d: %s", resp.status_code, erro)
        _tocar_beep_erro()


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def main() -> None:
    """Loop principal: abre câmera RTSP, exibe vídeo, detecta e processa QR codes.

    Inclui proteção contra travamento da câmera com timeout e reconexão automática.
    """
    if not QR_SERVICE_TOKEN:
        logger.error("QR_SERVICE_TOKEN não configurado. Defina a variável de ambiente e tente novamente.")
        sys.exit(1)
    if not RTSP_URL:
        logger.error("RTSP_URL não configurado. Defina a variável de ambiente e tente novamente.")
        sys.exit(1)

    logger.info("Iniciando totem de ponto...")
    logger.info("API: %s", FLASK_API_URL)
    logger.info("Stream RTSP: %s", RTSP_URL)

    # Inicializa pygame para reprodução de áudio não-bloqueante
    _inicializar_pygame()

    # Inicia cliente SocketIO em thread de background (não bloqueia câmera)
    threading.Thread(target=_loop_socketio, daemon=True, name="socketio-client").start()
    logger.info("SocketIO: thread de background iniciada.")

    # Detectar impressora na inicialização (DESATIVADO POR ENQUANTO)
    # impressora = _detectar_impressora()
    # if impressora:
    #     logger.info("Impressora selecionada: %s", impressora)
    # else:
    #     logger.warning("Nenhuma impressora detectada. Comprovantes não serão impressos.")
    logger.info("Detecção de impressora desativada. Comprovantes não serão impressos.")

    logger.info("Conectando ao stream RTSP...")
    cap = cv2.VideoCapture(RTSP_URL)

    # Configuração agressiva para evitar travamento
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Buffer mínimo — evita acúmulo de frames
    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)  # Timeout de conexão: 5s
    cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)  # Timeout de leitura: 5s
    cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)  # Desabilita autofocus (pode causar lag)

    if not cap.isOpened():
        logger.error("Não foi possível conectar ao stream RTSP: %s", RTSP_URL)
        logger.error("Verifique a URL e a conectividade de rede.")
        sys.exit(1)

    logger.info("Câmera aberta. Aguardando QR codes... (pressione 'q' para sair)")

    ultimo_scan: dict[str, float] = {}  # payload → timestamp da última leitura
    frames_sem_leitura = 0  # contador para detectar câmera travada
    max_frames_sem_leitura = 30  # ~0.5 segundos a 60 fps — reconecta rápido se câmera travar

    while True:
        # Lê frame com timeout implícito (cv2 tem timeout interno)
        ret, frame = cap.read()

        if not ret:
            frames_sem_leitura += 1
            logger.warning(f"Falha ao ler frame ({frames_sem_leitura}/{max_frames_sem_leitura})")

            # Se câmera está travada por muito tempo, reconecta
            if frames_sem_leitura > max_frames_sem_leitura:
                logger.error("Câmera não responde. Tentando reconectar...")
                cap.release()
                cv2.destroyAllWindows()
                time.sleep(1)  # Aguarda 1s antes de reconectar

                # Tenta reconectar com mesmas configurações
                cap = cv2.VideoCapture(RTSP_URL)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
                cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)

                if cap.isOpened():
                    logger.info("Reconectado ao stream RTSP.")
                    frames_sem_leitura = 0
                else:
                    logger.error("Falha ao reconectar. Tentando novamente em 3s...")
                    time.sleep(3)
                    frames_sem_leitura = 0  # Reset para tentar novamente

            # Verifica se pressionou 'q' mesmo sem frame
            if cv2.waitKey(100) & 0xFF == ord("q"):
                break
            continue

        # Reset do contador quando consegue ler um frame
        frames_sem_leitura = 0

        # Recorta metade inferior (câmera RTSP exibe duas imagens empilhadas)
        try:
            altura = frame.shape[0]
            frame = frame[altura // 2 :, :]
            frame = cv2.resize(frame, RESOLUCAO_PROCESSAMENTO, interpolation=cv2.INTER_LINEAR)
        except Exception as exc:
            logger.warning("Erro ao processar frame: %s. Ignorando...", exc)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            continue

        agora = time.monotonic()

        # Verifica se algum QR está em cooldown
        em_cooldown = any(
            (agora - t) < DEBOUNCE_SECONDS for t in ultimo_scan.values()
        )

        if em_cooldown:
            # Calcula o menor tempo restante entre todos os em cooldown
            restante = min(
                DEBOUNCE_SECONDS - (agora - t)
                for t in ultimo_scan.values()
                if (agora - t) < DEBOUNCE_SECONDS
            )
            cv2.putText(
                frame, f"Aguarde {restante:.1f}s", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2,
            )
        else:
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                qr_codes = pyzbar_decode(gray)

                for qr in qr_codes:
                    conteudo = qr.data.decode("utf-8", errors="replace")
                    ultimo_visto = ultimo_scan.get(conteudo)
                    if ultimo_visto and (agora - ultimo_visto) < DEBOUNCE_SECONDS:
                        continue

                    # Feedback visual no frame
                    x, y, w, h = qr.rect
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
                    cv2.putText(
                        frame, "REGISTRANDO...", (x, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2,
                    )

                    _tocar_beep_leitura()
                    _enviar_para_api(conteudo)
                    ultimo_scan[conteudo] = time.monotonic()
                    logger.info("Cooldown de %ds iniciado.", DEBOUNCE_SECONDS)
                    break  # processa um QR por frame
            except Exception as exc:
                logger.warning("Erro ao decodificar QR: %s. Ignorando...", exc)

        # Limpa entradas antigas do cache
        ultimo_scan = {
            k: v for k, v in ultimo_scan.items()
            if (agora - v) < DEBOUNCE_SECONDS * 10
        }

        try:
            cv2.imshow("Leitor Ponto — Cachoeira do Girassol", frame)
        except Exception:
            pass  # Ignora erros ao exibir (pode acontecer se janela for fechada)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    logger.info("Totem encerrado.")


if __name__ == "__main__":
    main()
