"""阿里云 OSS API 客户端(V4 签名)。

直接调用 OSS REST API(PutObject / CopyObject / ListObjectsV2 / DeleteObject),
不引入官方 SDK,保持与项目其余部分一致的全异步 httpx 风格。
凭据来自 .env(见 config.Config 的 oss_* 字段),明文持有,不经过 Crypto。

签名实现参考官方文档「在 Header 中包含 V4 签名」;新建 bucket 已不再
开放 V1 签名,故这里只实现 V4。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)


_UNSIGNED = "UNSIGNED-PAYLOAD"

# 上传可能是几百 MB 的安装包,读写超时放宽
_TRANSFER_TIMEOUT = httpx.Timeout(15, read=900, write=900, pool=15)


class OSSAPIError(RuntimeError):
    """OSS API 调用失败的统一异常。"""


def _pct(value: str) -> str:
    """OSS V4 签名要求的 percent-encode(除 A-Za-z0-9-_.~ 外全部编码)。"""
    return quote(value, safe="")


def _strip_ns(tag: str) -> str:
    """去掉 XML 标签可能携带的 namespace 前缀。"""
    return tag.rsplit("}", 1)[-1]


class OSSClient:
    """单 bucket 的 OSS 客户端。"""

    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str | None = None,
        public_base_url: str | None = None,
    ) -> None:
        self._region = region
        self._bucket = bucket
        self._ak = access_key_id
        self._sk = access_key_secret
        self._endpoint = endpoint or f"oss-{region}.aliyuncs.com"
        self._public_base_url = public_base_url

    # ---------- URL ----------

    def public_url(self, key: str) -> str:
        """对象的公网访问地址(bucket 需 public-read)。"""
        encoded = quote(key, safe="/")
        if self._public_base_url:
            return f"{self._public_base_url}/{encoded}"
        return f"https://{self._bucket}.{self._endpoint}/{encoded}"

    @property
    def bucket(self) -> str:
        return self._bucket

    # ---------- V4 签名 ----------

    def _authorization(
        self,
        method: str,
        key: str,
        query: dict[str, str],
        headers: dict[str, str],
    ) -> str:
        """按 V4 规则计算 Authorization 头。headers 需已含 x-oss-date。"""
        canonical_uri = f"/{self._bucket}/" + quote(key, safe="/")
        canonical_query = "&".join(
            f"{_pct(k)}={_pct(v)}" if v else _pct(k)
            for k, v in sorted(query.items())
        )
        # 参与签名的 header: x-oss-* / content-type / content-md5
        signed = {
            k.lower(): v.strip()
            for k, v in headers.items()
            if k.lower().startswith("x-oss-")
            or k.lower() in ("content-type", "content-md5")
        }
        canonical_headers = "\n".join(
            f"{k}:{v}" for k, v in sorted(signed.items())
        )
        payload = headers.get("x-oss-content-sha256", _UNSIGNED)
        canonical_request = "\n".join([
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            "",  # canonical headers 的收尾换行
            "",  # additional headers(未使用)
            payload,
        ])

        ts = headers["x-oss-date"]
        date = ts[:8]
        scope = f"{date}/{self._region}/oss/aliyun_v4_request"
        string_to_sign = "\n".join([
            "OSS4-HMAC-SHA256",
            ts,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])

        def _hmac(key_bytes: bytes, msg: str) -> bytes:
            return hmac.new(key_bytes, msg.encode(), hashlib.sha256).digest()

        signing_key = _hmac(
            _hmac(_hmac(_hmac(f"aliyun_v4{self._sk}".encode(), date),
                        self._region), "oss"),
            "aliyun_v4_request",
        )
        signature = hmac.new(
            signing_key, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return (
            f"OSS4-HMAC-SHA256 Credential={self._ak}/{scope},"
            f"Signature={signature}"
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
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content=None,
    ) -> httpx.Response:
        query = query or {}
        headers = dict(headers or {})
        headers["x-oss-date"] = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        headers["x-oss-content-sha256"] = _UNSIGNED
        headers["Authorization"] = self._authorization(
            method, key, query, headers
        )

        url = f"https://{self._bucket}.{self._endpoint}/" + quote(key, safe="/")
        if query:
            # 与签名使用完全一致的编码,避免 SignatureDoesNotMatch
            url += "?" + "&".join(
                f"{_pct(k)}={_pct(v)}" if v else _pct(k)
                for k, v in sorted(query.items())
            )
        try:
            async with httpx.AsyncClient(timeout=_TRANSFER_TIMEOUT) as client:
                resp = await client.request(
                    method, url, headers=headers, content=content,
                )
        except httpx.HTTPError as exc:
            raise OSSAPIError(f"HTTP 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise OSSAPIError(self._extract_error(resp))
        return resp

    # ---------- 对象操作 ----------

    @staticmethod
    async def _aiter_file(path: str, chunk: int = 1024 * 1024) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        with open(path, "rb") as f:
            while True:
                data = await loop.run_in_executor(None, f.read, chunk)
                if not data:
                    break
                yield data

    async def put_object_file(
        self,
        key: str,
        path: str,
        *,
        content_type: str | None = None,
        acl: str = "public-read",
    ) -> None:
        """PutObject: 流式上传本地文件(单文件上限 5 GB)。"""
        size = os.path.getsize(path)
        headers = {
            "Content-Type": (
                content_type
                or mimetypes.guess_type(key)[0]
                or "application/octet-stream"
            ),
            "Content-Length": str(size),
            "x-oss-object-acl": acl,
        }
        await self._request("PUT", key, headers=headers,
                            content=self._aiter_file(path))

    async def copy_object(
        self, src_key: str, dst_key: str, *, acl: str = "public-read",
    ) -> None:
        """CopyObject: 服务端复制(同 bucket 内,单文件上限 1 GB)。"""
        headers = {
            "x-oss-copy-source": f"/{self._bucket}/" + quote(src_key, safe="/"),
            "x-oss-object-acl": acl,
            "Content-Length": "0",
        }
        await self._request("PUT", dst_key, headers=headers)

    async def delete_object(self, key: str) -> None:
        await self._request("DELETE", key)

    async def check_access(self) -> None:
        """轻量校验凭据/区域/bucket 是否可用(ListObjectsV2 取 1 条)。"""
        await self._request("GET", "", query={"list-type": "2", "max-keys": "1"})

    async def list_keys(self, prefix: str) -> list[str]:
        """ListObjectsV2: 列出指定前缀下的全部 object key。"""
        keys: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "200"}
            if token:
                query["continuation-token"] = token
            resp = await self._request("GET", "", query=query)
            try:
                root = ElementTree.fromstring(resp.content)
            except ElementTree.ParseError as exc:
                raise OSSAPIError("ListObjects 响应不是合法 XML") from exc
            token = ""
            truncated = False
            for child in root:
                tag = _strip_ns(child.tag)
                if tag == "Contents":
                    for sub in child:
                        if _strip_ns(sub.tag) == "Key" and sub.text:
                            keys.append(sub.text)
                elif tag == "IsTruncated":
                    truncated = (child.text or "").lower() == "true"
                elif tag == "NextContinuationToken":
                    token = child.text or ""
            if not truncated or not token:
                return keys
