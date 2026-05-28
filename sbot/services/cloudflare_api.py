"""Cloudflare DNS API 客户端。

鉴权方式: API Token (Authorization: Bearer <token>),官方推荐,可在
Cloudflare dashboard → My Profile → API Tokens 生成,
权限至少需要 Zone:Read + DNS:Edit。

设计与 services/v2board_api.py 保持一致:
- 每次调用新建 httpx 客户端
- 错误统一抛 CloudflareAPIError,handler 层 catch
- 客户端无状态,凭据由调用方从 DnsAccount 解密后传入
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..core.crypto import Crypto
from ..db.models import DnsAccount


log = logging.getLogger(__name__)


CF_API_BASE = "https://api.cloudflare.com/client/v4"

# Cloudflare 支持的常见记录类型;为了 UX 收敛先只暴露这几种
SUPPORTED_RECORD_TYPES: tuple[str, ...] = ("A", "AAAA", "CNAME", "TXT", "MX")
# A/AAAA/CNAME 才允许 proxied
PROXYABLE_TYPES: frozenset[str] = frozenset({"A", "AAAA", "CNAME"})

# Cloudflare 免费版 TTL 最小 60 秒,最大 86400;1 代表 Automatic
TTL_PRESETS: tuple[tuple[int, str], ...] = (
    (1, "自动"),
    (60, "1 分钟"),
    (300, "5 分钟"),
    (3600, "1 小时"),
    (86400, "1 天"),
)


class CloudflareAPIError(RuntimeError):
    """Cloudflare API 调用失败的统一异常。"""


# ---------- 输入校验 ----------

_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:(?!-)[A-Za-z0-9-]{1,63}(?<!-)\.)*"
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)$"
)
_IPV4_PATTERN = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)


def validate_record_name(value: str) -> str:
    """校验记录名。支持 '@' 表示 zone apex 以及子域名。

    返回去空白后的字符串。深层正确性由 Cloudflare 校验。
    """
    value = value.strip().rstrip(".")
    if not value:
        raise CloudflareAPIError("名称不能为空(根域名请输入 @)")
    if value == "@":
        return value
    # 允许 _service._proto.name 之类的 SRV 风格
    if not _HOSTNAME_PATTERN.match(value.replace("_", "a")):
        raise CloudflareAPIError("名称格式不合法,只能含字母/数字/'-'/'.'")
    return value


def validate_record_content(record_type: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise CloudflareAPIError("内容不能为空")
    if record_type == "A":
        if not _IPV4_PATTERN.match(value):
            raise CloudflareAPIError("A 记录内容必须是合法 IPv4 地址")
    elif record_type == "AAAA":
        if ":" not in value:
            raise CloudflareAPIError("AAAA 记录内容必须是 IPv6 地址")
    elif record_type in ("CNAME", "MX"):
        if not _HOSTNAME_PATTERN.match(value.rstrip(".")):
            raise CloudflareAPIError(
                f"{record_type} 记录内容必须是合法主机名"
            )
    # TXT 不校验,Cloudflare 自行处理引号 / 长度
    return value


def validate_ttl(value: str) -> int:
    try:
        ttl = int(value.strip())
    except ValueError as exc:
        raise CloudflareAPIError("TTL 必须是整数") from exc
    if ttl == 1:
        return 1
    if ttl < 60 or ttl > 86400:
        raise CloudflareAPIError("TTL 必须为 1(自动)或 60-86400 秒之间")
    return ttl


def validate_priority(value: str) -> int:
    try:
        prio = int(value.strip())
    except ValueError as exc:
        raise CloudflareAPIError("优先级必须是整数") from exc
    if prio < 0 or prio > 65535:
        raise CloudflareAPIError("优先级范围 0-65535")
    return prio


def ttl_label(ttl: int | None) -> str:
    if ttl is None:
        return "-"
    for v, label in TTL_PRESETS:
        if v == ttl:
            return f"{label} ({ttl})" if ttl != 1 else label
    return f"{ttl} 秒"


# ---------- 客户端 ----------

class CloudflareClient:
    """无状态 Cloudflare API 客户端。

    所有方法接收 DnsAccount 实体,内部解密 api_token 后调用 API。
    """

    def __init__(self, crypto: Crypto, timeout: int = 15) -> None:
        self._crypto = crypto
        self._timeout = timeout

    def _decrypt_token(self, account: DnsAccount) -> str:
        try:
            return self._crypto.decrypt(account.api_token)
        except ValueError as exc:
            raise CloudflareAPIError(f"API token 解密失败: {exc}") from exc

    def _headers(self, account: DnsAccount) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._decrypt_token(account)}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if isinstance(data, dict):
            errs = data.get("errors") or []
            if isinstance(errs, list) and errs:
                parts = []
                for err in errs:
                    if not isinstance(err, dict):
                        continue
                    code = err.get("code")
                    msg = err.get("message")
                    if msg:
                        parts.append(f"{msg}" + (f" (code={code})" if code else ""))
                if parts:
                    return "; ".join(parts) + f" (HTTP {resp.status_code})"
            msg = data.get("message")
            if msg:
                return f"{msg} (HTTP {resp.status_code})"
        return f"HTTP {resp.status_code}"

    async def _request(
        self,
        account: DnsAccount,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{CF_API_BASE}{path}"
        headers = self._headers(account)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json_body,
                )
        except httpx.HTTPError as exc:
            raise CloudflareAPIError(f"HTTP 请求失败: {exc}") from exc

        if resp.status_code >= 400:
            raise CloudflareAPIError(self._extract_error(resp))

        try:
            payload = resp.json()
        except ValueError as exc:
            raise CloudflareAPIError(
                f"响应不是合法 JSON (HTTP {resp.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise CloudflareAPIError("响应顶层不是对象")
        if payload.get("success") is False:
            # success=false 时 _extract_error 已经在 4xx 路径吃掉了,这里兜底
            raise CloudflareAPIError(self._extract_error(resp))
        return payload

    # ---------- token 测试 ----------

    async def verify_token(self, account: DnsAccount) -> dict[str, Any]:
        """调用 /user/tokens/verify 测试 token 是否可用。返回 result 字段。"""
        payload = await self._request(account, "GET", "/user/tokens/verify")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CloudflareAPIError("verify 响应缺少 result 字段")
        return result

    # ---------- zones ----------

    async def list_zones(
        self, account: DnsAccount, *, page: int = 1, per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回 (zones 列表, result_info 分页信息)。"""
        payload = await self._request(
            account,
            "GET",
            "/zones",
            params={"page": page, "per_page": per_page},
        )
        result = payload.get("result") or []
        info = payload.get("result_info") or {}
        if not isinstance(result, list):
            raise CloudflareAPIError("zones 响应 result 字段不是数组")
        return result, info if isinstance(info, dict) else {}

    async def get_zone(self, account: DnsAccount, zone_id: str) -> dict[str, Any]:
        payload = await self._request(account, "GET", f"/zones/{zone_id}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CloudflareAPIError("zone 响应缺少 result")
        return result

    # ---------- dns records ----------

    async def list_records(
        self,
        account: DnsAccount,
        zone_id: str,
        *,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        payload = await self._request(
            account,
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"page": page, "per_page": per_page},
        )
        result = payload.get("result") or []
        info = payload.get("result_info") or {}
        if not isinstance(result, list):
            raise CloudflareAPIError("dns_records 响应 result 字段不是数组")
        return result, info if isinstance(info, dict) else {}

    async def get_record(
        self, account: DnsAccount, zone_id: str, record_id: str,
    ) -> dict[str, Any]:
        payload = await self._request(
            account, "GET", f"/zones/{zone_id}/dns_records/{record_id}",
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CloudflareAPIError("record 响应缺少 result")
        return result

    async def create_record(
        self,
        account: DnsAccount,
        zone_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        resp = await self._request(
            account,
            "POST",
            f"/zones/{zone_id}/dns_records",
            json_body=payload,
        )
        result = resp.get("result")
        if not isinstance(result, dict):
            raise CloudflareAPIError("create record 响应缺少 result")
        return result

    async def update_record(
        self,
        account: DnsAccount,
        zone_id: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        resp = await self._request(
            account,
            "PUT",
            f"/zones/{zone_id}/dns_records/{record_id}",
            json_body=payload,
        )
        result = resp.get("result")
        if not isinstance(result, dict):
            raise CloudflareAPIError("update record 响应缺少 result")
        return result

    async def delete_record(
        self, account: DnsAccount, zone_id: str, record_id: str,
    ) -> None:
        await self._request(
            account, "DELETE", f"/zones/{zone_id}/dns_records/{record_id}",
        )
