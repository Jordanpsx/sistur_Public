# Kiosk de Ponto Eletrônico — SISTUR

Aplicação standalone para leitura de QR codes e registro de batidas de ponto.
Funciona em Windows e Linux, com ou sem Docker.

## Visão geral

- **Câmera:** USB webcam ou RTSP IP camera
- **QR code:** Lê diretamente o payload Fernet criptografado contido no QR
- **Autenticação:** Bearer token via header HTTP
- **Servidor:** URL configurável (teste ou produção)
- **Resposta:** Exibe nome do funcionário, tipo de batida e horário no console

## Configuração Rápida

### Windows

1. Clone ou download este repositório
2. Duplo-clique em `start.bat`
3. Na primeira execução:
   - Cria virtual environment Python
   - Instala `opencv-python`, `pyzbar`, `requests`, `python-dotenv`
   - Copia `.env.example` para `.env`
4. Edite `.env` com:
   - `API_URL` (teste): `https://test-admin.cachoeiradogirassol.com.br/api/internal/ponto/qr-scan`
   - `QR_SERVICE_TOKEN` (fornecido pelo administrador)
5. Duplo-clique em `start.bat` novamente para rodar

### Linux / Kiosk

```bash
# Primeiro acesso
cd kiosk
./start.sh

# Nas próximas vezes
./start.sh
```

Na primeira execução, o script:
- Instala `python3-venv`, `libzbar0`, `libgl1-mesa-glx` (via `apt-get`)
- Cria virtual environment
- Instala dependências Python
- Copia `.env.example` para `.env`

Edite `.env` com suas credenciais antes da segunda execução.

## Arquivos

| Arquivo | Descrição |
|---------|-----------|
| `kiosk.py` | Aplicação principal (leitura QR + envio HTTP) |
| `requirements.txt` | Dependências Python |
| `.env.example` | Template de configuração (copiar para `.env`) |
| `.env` | Arquivo de configuração (NÃO faça commit) |
| `start.bat` | Launcher Windows (recomendado) |
| `start.sh` | Launcher Linux (executar com `chmod +x start.sh`) |
| `README.md` | Este arquivo |

## Configuração (`.env`)

```env
# URL do servidor (teste ou produção)
API_URL=https://test-admin.cachoeiradogirassol.com.br/api/internal/ponto/qr-scan

# Token Bearer de autenticação (obtém com administrador)
QR_SERVICE_TOKEN=seu_token_aqui

# Câmera: índice inteiro (0 = webcam padrão) ou URL RTSP
# Exemplos:
#   CAMERA_INDEX=0                    # Webcam padrão
#   CAMERA_INDEX=1                    # Segunda câmera USB
#   CAMERA_INDEX=rtsp://<ip-da-camera>:554/...  # IP camera
CAMERA_INDEX=0

# Intervalo mínimo (segundos) entre leituras do mesmo QR
# Evita duplos-cliques acidentais
DEBOUNCE_SECONDS=5

# Exibir preview da câmera (True/False)
SHOW_CAMERA_PREVIEW=True
```

## Uso

Após configuração, execute:

**Windows:** Duplo-clique em `start.bat`
**Linux:** `./start.sh` ou `python kiosk.py` (se já estiver em venv)

### Loop de batida

1. Aplicação abre câmera e exibe mensagem "Apresente o QR code"
2. Aproxime o QR code (impresso ou em tela) da câmera
3. Após detecção e envio bem-sucedido:
   - Console exibe: `✓ Nome do Funcionário — entrada às 14:35:22`
   - Windows: toca beep (se `winsound` disponível)
4. Aguarda 5 segundos (debounce) antes de aceitar o mesmo QR novamente

### Saída do console

```
2026-03-05 14:35:00 [INFO   ] Iniciando kiosk de ponto eletrônico...
2026-03-05 14:35:00 [INFO   ] Câmera aberta com sucesso
2026-03-05 14:35:05 [INFO   ] QR detectado (tamanho: 256 bytes)
2026-03-05 14:35:05 [INFO   ] ✓ João da Silva — entrada às 14:35:22
2026-03-05 14:35:12 [INFO   ] QR detectado (tamanho: 256 bytes)
2026-03-05 14:35:12 [INFO   ] ✓ João da Silva — saída às 14:35:42
```

## Geração de QR Code

Cada funcionário tem um QR code único gerado no portal (`/rh/funcionarios/<id>/qr`).

**O QR contém:**
- ID do funcionário
- Nome (para confirmação visual)
- Token Fernet criptografado

**Para gerar um novo QR:**
1. Abra o portal em `/rh/funcionarios/`
2. Clique no funcionário desejado
3. Aba "QR Code" → "Gerar novo QR"
4. Imprima ou escaneie a tela

**Revogação:** Gerar um novo QR automaticamente invalida todos os anteriores (token é salvo no BD).

## Solução de problemas

### Câmera não funciona

**Erro:** `Erro ao abrir câmera: 0`

- Windows: Verifique se outra aplicação (Zoom, Teams) não está usando a câmera
- Linux: Verifique permissões — execute com `sudo` se necessário
- Tente `CAMERA_INDEX=1` ou `CAMERA_INDEX=2` no `.env`

**Diagnóstico:**

```bash
# Linux
ls /dev/video*
```

### QR não é detectado

- Certifique-se que o QR está claro e bem iluminado
- Não reutilize QRs impressos há muitos meses (tinta desbota)
- Regenere um QR novo no portal: `/rh/funcionarios/<id>/qr`

### Erro de autenticação

**Erro:** `401 Unauthorized`

- Verifique se `QR_SERVICE_TOKEN` está correto no `.env`
- Confirme com o administrador que o token foi emitido para a máquina

**Erro:** `422 Unprocessable Entity — QR code revogado`

- O QR foi revogado (novo QR foi gerado no portal)
- Gere um novo QR em `/rh/funcionarios/<id>/qr` e imprima

### Servidor de teste não responde

- Verifique Internet e DNS
- Teste em browser: `https://test-admin.cachoeiradogirassol.com.br/`
- Confirme se servidor está online com administrador

## Licença

SISTUR — © Pousada Cachoeira do Girassol
