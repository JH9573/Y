"""腾讯云 COS API 客户端。

直接调用 COS XML API(GetObject / PutObject / GetBucket),httpx 全异步,
不引入官方 SDK。签名为腾讯云自有方案(HMAC-SHA1,q-sign-algorithm=sha1),
实现参考官方文档「请求签名」。

远程配置功能只需读写小 JSON 文件,故仅封装文本对象的取/存与连通性检查。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)


_TIMEOUT = httpx.Timeout(15, read=60, write=60, pool=15)
# 签名有效期(秒)。时钟允许 60s 回拨容差。
_SIGN_EXPIRE = 600


class COSAPIError(RuntimeError):
    """COS API 调用失败的统一异常。status 保存 HTTP 状态码(网络错误为 0)。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _pct(value: str) -> str:
    """签名要求的 percent-encode(除 A-Za-z0-9-_.~ 外全部编码)。"""
    return quote(value, safe="")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class COSClient:
    """单 bucket 的 COS 客户端。bucket 需带 APPID 后缀。"""

    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        secret_id: str,
        secret_key: str,
    ) -> None:
        self._region = region
        self._bucket = bucket
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._host = f"{bucket}.cos.{region}.myqcloud.com"

    # ---------- 签名 ----------

    def _authorization(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> str:
        now = int(time.time())
        key_time = f"{now - 60};{now + _SIGN_EXPIRE}"
        sign_key = hmac.new(
            self._secret_key.encode(), key_time.encode(), hashlib.sha1
        ).hexdigest()

        def fmt(d: dict[str, str]) -> tuple[str, str]:
            items = sorted(
                (_pct(k.lower()), _pct(str(v))) for k, v in d.items()
            )
            return (
                ";".join(k for k, _ in items),
                "&".join(f"{k}={v}" for k, v in items),
            )

        param_list, param_str = fmt(params)
        header_list, header_str = fmt(headers)
        http_string = f"{method.lower()}\n{path}\n{param_str}\n{header_str}\n"
        string_to_sign = (
            f"sha1\n{key_time}\n"
            f"{hashlib.sha1(http_string.encode()).hexdigest()}\n"
        )
        signature = hmac.new(
            sign_key.encode(), string_to_sign.encode(), hashlib.sha1
        ).hexdigest()
        return (
            f"q-sign-algorithm=sha1&q-ak={self._secret_id}"
            f"&q-sign-time={key_time}&q-key-time={key_time}"
            f"&q-header-list={header_list}&q-url-param-list={param_list}"
            f"&q-signature={signature}"
        )

    # ---------- 请求 ----------

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError:
            return f"HTTP {resp.status_code}"
        code = message = ""
        for child in root:
            if _strip_ns(child.tag) == "Code":
                code = child.text or ""
            elif _strip_ns(child.tag) == "Message":
                message = child.text or ""
        if code or message:
            return f"{code}: {message} (HTTP {resp.status_code})"
        return f"HTTP {resp.status_code}"

    async def _request(
        self,
        method: str,
        key: str = "",
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        params = params or {}
        headers = dict(headers or {})
        path = "/" + key
        signed_headers = {"host": self._host}
        auth = self._authorization(method, path, params, signed_headers)
        headers["Authorization"] = auth

        url = f"https://{self._host}" + quote(path, safe="/")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(
                    method, url, params=params, headers=headers, content=content,
                )
        except httpx.HTTPError as exc:
            raise COSAPIError(f"HTTP 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise COSAPIError(
                self._extract_error(resp), status=resp.status_code
            )
        return resp

    # ---------- 对象操作 ----------

    async def get_object_text(self, key: str) -> str:
        """读取文本对象内容(远程配置 JSON 都是小文件,直接进内存)。"""
        resp = await self._request("GET", key)
        return resp.content.decode("utf-8")

    async def put_object_text(
        self,
        key: str,
        text: str,
        *,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        data = text.encode("utf-8")
        await self._request(
            "PUT",
            key,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(data)),
            },
            content=data,
        )

    async def check_access(self) -> None:
        """轻量校验凭据/区域/bucket 是否可用(GetBucket 取 1 条)。"""
        await self._request("GET", "", params={"max-keys": "1"})
