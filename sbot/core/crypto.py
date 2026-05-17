"""敏感字段加密。

使用 Fernet 对称加密。密钥从环境变量读取,不写入代码或数据库。
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class Crypto:
    def __init__(self, key: str) -> None:
        # Fernet 要求 bytes 密钥
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("解密失败,可能加密密钥不匹配") from exc


def mask_secret(value: str, keep: int = 4) -> str:
    """打码敏感字段,保留前后各 keep 位。"""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"
