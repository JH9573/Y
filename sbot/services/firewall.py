"""服务器端口体检与防火墙放行(仅识别 ufw / firewalld)。

设计原则与其它 services 模块一致:命令在本模块集中构造,端口一律先转 int
再拼接,proto 限定在 {tcp, udp},杜绝注入。检测靠"实测 v2node 进程监听端口",
不依赖 DB 里的 port/server_port。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.ssh import SSHClient, SSHError


# v2node 进程名,用于从 ss / netstat 输出里挑出它监听的端口
_PROC_NAME = "v2node"
_VALID_PROTO = ("tcp", "udp")


@dataclass(frozen=True)
class ListenPort:
    proto: str  # "tcp" | "udp"
    port: int

    def label(self) -> str:
        return f"{self.port}/{self.proto}"


@dataclass
class PortCheck:
    """一次端口体检的结果。"""

    listen_ports: list[ListenPort] = field(default_factory=list)
    manager: str | None = None  # "ufw" | "firewalld" | None
    active: bool = False
    allowed: dict[tuple[str, int], bool] = field(default_factory=dict)
    unallowed: list[ListenPort] = field(default_factory=list)
    note: str | None = None  # 检测被跳过的原因(缺工具 / SSH 错误等)


# ---------- 监听端口检测 ----------

def _extract_port(local: str) -> int | None:
    tail = local.rsplit(":", 1)
    if len(tail) != 2:
        return None
    try:
        return int(tail[1])
    except ValueError:
        return None


def _is_loopback(local: str) -> bool:
    addr = local.rsplit(":", 1)[0]
    return addr.startswith("127.") or addr in ("::1", "[::1]")


def _parse_ss(out: str) -> set[tuple[str, int]]:
    """ss -lntup 输出:Netid State Recv-Q Send-Q Local Peer Process。"""
    found: set[tuple[str, int]] = set()
    for line in out.splitlines():
        if _PROC_NAME not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        if proto not in _VALID_PROTO:
            continue
        local = parts[4]
        if _is_loopback(local):
            continue
        port = _extract_port(local)
        if port is not None:
            found.add((proto, port))
    return found


def _parse_netstat(out: str) -> set[tuple[str, int]]:
    """netstat -lntup 输出:Proto Recv-Q Send-Q Local Foreign State PID/Program。"""
    found: set[tuple[str, int]] = set()
    for line in out.splitlines():
        if _PROC_NAME not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0].lower()
        if proto.startswith("tcp"):
            proto = "tcp"
        elif proto.startswith("udp"):
            proto = "udp"
        else:
            continue
        local = parts[3]
        if _is_loopback(local):
            continue
        port = _extract_port(local)
        if port is not None:
            found.add((proto, port))
    return found


async def detect_listening_ports(
    ssh: SSHClient, server
) -> tuple[list[ListenPort], str | None]:
    """实测 v2node 监听的端口。返回 (端口列表, 跳过原因)。"""
    try:
        res = await ssh.run(
            server,
            "command -v ss >/dev/null 2>&1 && ss -lntup || echo __NOSS__",
        )
    except SSHError as exc:
        return [], f"SSH 错误: {exc}"

    if "__NOSS__" not in res.stdout:
        found = _parse_ss(res.stdout)
    else:
        try:
            res2 = await ssh.run(
                server,
                "command -v netstat >/dev/null 2>&1 && netstat -lntup || echo __NONS__",
            )
        except SSHError as exc:
            return [], f"SSH 错误: {exc}"
        if "__NONS__" in res2.stdout:
            return [], "服务器缺少 ss / netstat"
        found = _parse_netstat(res2.stdout)

    ports = sorted(
        (ListenPort(proto, port) for proto, port in found),
        key=lambda p: (p.port, p.proto),
    )
    return ports, None


# ---------- 防火墙检测 ----------

async def _detect_manager(ssh: SSHClient, server) -> tuple[str | None, bool]:
    """返回 (manager, active)。只认 ufw / firewalld。"""
    res = await ssh.run(
        server,
        "command -v ufw >/dev/null 2>&1 && ufw status || echo __NOUFW__",
    )
    ufw_out = res.stdout
    if "__NOUFW__" not in ufw_out and "Status: active" in ufw_out:
        return "ufw", True

    res2 = await ssh.run(
        server,
        "command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state 2>/dev/null "
        "|| echo __NOFWD__",
    )
    if res2.stdout.strip() == "running":
        return "firewalld", True

    # 装了 ufw 但未启用,也算"识别到管理器但 inactive"
    if "__NOUFW__" not in ufw_out and "Status: inactive" in ufw_out:
        return "ufw", False
    return None, False


def _ufw_status_allows(text: str, proto: str, port: int) -> bool:
    """解析 `ufw status` 文本,判断 port/proto 是否已放行。

    规则行首 token 形如 443/tcp、443(无协议=tcp+udp 都放行)。
    """
    for line in text.splitlines():
        if "ALLOW" not in line.upper():
            continue
        token = line.split()[0]
        if token in (f"{port}/{proto}", str(port)):
            return True
    return False


async def _is_allowed(
    ssh: SSHClient, server, manager: str, proto: str, port: int,
    *, ufw_text: str | None = None,
) -> bool:
    if manager == "ufw":
        text = ufw_text
        if text is None:
            res = await ssh.run(server, "ufw status")
            text = res.stdout
        return _ufw_status_allows(text, proto, port)
    if manager == "firewalld":
        res = await ssh.run(server, f"firewall-cmd --query-port={port}/{proto}")
        return res.ok or res.stdout.strip() == "yes"
    return False


async def check_ports(ssh: SSHClient, server) -> PortCheck:
    """完整体检:监听端口 + 防火墙状态 + 每个端口是否已放行。"""
    ports, note = await detect_listening_ports(ssh, server)
    if note is not None:
        return PortCheck(note=note)

    check = PortCheck(listen_ports=ports)
    try:
        manager, active = await _detect_manager(ssh, server)
    except SSHError as exc:
        check.note = f"防火墙检测失败: {exc}"
        return check
    check.manager = manager
    check.active = active

    if not (manager and active) or not ports:
        return check

    # ufw 用一次 status 文本判断所有端口,少跑几条命令
    ufw_text: str | None = None
    if manager == "ufw":
        try:
            ufw_text = (await ssh.run(server, "ufw status")).stdout
        except SSHError:
            ufw_text = ""

    for lp in ports:
        try:
            ok = await _is_allowed(
                ssh, server, manager, lp.proto, lp.port, ufw_text=ufw_text
            )
        except SSHError:
            ok = False
        check.allowed[(lp.proto, lp.port)] = ok
        if not ok:
            check.unallowed.append(lp)
    return check


# ---------- 放行 ----------

async def open_port(
    ssh: SSHClient, server, manager: str, proto: str, port: int
) -> tuple[bool, str]:
    port = int(port)
    if proto not in _VALID_PROTO:
        return False, f"非法协议 {proto}"
    if manager == "ufw":
        cmd = f"ufw allow {port}/{proto}"
    elif manager == "firewalld":
        cmd = (
            f"firewall-cmd --permanent --add-port={port}/{proto} "
            "&& firewall-cmd --reload"
        )
    else:
        return False, "无可用防火墙管理器"
    try:
        res = await ssh.run(server, cmd, timeout=30)
    except SSHError as exc:
        return False, str(exc)
    return res.ok, res.combined or "ok"


async def open_unallowed(
    ssh: SSHClient, server
) -> tuple[str | None, list[ListenPort], list[tuple[ListenPort, str]]]:
    """重新体检后放行所有未放行端口。返回 (manager, 成功列表, 失败列表)。"""
    check = await check_ports(ssh, server)
    if not (check.manager and check.active):
        return check.manager, [], []
    opened: list[ListenPort] = []
    failed: list[tuple[ListenPort, str]] = []
    for lp in check.unallowed:
        ok, msg = await open_port(ssh, server, check.manager, lp.proto, lp.port)
        if ok:
            opened.append(lp)
        else:
            failed.append((lp, msg))
    return check.manager, opened, failed
