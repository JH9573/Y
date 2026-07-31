"""SSH 远程执行封装。

基于 asyncssh,提供命令执行与远程文件读写。
- 仅接受由 services/ 模块构造的命令,handler 与外部不得传入裸字符串。
- 统一处理连接、超时、非零退出码。

两种用法:
- `ssh.run(server, cmd)` —— 一次性调用,自建自关连接,适合零散的单条命令。
- `async with ssh.connection(server) as conn:` —— 一条连接跑完整个流程。
  安装 / 卸载 / 改配置这类要连发十几条命令的场景走这个,省掉每条命令一次
  TCP + SSH 握手。连接上的 runner 与 SSHClient 同签名,services 层无需改动。

主机密钥采用 TOFU(首次使用即信任):第一次连上时记录指纹到 servers.host_key,
之后每次连接都要求一致。校验发生在密钥交换阶段、认证之前,所以指纹对不上时
密码 / 私钥不会发出去。服务器重装导致指纹变化时,在「修改服务器」里重置即可。
"""
from __future__ import annotations

import logging
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from ..db.models import Server
from .crypto import Crypto

log = logging.getLogger(__name__)


class SSHError(RuntimeError):
    """SSH 操作失败的统一异常。"""


class HostKeyMismatch(SSHError):
    """服务器出示的主机密钥与已记录的不一致。"""


# ---------- 主机密钥 (TOFU) ----------

def _tofu_known_hosts(host: str, addr: str, port: int):
    """给 asyncssh 的 known_hosts 回调:不预置任何受信任 key。

    返回空列表(而不是 known_hosts=None)是关键:asyncssh 只有在信任列表
    非 None 时才会去问 SSHClient.validate_host_public_key,也就是下面的
    _HostKeyValidator;传 None 等于彻底关掉主机密钥校验。
    """
    return [], [], []


class _HostKeyValidator(asyncssh.SSHClient):
    """TOFU 校验器。首次连接放行并记录指纹,之后必须与记录一致。"""

    def __init__(self, expected: str | None) -> None:
        self._expected = expected
        self.observed: str | None = None
        self.mismatch = False

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        fingerprint = key.get_fingerprint()
        self.observed = fingerprint
        if self._expected is None:
            return True
        if fingerprint == self._expected:
            return True
        self.mismatch = True
        return False


# ---------- 私钥文件 ----------

def validate_key_path(raw: str) -> str:
    """校验 bot 机器上的私钥路径,返回规范化后的绝对路径。

    以前这里只把用户输入原样存库,路径打错要到真正连服务器时才报错。
    """
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise SSHError(f"请填绝对路径(当前: {raw})")
    if not path.exists():
        raise SSHError(f"文件不存在: {path}")
    if not path.is_file():
        raise SSHError(f"不是普通文件: {path}")
    if not os.access(path, os.R_OK):
        raise SSHError(f"bot 进程没有读取权限: {path}")
    return str(path)


def key_permission_warning(path: str) -> str | None:
    """私钥对同组 / 其它用户可读时给一句提醒(不阻断)。"""
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"⚠️ 私钥权限过松({oct(mode & 0o777)}),建议 chmod 600 {path}"
    return None


def key_needs_passphrase(path: str) -> bool:
    """私钥是否被口令加密。无法解析的私钥直接抛 SSHError。"""
    try:
        asyncssh.read_private_key(path)
    except asyncssh.KeyEncryptionError:
        return True
    except asyncssh.KeyImportError as exc:
        if "passphrase" in str(exc).lower():
            return True
        raise SSHError(f"私钥无法解析: {exc}") from exc
    return False


def check_key_passphrase(path: str, passphrase: str) -> None:
    """验证口令能解开该私钥,不对就抛 SSHError。"""
    try:
        asyncssh.read_private_key(path, passphrase)
    except (asyncssh.KeyEncryptionError, asyncssh.KeyImportError) as exc:
        raise SSHError(f"口令无法解开该私钥: {exc}") from exc


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
        timeout: int | None = None,
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

    def __init__(
        self,
        crypto: Crypto,
        timeout: int = 15,
        *,
        on_host_key: Callable[[Server, str], Awaitable[None]] | None = None,
    ) -> None:
        self._crypto = crypto
        self._timeout = timeout
        # 首次见到某台服务器的主机密钥时回调,由上层负责落库(见 main)
        self._on_host_key = on_host_key

    def _conn_kwargs(self, server: Server) -> dict:
        kwargs: dict = {
            "host": server.host,
            "port": server.port,
            "username": server.username,
            "known_hosts": _tofu_known_hosts,
            "connect_timeout": self._timeout,
        }
        if server.auth_type == "key":
            kwargs["client_keys"] = [server.credential]
            passphrase = getattr(server, "key_passphrase", None)
            if passphrase:
                kwargs["passphrase"] = self._crypto.decrypt(passphrase)
        elif server.auth_type == "password":
            kwargs["password"] = self._crypto.decrypt(server.credential)
        else:
            raise SSHError(f"未知 auth_type: {server.auth_type}")
        return kwargs

    async def _connect(self, server: Server) -> asyncssh.SSHClientConnection:
        expected = getattr(server, "host_key", None)
        validator = _HostKeyValidator(expected)
        try:
            conn = await asyncssh.connect(
                **self._conn_kwargs(server), client_factory=lambda: validator
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            if validator.mismatch:
                raise HostKeyMismatch(
                    f"{server.host} 出示的主机密钥与首次连接时记录的不一致。\n"
                    f"已记录: {expected}\n"
                    f"本次:   {validator.observed}\n"
                    "若该服务器确实重装过,请到「修改服务器」重置主机密钥;"
                    "否则可能存在中间人,先别继续。"
                ) from exc
            raise SSHError(f"主机密钥校验失败: {exc}") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"SSH 连接失败: {exc}") from exc
        except OSError as exc:
            raise SSHError(f"网络错误: {exc}") from exc

        if expected is None and validator.observed:
            # 首次连接:记住指纹。游离对象(尚未入库的连通性试连)只写内存,
            # 由调用方在建档时一并持久化。
            server.host_key = validator.observed
            if self._on_host_key is not None and getattr(server, "id", None):
                try:
                    await self._on_host_key(server, validator.observed)
                except Exception:  # noqa: BLE001
                    log.exception("记录主机密钥失败: %s", server.host)
        return conn

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

    # 下面几个都是「建连 -> 干一件事 -> 关连接」,统一走 connection(),
    # 这样主机密钥校验、错误映射只有一份实现。
    async def run(
        self,
        server: Server,
        command: str,
        *,
        timeout: int | None = None,
        check: bool = False,
    ) -> CommandResult:
        """执行命令并返回结果。

        check=True 时,非零退出码会抛 SSHError。
        """
        async with self.connection(server) as sess:
            return await sess.run(server, command, timeout=timeout, check=check)

    async def check_connectivity(self, server: Server) -> bool:
        """轻量级连通性检测,跑一个无害命令。

        注意:主机密钥不一致属于安全问题,不能被当成「连不上」吞掉,照常抛出。
        """
        try:
            res = await self.run(server, "true", timeout=self._timeout)
            return res.ok
        except HostKeyMismatch:
            raise
        except SSHError:
            return False

    async def read_file(self, server: Server, path: str) -> str:
        """通过 SFTP 读取远程文件全文。"""
        async with self.connection(server) as sess:
            return await sess.read_file(server, path)

    async def write_file(self, server: Server, path: str, content: str) -> None:
        """通过 SFTP 写入远程文件(覆盖)。"""
        async with self.connection(server) as sess:
            await sess.write_file(server, path, content)


# services 层统一用这个类型:既接受一次性 SSHClient,也接受复用连接的 SSHSession
SSHRunner = SSHClient | SSHSession
