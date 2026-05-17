"""v2node 卸载 —— 系统中唯一的破坏性、不可逆操作。

强制先备份远程 config.json 到 bot 本地,然后执行卸载步骤。
任何调用方都应已经完成"文字输入别名"的二次确认,本模块不再校验。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from ..config import BACKUPS_DIR
from ..core.ssh import SSHClient, SSHError
from ..db.models import Server
from .v2node_config import CONFIG_PATH


log = logging.getLogger(__name__)


class UninstallError(RuntimeError):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message


@dataclass
class UninstallProgress:
    step: str
    detail: str


def _backup_filename(server_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in server_name)
    return BACKUPS_DIR / f"{safe}-{ts}-config.json"


async def _run(ssh: SSHClient, server: Server, cmd: str, step: str, *, check: bool = True) -> None:
    try:
        await ssh.run(server, cmd, check=check)
    except SSHError as exc:
        raise UninstallError(step, str(exc)) from exc


async def uninstall_v2node(
    ssh: SSHClient,
    server: Server,
) -> AsyncIterator[UninstallProgress]:
    """逐步卸载。每一步用 yield 暴露进度,失败抛 UninstallError。"""

    # 1. 备份远程 config.json 到 bot 本地
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_file = _backup_filename(server.name)
    try:
        content = await ssh.read_file(server, CONFIG_PATH)
        backup_file.write_text(content, encoding="utf-8")
        yield UninstallProgress("backup", f"已备份到 {backup_file}")
    except FileNotFoundError:
        # 远程没有配置文件也可以继续卸载,但记录一下
        yield UninstallProgress("backup", "远程无 config.json,跳过备份")
    except (SSHError, OSError) as exc:
        raise UninstallError("backup", f"备份失败: {exc}") from exc

    # 2. 停止服务(允许失败,可能本来就没在跑)
    yield UninstallProgress("stop", "停止 v2node 服务")
    await _run(ssh, server, "systemctl stop v2node || true", "stop", check=False)

    # 3. 禁用开机自启(允许失败)
    yield UninstallProgress("disable", "禁用开机自启")
    await _run(ssh, server, "systemctl disable v2node || true", "disable", check=False)

    # 4. 删除 systemd 服务单元
    yield UninstallProgress("unit", "删除 systemd 服务单元")
    await _run(
        ssh,
        server,
        "rm -f /etc/systemd/system/v2node.service && systemctl daemon-reload",
        "unit",
    )

    # 5. 删除程序目录
    yield UninstallProgress("binaries", "删除 /usr/local/v2node/")
    await _run(ssh, server, "rm -rf /usr/local/v2node", "binaries")

    # 6. 删除配置目录
    yield UninstallProgress("config", "删除 /etc/v2node/")
    await _run(ssh, server, "rm -rf /etc/v2node", "config")

    yield UninstallProgress("done", f"卸载完成,配置已备份到 bot 本地 {backup_file.name}")
