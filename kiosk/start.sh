#!/bin/bash
# ============================================================================
# Inicialização do Kiosk de Ponto Eletrônico (Linux)
# ============================================================================
# Este script:
# 1. Verifica se .env existe (se não, copia de .env.example)
# 2. Cria virtual environment se não existir
# 3. Instala dependências (incluindo bibliotecas do sistema)
# 4. Executa o kiosk

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "==============================================="
echo "  KIOSK PONTO ELETRONICO - INICIALIZACAO"
echo "==============================================="
echo ""

# Verifica se .env existe
if [ ! -f ".env" ]; then
    echo "[AVISO] Arquivo .env não encontrado."
    if [ -f ".env.example" ]; then
        echo "Copiando .env.example para .env..."
        cp .env.example .env
        echo "[OK] Arquivo .env criado. Por favor, edite-o com suas configurações."
        echo ""
        exit 0
    else
        echo "[ERRO] Arquivo .env.example não encontrado!"
        exit 1
    fi
fi

# Verifica se Python está instalado
if ! command -v python3 &> /dev/null; then
    echo "[ERRO] Python 3 não encontrado no PATH."
    echo "Para Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "Para Fedora/RHEL: sudo dnf install python3 python3-pip"
    exit 1
fi

# Instala bibliotecas do sistema se não estão presentes (Ubuntu/Debian)
if command -v apt-get &> /dev/null; then
    echo "Verificando bibliotecas do sistema..."
    for pkg in libzbar0 libgl1-mesa-glx libglib2.0-0; do
        if ! dpkg -l | grep -q "^ii  $pkg"; then
            echo "  Instalando $pkg..."
            sudo apt-get update -qq
            sudo apt-get install -y -qq "$pkg"
        fi
    done
fi

# Cria virtual environment se não existir
if [ ! -d "venv" ]; then
    echo "Criando ambiente virtual..."
    python3 -m venv venv
fi

# Ativa virtual environment
echo "Ativando virtual environment..."
source venv/bin/activate

# Instala dependências
echo ""
echo "Instalando dependências (primeira execução pode levar alguns minutos)..."
pip install --upgrade pip -q
pip install -q -r requirements.txt

echo ""
echo "[OK] Tudo pronto!"
echo ""

# Executa kiosk
python kiosk.py
