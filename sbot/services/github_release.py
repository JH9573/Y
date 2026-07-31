"""GitHub Releases API 客户端。

用于从私有仓库读取 Release 列表并下载构建产物(assets)。
鉴权: fine-grained PAT (Authorization: Bearer <token>),
对目标仓库至少需要 Contents:Read 权限。

设计与 services/cloudflare_api.py 保持一致:
- 每次调用新建 httpx 客户端
- 错误统一抛 GitHubAPIError,handler 层 catch
- 客户端无状态,凭据由调用方从 ReleaseSource 解密后传入
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from ..core.crypto import Crypto
from ..db.models import ReleaseSource

log = logging.getLogger(__name__)


GITHUB_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# 下载 asset 可能是几百 MB 的安装包,读超时放宽
_TRANSFER_TIMEOUT = httpx.Timeout(15, read=900, write=900, pool=15)


class GitHubAPIError(RuntimeError):
    """GitHub API 调用失败的统一异常。"""


def validate_repo(value: str) -> str:
    """校验 owner/name 形式的仓库标识,返回去空白后的字符串。"""
    value = value.strip().removeprefix("https://github.com/").strip("/")
    if not _REPO_PATTERN.match(value):
        raise GitHubAPIError("仓库格式不合法,应为 owner/name")
    return value


class GitHubReleaseClient:
    """无状态 GitHub Releases 客户端。

    所有方法接收 ReleaseSource 实体,内部解密 token 后调用 API。
    """

    def __init__(self, crypto: Crypto, timeout: int = 15) -> None:
        self._crypto = crypto
        self._timeout = timeout

    def _decrypt_token(self, source: ReleaseSource) -> str:
        try:
            return self._crypto.decrypt(source.token)
        except ValueError as exc:
            raise GitHubAPIError(f"token 解密失败: {exc}") from exc

    def _headers(self, source: ReleaseSource, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._decrypt_token(source)}",
            "Accept": accept,
            "X-GitHub-Api-Version": _API_VERSION,
        }

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if isinstance(data, dict) and data.get("message"):
            return f"{data['message']} (HTTP {resp.status_code})"
        return f"HTTP {resp.status_code}"

    async def _get_json(
        self,
        source: ReleaseSource,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{GITHUB_API_BASE}{path}"
        headers = self._headers(source, "application/vnd.github+json")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"HTTP 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise GitHubAPIError(self._extract_error(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubAPIError(
                f"响应不是合法 JSON (HTTP {resp.status_code})"
            ) from exc

    # ---------- 仓库校验 ----------

    async def verify_repo(self, source: ReleaseSource) -> dict[str, Any]:
        """读取仓库信息,校验 token 对仓库可读。返回仓库 JSON。"""
        payload = await self._get_json(source, f"/repos/{source.repo}")
        if not isinstance(payload, dict):
            raise GitHubAPIError("仓库响应格式异常")
        return payload

    # ---------- releases ----------

    async def list_releases(
        self, source: ReleaseSource, *, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """返回最近的 Release 列表(不含 draft),按发布时间倒序。

        每项: {tag, name, prerelease, published_at,
               assets: [{id, name, size}]}
        """
        payload = await self._get_json(
            source,
            f"/repos/{source.repo}/releases",
            params={"per_page": limit},
        )
        if not isinstance(payload, list):
            raise GitHubAPIError("releases 响应不是数组")
        releases: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            assets = []
            for asset in item.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                assets.append({
                    "id": asset.get("id"),
                    "name": str(asset.get("name") or ""),
                    "size": int(asset.get("size") or 0),
                })
            releases.append({
                "tag": str(item.get("tag_name") or ""),
                "name": str(item.get("name") or ""),
                "prerelease": bool(item.get("prerelease")),
                "published_at": str(item.get("published_at") or ""),
                "assets": assets,
            })
        return releases

    # ---------- asset 下载 ----------

    async def download_asset(
        self, source: ReleaseSource, asset_id: int, dest_path: str,
    ) -> int:
        """下载单个 asset 到本地文件,返回写入字节数。

        私有仓库需请求 asset 的 API 地址并带 Accept: octet-stream,
        GitHub 会 302 到对象存储的签名地址(httpx 跨域重定向时
        会自动丢弃 Authorization 头,不会泄漏 token)。
        """
        url = f"{GITHUB_API_BASE}/repos/{source.repo}/releases/assets/{asset_id}"
        headers = self._headers(source, "application/octet-stream")
        written = 0
        loop = asyncio.get_running_loop()
        try:
            async with httpx.AsyncClient(
                timeout=_TRANSFER_TIMEOUT, follow_redirects=True,
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise GitHubAPIError(self._extract_error(resp))
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            await loop.run_in_executor(None, f.write, chunk)
                            written += len(chunk)
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"下载失败: {exc}") from exc
        return written
