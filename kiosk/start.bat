@echo off
REM ============================================================================
REM Inicialização do Kiosk de Ponto Eletrônico (Windows)
REM ============================================================================
REM Este script:
REM 1. Verifica se .env existe (se não, copia de .env.example)
REM 2. Cria virtual environment se não existir
REM 3. Instala dependências
REM 4. Executa o kiosk

setlocal enabledelayedexpansion

echo.
echo ===============================================
echo  KIOSK PONTO ELETRONICO - INICIALIZACAO
echo ===============================================
echo.

REM Obtém o diretório do script
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM Verifica se .env existe
if not exist ".env" (
    echo [AVISO] Arquivo .env nao encontrado.
    if exist ".env.example" (
        echo Copiando .env.example para .env...
        copy .env.example .env >nul
        echo [OK] Arquivo .env criado. Por favor, edite-o com suas configuracoes.
        echo.
        pause
    ) else (
        echo [ERRO] Arquivo .env.example nao encontrado!
        pause
        exit /b 1
    )
)

REM Verifica se Python está instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao encontrado no PATH.
    echo Por favor, instale Python 3.8+ de https://www.python.org/
    pause
    exit /b 1
)

REM Cria virtual environment se não existir
if not exist "venv" (
    echo Criando ambiente virtual...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERRO] Falha ao criar virtual environment
        pause
        exit /b 1
    )
)

REM Ativa virtual environment
echo Ativando virtual environment...
call venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao ativar virtual environment
    pause
    exit /b 1
)

REM Instala dependências
echo.
echo Instalando dependencias (primeira execucao pode levar alguns minutos)...
pip install --upgrade pip >nul 2>&1
pip install -q -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao instalar dependencias
    pause
    exit /b 1
)

REM Executa kiosk
echo.
echo [OK] Tudo pronto!
echo.
python kiosk.py

pause
