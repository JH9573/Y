"""v2board 面板 admin API 客户端。

负责登录获取 auth_data,以及后续的 admin 接口调用。
设计原则:
- 客户端 stateless,每次调用建新 httpx 连接
- 401/403 时自动用 email/password 重登一次(后续接 admin 接口时启用)
- 错误统一抛 V2BoardAPIError,handler 层 catch 后转友好提示
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..core.crypto import Crypto
from ..db.models import Panel


log = logging.getLogger(__name__)


class V2BoardAPIError(RuntimeError):
    """面板 API 调用失败的统一异常。"""


# ---------- 输入校验 ----------

_URL_PATTERN = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not _URL_PATTERN.match(value):
        raise V2BoardAPIError("面板地址必须是 http:// 或 https:// 开头的 URL")
    return value


def validate_secure_path(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        raise V2BoardAPIError("后台路径不能为空")
    if "/" in value or " " in value:
        raise V2BoardAPIError("后台路径不能含 / 或空格")
    return value


def validate_email(value: str) -> str:
    value = value.strip()
    if not _EMAIL_PATTERN.match(value):
        raise V2BoardAPIError("邮箱格式不正确")
    return value


# ---------- 客户端 ----------

class V2BoardClient:
    """v2board 面板 admin API 客户端。

    handler 不直接调 httpx,统一走这里。crypto 用于解密 Panel.password。
    """

    def __init__(self, crypto: Crypto, timeout: int = 15) -> None:
        self._crypto = crypto
        self._timeout = timeout

    @staticmethod
    def _passport_url(base_url: str, path: str) -> str:
        """passport (登录)接口,无 secure_path 前缀。"""
        return f"{base_url.rstrip('/')}/api/v1/{path.lstrip('/')}"

    @staticmethod
    def _admin_url(base_url: str, secure_path: str, path: str) -> str:
        """admin 接口,带 secure_path 前缀。"""
        return (
            f"{base_url.rstrip('/')}/api/v1/"
            f"{secure_path.strip('/')}/{path.lstrip('/')}"
        )

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        """从 v2board 错误响应里提取人类可读消息。"""
        try:
            data = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if isinstance(data, dict):
            msg = data.get("message")
            if msg:
                return f"{msg} (HTTP {resp.status_code})"
        return f"HTTP {resp.status_code}"

    async def login(self, panel: Panel) -> str:
        """用 panel.email / panel.password 登录,返回新 auth_data。

        失败抛 V2BoardAPIError。不入库,调用方负责持久化。
        """
        try:
            password = self._crypto.decrypt(panel.password)
        except ValueError as exc:
            raise V2BoardAPIError(f"面板密码解密失败: {exc}") from exc

        url = self._passport_url(panel.base_url, "passport/auth/login")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, json={"email": panel.email, "password": password}
                )
        except httpx.HTTPError as exc:
            raise V2BoardAPIError(f"HTTP 请求失败: {exc}") from exc

        if resp.status_code >= 400:
            raise V2BoardAPIError(self._extract_error(resp))

        try:
            payload = resp.json()
        except ValueError as exc:
            raise V2BoardAPIError(
                f"登录响应不是合法 JSON (HTTP {resp.status_code})"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise V2BoardAPIError("登录响应缺少 data 字段")

        auth_data = data.get("auth_data")
        is_admin = data.get("is_admin")
        if not auth_data:
            raise V2BoardAPIError("登录响应缺少 auth_data")
        if not is_admin:
            raise V2BoardAPIError("该账号不是管理员,无法管理面板节点")
        return auth_data

    async def test_login(self, panel: Panel) -> tuple[bool, str]:
        """轻量级登录测试,返回 (是否成功, 消息)。供添加面板时调用。"""
        try:
            await self.login(panel)
        except V2BoardAPIError as exc:
            return False, str(exc)
        return True, "登录成功"
