# Totem de Ponto Local — Guia de Produção

## Visão Geral

O `ponto_local.py` é um leitor de QR code interativo para máquinas locais (Windows/Linux) que se comunica com o servidor SISTUR via HTTPS.

> **Este guia cobre apenas o totem de QR code.** O outro programa desta pasta,
> `importador_ponto.py`, lê o relatório `.xls` do relógio de ponto da portaria e envia
> as batidas em lote — são coisas independentes. Veja `README_IMPORTADOR.md`.

## Configuração para Produção

### 1. Dependências

Todas as dependências já estão instaladas no venv:

```bash
c:\projetos\sistur\externo\.venv\Scripts\activate
pip list
```

**Importadas no topo de `ponto_local.py` — sem elas o script não sobe:**
- `opencv-python>=4.9.0` — leitura de câmera RTSP
- `pyzbar>=0.1.9` — decodificação de QR code
- `requests>=2.31.0` — comunicação HTTPS com API
- `certifi>=2024.0.0` — validação de certificados SSL (produção)
- `pygame>=2.5.2` — reprodução de áudio da saudação de voz
- `python-socketio>=5.11.0` (+ `python-engineio`, `websocket-client`) — WebSocket da saudação

**Importadas sob demanda — a falta só quebra o recurso correspondente:**
- `pillow>=10.0.0` — impressão gráfica em impressora térmica (`from PIL import ...`)
- `pywin32>=311` — impressão no Windows (`import win32print`)
- `edge-tts>=6.1.9` — geração da voz (`import edge_tts`)

> `pip install -r requirements.txt` já cobre todas exceto `pywin32`, que precisa de
> `pip install pywin32` à parte.

### 2. Variáveis de Ambiente (Produção)

**Obrigatórias:**
```bash
QR_SERVICE_TOKEN=<token configurado no servidor>
RTSP_URL=rtsp://<ip-da-camera>:554/user=<usuario>_password=<senha>_channel=0_stream=0&onvif=0.sdp?real_stream
FLASK_API_URL=https://administrativo.cachoeiradogirassol.com.br/api/internal/ponto/qr-scan
```

**Opcionais (com defaults):**
```bash
CAMERA_INDEX=0              # Índice da câmera USB (ignorado se usar RTSP)
DEBOUNCE_SECONDS=5          # Intervalo mínimo entre leituras do mesmo QR
```

### 3. Certificados SSL

**Produção:** O código usa `certifi.where()` para validar certificados SSL.

```python
resp = requests.post(
    FLASK_API_URL,
    json=body,
    headers=headers,
    timeout=REQUEST_TIMEOUT,
    verify=certifi.where(),  # ← Validação de certificado
)
```

**Como funciona:**
1. `certifi` fornece um bundle de certificados CA (Certificate Authorities)
2. `requests` valida o certificado do servidor contra este bundle
3. Se o certificado for inválido/expirado/não confiável → erro `SSLError`

**Se houver erro de certificado:**
```
[ERROR] Erro de SSL/TLS — certificado inválido ou não confiável: ...
```

**Soluções:**
1. Verifique se o certificado do servidor é válido (Let's Encrypt, DigiCert, etc.)
2. Verifique se a data/hora do computador está correta
3. Atualize o `certifi`: `pip install --upgrade certifi`

### 4. Executar em Produção

**Via `Start_Ponto.bat` (Windows):**
```
Clique 2x em: C:\Users\Paulo\Desktop\Start_Ponto.bat
```

Esperado:
```
[OK] Python encontrado
[OK] Ambiente virtual ja existe
[OK] pywin32 configurado
[OK] Dependencias instaladas
[OK] Token: configurado (hardcodado no ponto_local.py)

Iniciando leitor de QR code... (pressione Q na janela para sair)
```

**Via terminal (desenvolvimento):**
```bash
cd c:\projetos\sistur\externo
.venv\Scripts\activate
python ponto_local.py
```

### 5. Monitoramento

**Logs aparecerão no console:**

✅ **Sucesso:**
```
2026-03-31 10:15:23 [INFO] Enviando para API: https://administrativo...
2026-03-31 10:15:24 [INFO] SUCESSO — João Silva | 10:15:23 | entrada
```

❌ **Erro de conexão:**
```
[ERROR] Sem conexão com a API Flask (https://administrativo...)
```

❌ **Erro de SSL:**
```
[ERROR] Erro de SSL/TLS — certificado inválido ou não confiável: ...
```

❌ **Erro de token:**
```
[ERROR] API retornou 401: Unauthorized
```

### 6. Câmera RTSP

A URL vem da variável de ambiente `RTSP_URL` (obrigatória) — ela carrega o usuário e a
senha da câmera, por isso não fica no código:

```bash
RTSP_URL=rtsp://<ip-da-camera>:554/user=<usuario>_password=<senha>_channel=0_stream=0&onvif=0.sdp?real_stream
```

**Diagnóstico:**
```bash
# Teste conectividade RTSP com ffmpeg
ffmpeg -rtsp_transport tcp -i "$RTSP_URL" -t 5 -f null -
```

### 7. Impressora Térmica (Windows)

**Detecção automática:**
```python
impressora = _detectar_impressora()
```

Procura por:
- USB + térmica (prioridade 1)
- Apenas térmica (prioridade 2)
- Apenas USB (prioridade 3)
- Impressora padrão do Windows (fallback)

**Se não funcionar:**
1. Verifique se a impressora está conectada via USB
2. Verifique se o driver está instalado
3. Teste a impressão via "Impressoras e Dispositivos" do Windows

### 8. Troubleshooting

| Erro | Causa | Solução |
|---|---|---|
| `Sem conexão com a API Flask` | URL inválida ou servidor offline | Verifique `FLASK_API_URL` e conectividade de rede |
| `Erro de SSL/TLS` | Certificado inválido/expirado | `pip install --upgrade certifi` |
| `API retornou 401` | Token inválido | Verifique `QR_SERVICE_TOKEN` |
| `API retornou 422` | QR code inválido ou funcionário inativo | Escaneie um QR code válido |
| `Nenhuma impressora encontrada` | Impressora não detectada | Conecte via USB e reinstale drivers |
| `AssertionError pyzbar` | Captura de câmera corrompida | Normal em câmeras de baixa qualidade — ignorar |

### 9. Deployment em Kiosk

Para usar em um kiosk Linux com câmera USB:

1. Instale Python 3.11+ e OpenCV (headless):
   ```bash
   apt-get install python3.11 python3.11-venv libzbar0 libopencv-dev
   ```

2. Copie o `externo/` para a máquina:
   ```bash
   scp -r externo/ usuario@kiosk:/home/usuario/
   ```

3. Crie o venv e instale dependências:
   ```bash
   cd /home/usuario/externo
   python3.11 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

4. Configure variáveis de ambiente:
   ```bash
   export QR_SERVICE_TOKEN=...
   export FLASK_API_URL=https://administrativo.cachoeiradogirassol.com.br/...
   export CAMERA_INDEX=0
   ```

5. Execute:
   ```bash
   .venv/bin/python ponto_local.py
   ```

---

**Versão:** 1.0 (2026-03-31)  
**Mantido por:** Jordan Barbosa Machado  
**Projeto:** SISTUR — Cachoeira do Girassol
