"""
Leitor de QR code para totem/kiosk — modo daemon Docker (headless).

Captura frames da câmera USB, decodifica QR codes via pyzbar,
e envia POST para a API interna do Flask registrando a batida de ponto.

Variáveis de ambiente:
    FLASK_API_URL      URL do endpoint (default: http://sistur-flask:5000/api/internal/ponto/qr-scan)
    QR_SERVICE_TOKEN   Token Bearer para autenticação na API interna
    CAMERA_INDEX       Índice da câmera (default: 0)
    DEBOUNCE_SECONDS   Intervalo mínimo entre leituras do mesmo QR (default: 5)
    FRAME_WIDTH        Largura do frame capturado (default: 640)
    FRAME_HEIGHT       Altura do frame capturado (default: 480)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

import cv2
import requests
from pyzbar.pyzbar import decode as pyzbar_decode

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------

FLASK_API_URL = os.environ.get(
    "FLASK_API_URL",
    "http://sistur-flask:5000/api/internal/ponto/qr-scan",
)
QR_SERVICE_TOKEN = os.environ.get("QR_SERVICE_TOKEN", "")
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "5"))
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "640"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "480"))

REQUEST_TIMEOUT = 10  # segundos

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("qr_reader")

# ---------------------------------------------------------------------------
# Controle de shutdown graceful
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum: int, _frame) -> None:
    """Trata SIGTERM/SIGINT para encerramento limpo do container Docker."""
    global _running
    logger.info("Sinal %s recebido. Encerrando...", signal.Signals(signum).name)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Debounce — evita leituras repetidas do mesmo QR
# ---------------------------------------------------------------------------

_last_seen: dict[str, float] = {}


def _is_debounced(payload: str) -> bool:
    """Verifica se o payload já foi lido dentro da janela de debounce.

    Args:
        payload: String do QR code lido.

    Returns:
        True se deve ignorar (dentro da janela), False se deve processar.
    """
    now = time.monotonic()
    last = _last_seen.get(payload)
    if last is not None and (now - last) < DEBOUNCE_SECONDS:
        return True
    _last_seen[payload] = now
    return False


def _cleanup_debounce() -> None:
    """Remove entradas antigas do cache de debounce para evitar crescimento ilimitado."""
    now = time.monotonic()
    stale = [k for k, v in _last_seen.items() if (now - v) > DEBOUNCE_SECONDS * 10]
    for k in stale:
        del _last_seen[k]


# ---------------------------------------------------------------------------
# Processamento de frame
# ---------------------------------------------------------------------------

def process_frame(frame):
    """Converte o frame para escala de cinza e reduz resolução para performance.

    Args:
        frame: Frame BGR capturado pela câmera (numpy array).

    Returns:
        Lista de objetos decodificados pelo pyzbar, ou lista vazia.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Reduz resolução pela metade para decodificação mais rápida
    h, w = gray.shape[:2]
    small = cv2.resize(gray, (w // 2, h // 2), interpolation=cv2.INTER_AREA)

    return pyzbar_decode(small)


# ---------------------------------------------------------------------------
# Comunicação com a API Flask
# ---------------------------------------------------------------------------

def send_to_api(qr_payload: str) -> None:
    """Envia o payload do QR code para a API interna do Flask.

    Args:
        qr_payload: String Fernet criptografada lida do QR code.
    """
    headers = {"Authorization": f"Bearer {QR_SERVICE_TOKEN}"}
    body = {"qr_payload": qr_payload}

    try:
        resp = requests.post(
            FLASK_API_URL,
            json=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.ConnectionError:
        logger.error("Sem conexão com a API Flask (%s).", FLASK_API_URL)
        return
    except requests.Timeout:
        logger.error("Timeout ao conectar com a API Flask.")
        return
    except requests.RequestException as exc:
        logger.error("Erro na requisição: %s", exc)
        return

    if resp.status_code == 201:
        data = resp.json()
        logger.info(
            "SUCESSO — %s | %s | %s",
            data.get("nome", "?"),
            data.get("hora", "?"),
            data.get("tipo_batida", "?"),
        )
    else:
        try:
            erro = resp.json().get("erro", resp.text)
        except Exception:
            erro = resp.text
        logger.warning("API retornou %d: %s", resp.status_code, erro)


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def main() -> None:
    """Loop principal: captura frames, decodifica QR codes, envia para API."""
    if not QR_SERVICE_TOKEN:
        logger.error("QR_SERVICE_TOKEN não configurado. Encerrando.")
        sys.exit(1)

    logger.info("Iniciando leitor de QR code...")
    logger.info("API: %s", FLASK_API_URL)
    logger.info("Câmera: %d | Resolução: %dx%d | Debounce: %ds",
                CAMERA_INDEX, FRAME_WIDTH, FRAME_HEIGHT, DEBOUNCE_SECONDS)

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        logger.error("Não foi possível abrir a câmera %d.", CAMERA_INDEX)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    logger.info("Câmera aberta. Aguardando QR codes...")

    scan_count = 0
    last_heartbeat = time.monotonic()

    try:
        while _running:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Falha ao capturar frame. Tentando novamente...")
                time.sleep(1)
                continue

            decoded = process_frame(frame)

            for obj in decoded:
                qr_data = obj.data.decode("utf-8", errors="replace")

                if _is_debounced(qr_data):
                    continue

                logger.info("QR code detectado. Enviando para API...")
                send_to_api(qr_data)
                scan_count += 1

            # Heartbeat a cada 60 segundos
            now = time.monotonic()
            if (now - last_heartbeat) >= 60:
                logger.info("Heartbeat — câmera ativa, %d leituras no último minuto.", scan_count)
                scan_count = 0
                last_heartbeat = now
                _cleanup_debounce()

            # Pequeno delay para não sobrecarregar a CPU
            time.sleep(0.03)  # ~30 FPS

    finally:
        cap.release()
        logger.info("Câmera liberada. Leitor encerrado.")


if __name__ == "__main__":
    main()
