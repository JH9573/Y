"""SSH 远程执行封装。

基于 asyncssh,提供命令执行与远程文件读写。
- 仅接受由 services/ 模块构造的命令,handler 与外部不得传入裸字符串。
- 统一处理连接、超时、非零退出码。

两种用法:
- `ssh.run(server, cmd)` —— 一次性调用,自建自关连接,适合零散的单条命令。
- `async with ssh.connection(server) as conn:` —— 一条连接跑完整个流程。
  安装 / 卸载 / 改配置这类要连发十几条命令的场景走这个,省掉每条命令一次
  TCP + SSH 握手。连接上的 runner 与 SSHClient 同签名,services 层无需改动。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Union

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


def _result_of(proc) -> CommandResult:
    return CommandResult(
        exit_status=proc.exit_status or 0,
        stdout=(proc.stdout or "").strip(),
        stderr=(proc.stderr or "").strip(),
    )


def _checked(result: CommandResult, command: str) -> CommandResult:
    if not result.ok:
        raise SSHError(
            f"命令执行失败 ({result.exit_status}): {result.combined or command}"
        )
    return result


class SSHSession:
    """绑定在一条已建立连接上的执行器。

    方法签名与 SSHClient 完全一致(含冗余的 server 参数),这样 services 层
    的 `async def foo(ssh, server)` 拿到哪一种都能跑。传入的 server 必须与
    建连时的是同一台,否则说明调用方串了流程,直接报错。
    """

    def __init__(self, conn: asyncssh.SSHClientConnection, server: Server, timeout: int) -> None:
        self._conn = conn
        self._target = (server.host, server.port, server.username)
        self._timeout = timeout

    def _verify(self, server: Server) -> None:
        if (server.host, server.port, server.username) != self._target:
            raise SSHError(
                f"连接绑定在 {self._target[2]}@{self._target[0]}:{self._target[1]},"
                f"不能用来操作 {server.username}@{server.host}:{server.port}"
            )

    async def run(
        self,
        server: Server,
        command: str,
        *,
        timeout: Optional[int] = None,
        check: bool = False,
    ) -> CommandResult:
        self._verify(server)
        try:
            proc = await self._conn.run(command, timeout=timeout or self._timeout)
        except asyncssh.Error as exc:
            raise SSHError(f"SSH 执行失败: {exc}") from exc
        except OSError as exc:
            raise SSHError(f"网络错误: {exc}") from exc
        result = _result_of(proc)
        return _checked(result, command) if check else result

    async def check_connectivity(self, server: Server) -> bool:
        try:
            return (await self.run(server, "true")).ok
        except SSHError:
            return False

    async def read_file(self, server: Server, path: str) -> str:
        self._verify(server)
        try:
            async with self._conn.start_sftp_client() as sftp:
                async with sftp.open(path, "r") as f:
                    return await f.read()
        except asyncssh.sftp.SFTPNoSuchFile as exc:
            raise FileNotFoundError(f"远程文件不存在: {path}") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"读取远程文件失败 {path}: {exc}") from exc

    async def write_file(self, server: Server, path: str, content: str) -> None:
        self._verify(server)
        try:
            async with self._conn.start_sftp_client() as sftp:
                async with sftp.open(path, "w") as f:
                    await f.write(content)
        except asyncssh.Error as exc:
            raise SSHError(f"写入远程文件失败 {path}: {exc}") from exc


class SSHClient:
    """对外接口:执行命令、读 / 写远程文件。

    单条调用每次新建连接;要连发多条命令时用 connection() 复用一条连接。
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

    async def _connect(self, server: Server) -> asyncssh.SSHClientConnection:
        try:
            return await asyncssh.connect(**self._conn_kwargs(server))
        except asyncssh.Error as exc:
            raise SSHError(f"SSH 连接失败: {exc}") from exc
        except OSError as exc:
            raise SSHError(f"网络错误: {exc}") from exc

    @asynccontextmanager
    async def connection(self, server: Server) -> AsyncIterator[SSHSession]:
        """建立一条连接并复用。退出时确保关闭。

        只有建连本身的异常会被转成 SSHError;with 体内抛出的异常原样上抛,
        不会被误包装(安装流程要靠 InstallError 的 step 信息定位)。
        """
        conn = await self._connect(server)
        try:
            yield SSHSession(conn, server, self._timeout)
        finally:
            conn.close()
            await conn.wait_closed()

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

        result = _result_of(proc)
        return _checked(result, command) if check else result

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


# services 层统一用这个类型:既接受一次性 SSHClient,也接受复用连接的 SSHSession
SSHRunner = Union[SSHClient, SSHSession]
