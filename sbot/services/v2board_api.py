"""v2board 面板 admin API 客户端。

负责登录获取 auth_data,以及 admin 接口调用。
设计原则:
- 客户端每次调用建新 httpx 连接
- 401/403 时自动用 email/password 重登一次并写回库
- 错误统一抛 V2BoardAPIError,handler 层 catch 后转友好提示
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..core.crypto import Crypto
from ..db import crud
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

    # ---------- admin 请求 ----------

    async def _renew_auth(self, panel: Panel) -> str:
        """重登并把新 token 加密入库,同时更新游离 panel 对象。返回明文 token。"""
        token = await self.login(panel)
        encrypted = self._crypto.encrypt(token)
        async with crud.session() as s:
            await crud.update_panel_auth(s, panel.id, encrypted)
            await s.commit()
        panel.auth_data = encrypted
        return token

    async def _current_token(self, panel: Panel) -> str:
        """取当前明文 token。若 panel.auth_data 缺失或解密失败,自动重登一次。"""
        if panel.auth_data:
            try:
                return self._crypto.decrypt(panel.auth_data)
            except ValueError:
                log.warning("auth_data 解密失败,重新登录: panel_id=%s", panel.id)
        return await self._renew_auth(panel)

    async def _request_admin(
        self,
        panel: Panel,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """对 admin 接口发请求,处理 401/403 自动重登一次。

        返回响应顶层 dict;若需要 data 字段调用方自行取。
        """
        url = self._admin_url(panel.base_url, panel.secure_path, path)

        async def _send(token: str) -> httpx.Response:
            headers = {"Authorization": token}
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    return await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
            except httpx.HTTPError as exc:
                raise V2BoardAPIError(f"HTTP 请求失败: {exc}") from exc

        token = await self._current_token(panel)
        resp = await _send(token)
        if resp.status_code in (401, 403):
            log.info("admin 接口鉴权失败,尝试重登: panel_id=%s", panel.id)
            token = await self._renew_auth(panel)
            resp = await _send(token)

        if resp.status_code >= 400:
            raise V2BoardAPIError(self._extract_error(resp))

        try:
            payload = resp.json()
        except ValueError as exc:
            raise V2BoardAPIError(
                f"响应不是合法 JSON (HTTP {resp.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise V2BoardAPIError("响应顶层不是对象")
        return payload

    # ---------- 节点 ----------

    async def get_v2nodes(self, panel: Panel) -> list[dict[str, Any]]:
        """拉所有节点并过滤 type == 'v2node'。"""
        payload = await self._request_admin(
            panel, "GET", "server/manage/getNodes"
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise V2BoardAPIError("getNodes 响应缺少 data 数组")
        return [n for n in data if isinstance(n, dict) and n.get("type") == "v2node"]

    async def get_groups(self, panel: Panel) -> list[dict[str, Any]]:
        """权限组列表,详情页解析 group_id 用。"""
        payload = await self._request_admin(panel, "GET", "server/group/fetch")
        data = payload.get("data")
        if not isinstance(data, list):
            raise V2BoardAPIError("group/fetch 响应缺少 data 数组")
        return data

    async def update_v2node_show(
        self, panel: Panel, node_id: int, show: int
    ) -> None:
        """切换节点上/下架。show ∈ {0, 1}。"""
        if show not in (0, 1):
            raise V2BoardAPIError(f"show 必须是 0 或 1: {show!r}")
        await self._request_admin(
            panel,
            "POST",
            "server/v2node/update",
            json_body={"id": node_id, "show": show},
        )

    async def drop_v2node(self, panel: Panel, node_id: int) -> None:
        """删除节点。"""
        await self._request_admin(
            panel,
            "POST",
            "server/v2node/drop",
            json_body={"id": node_id},
        )
