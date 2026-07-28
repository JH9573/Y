"""日志脱敏。

操作日志的 detail 里常常直接塞了第三方接口的异常原文,可能带上 token、
密钥、带口令的 URL。日志表是明文的,写库前统一在这里过一遍。

原则:只打码「看得出是凭据」的部分,保留其余上下文——日志的用途是排查问题,
整段抹掉就没意义了。
"""
from __future__ import annotations

import re

MASK = "***"

# key=value / "key": "value" / key is value 形式的凭据字段。
# 第 1 组把「字段名 + 分隔符 + 可能的引号」原样留下,只替换后面的值。
_KEYED = re.compile(
    r"(?i)(\b(?:"
    r"api[_-]?key|api[_-]?token|access[_-]?key[_-]?secret|access[_-]?key[_-]?id|"
    r"secret[_-]?key|secret[_-]?id|auth[_-]?data|authorization|"
    r"passphrase|password|passwd|pwd|token|secret"
    r")\b[\"']?\s*(?:[=:]|\bis\b)\s*[\"']?)([^\s,;\"'}\)]{4,})"
)

# URL 里的 user:password@host
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")

# JWT(v2board 的 auth_data 就是这种)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")

# Bearer <token>
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

# GitHub / 阿里云等常见前缀的令牌
_PREFIXED = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|LTAI[A-Za-z0-9]{12,})\b"
)


def redact(text: str | None) -> str | None:
    """把文本里像凭据的部分替换成 ***。None 原样返回。"""
    if not text:
        return text
    out = _JWT.sub(MASK, text)
    out = _PREFIXED.sub(MASK, out)
    out = _BEARER.sub(f"Bearer {MASK}", out)
    out = _URL_CRED.sub(rf"\1\2:{MASK}@", out)
    out = _KEYED.sub(rf"\1{MASK}", out)
    return out
