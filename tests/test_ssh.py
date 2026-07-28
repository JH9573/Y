"""SSH 层:主机密钥 TOFU、连接复用、私钥校验。

这里在本机起一个真的 asyncssh 服务端来测——主机密钥校验发生在协议层,
用假对象测不出「指纹不符时认证根本没开始」这个关键性质。
"""
from __future__ import annotations

import asyncssh
import pytest

from sbot.core.ssh import (
    HostKeyMismatch,
    SSHClient,
    SSHError,
    SSHSession,
    check_key_passphrase,
    key_needs_passphrase,
    key_permission_warning,
    validate_key_path,
)
from sbot.db.models import Server


class _Server(asyncssh.SSHServer):
    """记账用:谁连上来了、有没有走到认证阶段。"""

    stats: dict = {}

    def connection_made(self, conn):
        self.stats["connections"] = self.stats.get("connections", 0) + 1

    def begin_auth(self, username: str) -> bool:
        # 认证在密钥交换之后。主机密钥不匹配时这里绝不该被调用。
        self.stats["auth_started"] = self.stats.get("auth_started", 0) + 1
        return False  # 不要求认证


async def _handle(process):
    process.stdout.write(f"ran:{process.command}\n")
    process.exit(0)


@pytest.fixture
def host_keys():
    return (
        asyncssh.generate_private_key("ssh-ed25519"),
        asyncssh.generate_private_key("ssh-ed25519"),
    )


@pytest.fixture
async def sshd(host_keys):
    """起一个只认第一把 host key 的本地 sshd,返回 (端口, 统计, 换密钥回调)。"""
    stats: dict = {}
    _Server.stats = stats
    servers = []

    async def start(key):
        srv = await asyncssh.create_server(
            _Server, "127.0.0.1", 0,
            server_host_keys=[key],
            process_factory=_handle,
        )
        servers.append(srv)
        return next(iter(srv.sockets)).getsockname()[1]

    port = await start(host_keys[0])
    yield port, stats, start
    for srv in servers:
        srv.close()


def _server_obj(port: int, host_key: str | None = None) -> Server:
    return Server(
        name="t", host="127.0.0.1", port=port, username="tester",
        auth_type="password", credential="ignored",
        host_key=host_key, status="active", v2node_installed=False,
    )


@pytest.fixture
def client(crypto, monkeypatch):
    # 密码认证路径会走 crypto.decrypt,这里让它原样返回,便于用假凭据
    monkeypatch.setattr(crypto, "decrypt", lambda token: "pw")
    recorded: list[tuple[str, str]] = []

    async def on_host_key(server, fp):
        recorded.append((server.name, fp))

    c = SSHClient(crypto, timeout=10, on_host_key=on_host_key)
    c.recorded = recorded          # 测试里读
    return c


# ---------- TOFU ----------

async def test_first_connect_records_fingerprint(client, sshd, host_keys):
    port, stats, _ = sshd
    server = _server_obj(port)
    res = await client.run(server, "echo hi")
    assert res.ok and res.stdout == "ran:echo hi"
    # 指纹被记到对象上(游离对象没有 id,所以不触发落库回调)
    assert server.host_key == host_keys[0].get_fingerprint()
    assert client.recorded == []


async def test_persisted_when_server_has_id(client, sshd, host_keys):
    port, _, _ = sshd
    server = _server_obj(port)
    server.id = 7
    await client.run(server, "true")
    assert client.recorded == [("t", host_keys[0].get_fingerprint())]


async def test_matching_fingerprint_passes(client, sshd, host_keys):
    port, _, _ = sshd
    server = _server_obj(port, host_key=host_keys[0].get_fingerprint())
    assert (await client.run(server, "true")).ok


async def test_mismatch_is_refused_before_auth(client, sshd, host_keys):
    """核心性质:指纹对不上时连接在认证前就断,凭据不会外发。"""
    port, stats, _ = sshd
    stats.clear()
    server = _server_obj(port, host_key=host_keys[1].get_fingerprint())

    with pytest.raises(HostKeyMismatch) as exc:
        await client.run(server, "true")

    assert "不一致" in str(exc.value)
    assert host_keys[1].get_fingerprint() in str(exc.value)   # 记录的
    assert host_keys[0].get_fingerprint() in str(exc.value)   # 实际的
    assert stats.get("connections", 0) >= 1        # TCP 层连上了
    assert stats.get("auth_started", 0) == 0       # 但从没进入认证


async def test_mismatch_not_swallowed_by_connectivity_check(client, sshd, host_keys):
    """check_connectivity 会把普通连不上转成 False,但安全问题必须抛出来。"""
    port, _, _ = sshd
    server = _server_obj(port, host_key=host_keys[1].get_fingerprint())
    with pytest.raises(HostKeyMismatch):
        await client.check_connectivity(server)

    # 而端口不通仍然是 False
    dead = _server_obj(1)
    assert await client.check_connectivity(dead) is False


async def test_reset_fingerprint_allows_rebuilt_host(client, sshd, host_keys):
    port, _, _ = sshd
    server = _server_obj(port, host_key=host_keys[1].get_fingerprint())
    with pytest.raises(HostKeyMismatch):
        await client.run(server, "true")
    server.host_key = None                      # 相当于「重置主机密钥」
    assert (await client.run(server, "true")).ok
    assert server.host_key == host_keys[0].get_fingerprint()


# ---------- 连接复用 ----------

async def test_connection_reuses_one_tcp_connection(client, sshd):
    port, stats, _ = sshd
    server = _server_obj(port)
    stats.clear()
    async with client.connection(server) as conn:
        for _ in range(5):
            assert (await conn.run(server, "true")).ok
    assert stats["connections"] == 1


async def test_oneshot_run_opens_one_connection_each(client, sshd):
    port, stats, _ = sshd
    server = _server_obj(port)
    stats.clear()
    for _ in range(3):
        await client.run(server, "true")
    assert stats["connections"] == 3


async def test_session_rejects_other_server(client, sshd):
    port, _, _ = sshd
    server = _server_obj(port)
    other = _server_obj(port)
    other.host = "10.0.0.1"
    async with client.connection(server) as conn:
        with pytest.raises(SSHError, match="不能用来操作"):
            await conn.run(other, "true")


async def test_connection_body_exceptions_are_not_wrapped(client, sshd):
    port, _, _ = sshd
    server = _server_obj(port)

    class Custom(Exception):
        pass

    with pytest.raises(Custom):
        async with client.connection(server):
            raise Custom("安装流程自己的异常要原样上抛")


async def test_check_flag_raises_on_nonzero(client, sshd, monkeypatch):
    async def failing(process):
        process.exit(3)

    # 换一个总是退出码 3 的处理器
    srv = await asyncssh.create_server(
        _Server, "127.0.0.1", 0,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=failing,
    )
    try:
        bad = _server_obj(next(iter(srv.sockets)).getsockname()[1])
        res = await client.run(bad, "boom")
        assert res.exit_status == 3 and not res.ok
        with pytest.raises(SSHError, match="命令执行失败"):
            await client.run(bad, "boom", check=True)
    finally:
        srv.close()


async def test_connect_failure_maps_to_ssherror(client):
    with pytest.raises(SSHError):
        await client.run(_server_obj(1), "true")


# ---------- 私钥文件 ----------

def test_validate_key_path_rejects_bad_paths(tmp_path):
    with pytest.raises(SSHError, match="绝对路径"):
        validate_key_path("relative/key")
    with pytest.raises(SSHError, match="不存在"):
        validate_key_path(str(tmp_path / "nope"))
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(SSHError, match="不是普通文件"):
        validate_key_path(str(d))


def test_validate_key_path_normalizes(tmp_path):
    f = tmp_path / "id_ed25519"
    f.write_text("x")
    assert validate_key_path(f"  {f}  ") == str(f)


def test_key_permission_warning(tmp_path):
    f = tmp_path / "k"
    f.write_text("x")
    f.chmod(0o644)
    assert "权限过松" in key_permission_warning(str(f))
    f.chmod(0o600)
    assert key_permission_warning(str(f)) is None


def test_passphrase_detection_and_check(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    plain, enc = tmp_path / "plain", tmp_path / "enc"
    key.write_private_key(str(plain))
    key.write_private_key(str(enc), passphrase="hunter2")

    assert key_needs_passphrase(str(plain)) is False
    assert key_needs_passphrase(str(enc)) is True

    check_key_passphrase(str(enc), "hunter2")          # 不抛即通过
    with pytest.raises(SSHError, match="口令无法解开"):
        check_key_passphrase(str(enc), "wrong")


def test_unparsable_key_reports_clearly(tmp_path):
    junk = tmp_path / "junk"
    junk.write_text("not a key at all")
    with pytest.raises(SSHError, match="私钥无法解析"):
        key_needs_passphrase(str(junk))


def test_conn_kwargs_passes_passphrase(crypto):
    c = SSHClient(crypto)
    server = Server(
        name="t", host="h", port=22, username="root", auth_type="key",
        credential="/path/key", key_passphrase=crypto.encrypt("hunter2"),
        status="active", v2node_installed=False,
    )
    kwargs = c._conn_kwargs(server)
    assert kwargs["client_keys"] == ["/path/key"]
    assert kwargs["passphrase"] == "hunter2"
    # 关键:不是 known_hosts=None(那等于关掉校验),而是走我们的校验器
    assert callable(kwargs["known_hosts"])
    assert kwargs["known_hosts"]("h", "1.2.3.4", 22) == ([], [], [])


def test_conn_kwargs_rejects_unknown_auth(crypto):
    c = SSHClient(crypto)
    server = Server(
        name="t", host="h", port=22, username="root", auth_type="magic",
        credential="x", status="active", v2node_installed=False,
    )
    with pytest.raises(SSHError, match="未知 auth_type"):
        c._conn_kwargs(server)


def test_session_verify_matches_on_host_port_user():
    sess = SSHSession.__new__(SSHSession)
    sess._target = ("h", 22, "root")
    ok = Server(name="a", host="h", port=22, username="root", auth_type="password",
                credential="x", status="active", v2node_installed=False)
    sess._verify(ok)     # 不抛
    bad = Server(name="b", host="h", port=2222, username="root", auth_type="password",
                 credential="x", status="active", v2node_installed=False)
    with pytest.raises(SSHError):
        sess._verify(bad)
