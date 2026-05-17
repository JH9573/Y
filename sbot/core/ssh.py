"""SSH 远程执行封装。

基于 asyncssh,提供命令执行与远程文件读写。
- 仅接受由 services/ 模块构造的命令,handler 与外部不得传入裸字符串。
- 统一处理连接、超时、非零退出码。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import asyncssh

from ..db.models import Server
from .crypto import Crypto


log = logging.getLogger(__name__)


class SSHError(RuntimeError):
    """SSH 操作失败的统一异常。"""


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    @property
    def combined(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts).strip()


class SSHClient:
    """对外接口:执行命令、读 / 写远程文件。

    每次调用都建立新连接,适合低频运维场景。后续若需高频可加连接池。
    """

    def __init__(self, crypto: Crypto, timeout: int = 15) -> None:
        self._crypto = crypto
        self._timeout = timeout

    def _conn_kwargs(self, server: Server) -> dict:
        kwargs: dict = {
            "host": server.host,
            "port": server.port,
            "username": server.username,
            "known_hosts": None,  # 受控环境,不强制 known_hosts
            "connect_timeout": self._timeout,
        }
        if server.auth_type == "key":
            kwargs["client_keys"] = [server.credential]
        elif server.auth_type == "password":
            kwargs["password"] = self._crypto.decrypt(server.credential)
        else:
            raise SSHError(f"未知 auth_type: {server.auth_type}")
        return kwargs

    async def run(
        self,
        server: Server,
        command: str,
        *,
        timeout: Optional[int] = None,
        check: bool = False,
    ) -> CommandResult:
        """执行命令并返回结果。

        check=True 时,非零退出码会抛 SSHError。
        """
        kwargs = self._conn_kwargs(server)
        try:
            async with asyncssh.connect(**kwargs) as conn:
                proc = await conn.run(command, timeout=timeout or self._timeout)
        except asyncssh.Error as exc:
            raise SSHError(f"SSH 连接 / 执行失败: {exc}") from exc
        except OSError as exc:
            raise SSHError(f"网络错误: {exc}") from exc

        result = CommandResult(
            exit_status=proc.exit_status or 0,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
        )
        if check and not result.ok:
            raise SSHError(
                f"命令执行失败 ({result.exit_status}): {result.combined or command}"
            )
        return result

    async def check_connectivity(self, server: Server) -> bool:
        """轻量级连通性检测,跑一个无害命令。"""
        try:
            res = await self.run(server, "true", timeout=self._timeout)
            return res.ok
        except SSHError:
            return False

    async def read_file(self, server: Server, path: str) -> str:
        """通过 SFTP 读取远程文件全文。"""
        kwargs = self._conn_kwargs(server)
        try:
            async with asyncssh.connect(**kwargs) as conn:
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(path, "r") as f:
                        return await f.read()
        except asyncssh.sftp.SFTPNoSuchFile as exc:
            raise FileNotFoundError(f"远程文件不存在: {path}") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"读取远程文件失败 {path}: {exc}") from exc

    async def write_file(self, server: Server, path: str, content: str) -> None:
        """通过 SFTP 写入远程文件(覆盖)。"""
        kwargs = self._conn_kwargs(server)
        try:
            async with asyncssh.connect(**kwargs) as conn:
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(path, "w") as f:
                        await f.write(content)
        except asyncssh.Error as exc:
            raise SSHError(f"写入远程文件失败 {path}: {exc}") from exc
