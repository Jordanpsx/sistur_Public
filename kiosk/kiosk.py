#!/usr/bin/env python3
"""
Aplicação Kiosk Standalone para Ponto Eletrônico por QR Code.

Lê QR codes via câmera e envia o payload Fernet criptografado
ao servidor Flask via Bearer token HTTP.

Uso:
    python kiosk.py
    # ou
    python -m kiosk

Configuração:
    Crie um arquivo .env baseado em .env.example com:
    - API_URL: URL do endpoint do servidor
    - QR_SERVICE_TOKEN: Bearer token
    - CAMERA_INDEX: índice ou URL da câmera
    - DEBOUNCE_SECONDS: intervalo mínimo entre leituras
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime
from typing import Optional
from pathlib import Path

import cv2
import requests
from pyzbar import pyzbar
from dotenv import load_dotenv

# ============================================================================
# CONFIGURAÇÃO DE LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================================
# CARREGAMENTO DE VARIÁVEIS DE AMBIENTE
# ============================================================================

# Procura .env na mesma pasta do script ou em diretórios pai
def _localizar_env() -> Optional[Path]:
    """Localiza arquivo .env começando do diretório do script."""
    script_dir = Path(__file__).parent
    current = script_dir
    for _ in range(3):  # procura até 3 níveis acima
        env_file = current / ".env"
        if env_file.exists():
            return env_file
        current = current.parent
    return None


env_path = _localizar_env()
if env_path:
    logger.info(f"Carregando configuração de: {env_path}")
    load_dotenv(env_path)
else:
    logger.warning("Arquivo .env não encontrado. Usando variáveis de ambiente do sistema.")

# Configuração obrigatória
API_URL = os.getenv("API_URL")
QR_SERVICE_TOKEN = os.getenv("QR_SERVICE_TOKEN")

if not API_URL or not QR_SERVICE_TOKEN:
    logger.error("Erro: API_URL e QR_SERVICE_TOKEN são obrigatórias")
    logger.error("Configure o arquivo .env ou as variáveis de ambiente")
    sys.exit(1)

# Configuração opcional
try:
    CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
except ValueError:
    CAMERA_INDEX = os.getenv("CAMERA_INDEX", "0")  # URL RTSP

DEBOUNCE_SECONDS = int(os.getenv("DEBOUNCE_SECONDS", "5"))
SHOW_CAMERA_PREVIEW = os.getenv("SHOW_CAMERA_PREVIEW", "True").lower() in ("true", "1", "yes")

logger.info(f"API_URL: {API_URL}")
logger.info(f"CAMERA_INDEX: {CAMERA_INDEX}")
logger.info(f"DEBOUNCE_SECONDS: {DEBOUNCE_SECONDS}s")
logger.info(f"SHOW_CAMERA_PREVIEW: {SHOW_CAMERA_PREVIEW}")

# ============================================================================
# CLASSE KIOSK
# ============================================================================


class KioskQRCode:
    """Aplicação kiosk para leitura de QR code e envio ao servidor."""

    def __init__(
        self,
        api_url: str,
        qr_token: str,
        camera_index: int | str,
        debounce_seconds: int = 5,
        show_preview: bool = True,
    ):
        """Inicializa o kiosk.

        Args:
            api_url: URL completa do endpoint /api/internal/ponto/qr-scan
            qr_token: Bearer token para autenticação
            camera_index: Índice da câmera (int) ou URL RTSP (str)
            debounce_seconds: Intervalo mínimo entre leituras do mesmo QR
            show_preview: Se True, exibe preview da câmera (requer GUI)
        """
        self.api_url = api_url
        self.qr_token = qr_token
        self.camera_index = camera_index
        self.debounce_seconds = debounce_seconds
        self.show_preview = show_preview
        self.running = True
        self.last_read: dict[str, float] = {}  # {payload: timestamp}
        self.cap: Optional[cv2.VideoCapture] = None

    def _pode_registrar(self, qr_payload: str) -> bool:
        """Verifica se o QR pode ser registrado (respeita debounce)."""
        agora = time.time()
        ultima_leitura = self.last_read.get(qr_payload, 0)
        tempo_decorrido = agora - ultima_leitura

        if tempo_decorrido < self.debounce_seconds:
            return False

        self.last_read[qr_payload] = agora
        return True

    def _tentar_beep(self) -> None:
        """Toca beep se disponível (Windows)."""
        try:
            import winsound

            # Tom alto (900 Hz), duração de 200ms
            winsound.Beep(900, 200)
        except (ImportError, RuntimeError):
            pass  # winsound não disponível (Linux) ou acesso negado

    def _enviar_qr(self, qr_payload: str) -> dict:
        """Envia o payload QR ao servidor.

        Args:
            qr_payload: String Fernet criptografada (extraída do QR code)

        Returns:
            Dicionário com resposta do servidor ou erro.
        """
        headers = {
            "Authorization": f"Bearer {self.qr_token}",
            "Content-Type": "application/json",
        }
        data = {"qr_payload": qr_payload}

        try:
            resp = requests.post(self.api_url, json=data, headers=headers, timeout=10)
            return resp.json() if resp.text else {}
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro ao contatar servidor: {e}")
            return {"sucesso": False, "erro": str(e)}

    def _processar_resultado(self, resultado: dict) -> None:
        """Processa e exibe o resultado da batida."""
        if resultado.get("sucesso"):
            nome = resultado.get("nome", "Desconhecido")
            hora = resultado.get("hora", "??:??:??")
            tipo = resultado.get("tipo_batida", "?")

            # Formata tipo de batida em português
            tipo_map = {
                "clock_in": "entrada",
                "lunch_start": "início pausa",
                "lunch_end": "fim pausa",
                "clock_out": "saída",
                "extra": "extra",
            }
            tipo_br = tipo_map.get(tipo, tipo)

            logger.info(f"✓ {nome} — {tipo_br} às {hora}")
            self._tentar_beep()
        else:
            erro = resultado.get("erro", "Erro desconhecido")
            logger.warning(f"✗ Batida rejeitada: {erro}")

    def executar(self) -> None:
        """Loop principal de leitura de QR codes."""
        logger.info("Iniciando kiosk de ponto eletrônico...")
        logger.info("Pressione Ctrl+C para sair")

        # Configura tratamento de sinais para saída graciosa
        def handle_sigint(signum, frame):
            logger.info("\nEncerrando kiosk...")
            self.running = False

        signal.signal(signal.SIGINT, handle_sigint)

        # Abre câmera
        try:
            self.cap = cv2.VideoCapture(self.camera_index)
            if not self.cap.isOpened():
                logger.error(f"Erro ao abrir câmera: {self.camera_index}")
                sys.exit(1)

            # Configura resolução para melhor performance
            if isinstance(self.camera_index, int):
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

            logger.info("Câmera aberta com sucesso")
        except Exception as e:
            logger.error(f"Falha ao inicializar câmera: {e}")
            sys.exit(1)

        # Loop principal
        frame_count = 0
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                logger.error("Erro ao ler frame da câmera")
                break

            frame_count += 1

            # Exibe preview opcionalmente (a cada 10 frames para performance)
            if self.show_preview and frame_count % 10 == 0:
                # Redimensiona para exibição
                display = cv2.resize(frame, (640, 480))
                cv2.putText(
                    display,
                    "Apresente o QR code",
                    (50, 50),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 0),
                    2,
                )
                cv2.imshow("Kiosk Ponto Eletrônico", display)

            # Trata tecla ESC para fechar preview
            key = cv2.waitKey(1) & 0xFF
            if key == 27:  # ESC
                self.running = False

            # Decodifica QR codes no frame
            qr_codes = pyzbar.decode(frame)
            for qr_code in qr_codes:
                qr_payload = qr_code.data.decode("utf-8")

                # Verifica debounce
                if not self._pode_registrar(qr_payload):
                    continue

                logger.info(f"QR detectado (tamanho: {len(qr_payload)} bytes)")

                # Envia ao servidor
                resultado = self._enviar_qr(qr_payload)
                self._processar_resultado(resultado)

        # Limpeza
        if self.cap:
            self.cap.release()
        cv2.destroyAllWindows()
        logger.info("Kiosk encerrado")


# ============================================================================
# PONTO DE ENTRADA
# ============================================================================


def main():
    """Função principal."""
    kiosk = KioskQRCode(
        api_url=API_URL,
        qr_token=QR_SERVICE_TOKEN,
        camera_index=CAMERA_INDEX,
        debounce_seconds=DEBOUNCE_SECONDS,
        show_preview=SHOW_CAMERA_PREVIEW,
    )
    kiosk.executar()


if __name__ == "__main__":
    main()
