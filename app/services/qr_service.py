# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
QRService — geração e validação de QR codes de funcionários.

O payload do QR code é criptografado com Fernet (AES-128-CBC + HMAC-SHA256)
e contém: id, nome e token_qr do funcionário.

A chave Fernet é derivada de QR_SECRET_KEY via SHA-256 + base64url, de modo
que nenhuma dependência extra de configuração é necessária em ambientes que
já definem SECRET_KEY.

Regras de arquitetura:
    - Sem importações Flask — puro Python, compatível com Cython.
    - Nenhuma mutação de banco de dados neste serviço — apenas geração de imagem.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid


def _derivar_chave_fernet(raw_key: str) -> bytes:
    """Deriva uma chave Fernet válida (32 bytes base64url) a partir de uma string arbitrária.

    Args:
        raw_key: String de chave de origem (QR_SECRET_KEY ou SECRET_KEY).

    Returns:
        Bytes da chave Fernet pronta para uso.
    """
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class QRService:
    """Geração de QR codes criptografados para autenticação de ponto eletrônico.

    Todos os métodos são estáticos — sem estado de instância — compatível com Cython.
    """

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    @staticmethod
    def gerar_token() -> str:
        """Gera um UUID v4 único para identificar o funcionário no QR code.

        Returns:
            String UUID v4 (ex: '550e8400-e29b-41d4-a716-446655440000').
        """
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Criptografia
    # ------------------------------------------------------------------

    @staticmethod
    def criptografar_payload(
        funcionario_id: int,
        nome: str,
        token_qr: str,
        qr_secret_key: str,
    ) -> str:
        """Serializa e criptografa o payload do QR code.

        O payload JSON é cifrado com Fernet. O resultado é uma string
        base64url segura para embedding em QR codes.

        Args:
            funcionario_id: ID do funcionário.
            nome:           Nome completo do funcionário.
            token_qr:       UUID token único armazenado em Funcionario.token_qr.
            qr_secret_key:  Valor de Config.QR_SECRET_KEY passado pela rota.

        Returns:
            String Fernet criptografada (base64url) com o payload.

        Raises:
            RuntimeError: Se a biblioteca cryptography não estiver instalada.
        """
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise RuntimeError(
                "Biblioteca 'cryptography' não instalada. Execute: pip install cryptography"
            ) from exc

        chave = _derivar_chave_fernet(qr_secret_key)
        fernet = Fernet(chave)

        payload = json.dumps(
            {"id": funcionario_id, "nome": nome, "token": token_qr},
            ensure_ascii=False,
        ).encode("utf-8")

        return fernet.encrypt(payload).decode("utf-8")

    @staticmethod
    def descriptografar_payload(
        token_criptografado: str,
        qr_secret_key: str,
        max_age_seconds: int = 0,
    ) -> dict:
        """Descriptografa e valida o payload do QR code.

        Args:
            token_criptografado: String Fernet recebida pelo scanner.
            qr_secret_key:       Valor de Config.QR_SECRET_KEY.
            max_age_seconds:     Se > 0, rejeita tokens mais antigos que N segundos.
                                 0 = sem expiração (padrão — QR code permanente).

        Returns:
            Dict com campos: id, nome, token.

        Raises:
            ValueError: Se o token for inválido, adulterado ou expirado.
        """
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:
            raise RuntimeError("Biblioteca 'cryptography' não instalada.") from exc

        chave = _derivar_chave_fernet(qr_secret_key)
        fernet = Fernet(chave)

        try:
            kwargs = {"ttl": max_age_seconds} if max_age_seconds > 0 else {}
            payload_bytes = fernet.decrypt(token_criptografado.encode("utf-8"), **kwargs)
        except InvalidToken as exc:
            raise ValueError("QR code inválido ou expirado.") from exc

        return json.loads(payload_bytes.decode("utf-8"))

    # ------------------------------------------------------------------
    # Geração de imagem
    # ------------------------------------------------------------------

    @staticmethod
    def gerar_imagem_bytes(
        funcionario_id: int,
        nome: str,
        token_qr: str,
        qr_secret_key: str,
    ) -> bytes:
        """Gera o QR code como PNG em bytes prontos para servir via HTTP.

        O conteúdo do QR code é o payload Fernet criptografado.
        Tamanho: 300×300 px, fundo branco, módulos pretos.

        Args:
            funcionario_id: ID do funcionário.
            nome:           Nome completo do funcionário.
            token_qr:       UUID token único armazenado em Funcionario.token_qr.
            qr_secret_key:  Valor de Config.QR_SECRET_KEY.

        Returns:
            Bytes PNG da imagem do QR code.

        Raises:
            RuntimeError: Se qrcode[pil] ou Pillow não estiverem instalados.
        """
        try:
            import qrcode
            from qrcode.image.pil import PilImage
        except ImportError as exc:
            raise RuntimeError(
                "Biblioteca 'qrcode[pil]' não instalada. Execute: pip install 'qrcode[pil]'"
            ) from exc

        conteudo = QRService.criptografar_payload(
            funcionario_id=funcionario_id,
            nome=nome,
            token_qr=token_qr,
            qr_secret_key=qr_secret_key,
        )

        qr = qrcode.QRCode(
            version=None,        # auto-tamanho
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(conteudo)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white", image_factory=PilImage)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()
