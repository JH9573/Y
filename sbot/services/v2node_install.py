"""v2node 安装 —— 由 bot 以受控步骤完成,不使用官方一键脚本。

每一步都是明确、可控、可观察的操作。任一步失败立即停止并向上抛出 InstallError,
错误信息中包含失败步骤名,便于用户排查。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from ..core.ssh import SSHClient, SSHError
from ..db.models import Server
from .v2node import IS_ACTIVE_CMD
from .v2node_config import CONFIG_PATH, NodeEntry, serialize_config


log = logging.getLogger(__name__)


SYSTEMD_UNIT_PATH = "/etc/systemd/system/v2node.service"
INSTALL_DIR = "/usr/local/v2node"
CONFIG_DIR = "/etc/v2node"

# v2node 的 GitHub release 资产命名:v2node-linux-{arch}.zip
GITHUB_LATEST_API = "https://api.github.com/repos/wyx2685/v2node/releases/latest"
GITHUB_RELEASE_DOWNLOAD = (
    "https://github.com/wyx2685/v2node/releases/download/{tag}/v2node-linux-{arch}.zip"
)


SYSTEMD_UNIT = """[Unit]
Description=v2node Service
After=network.target nss-lookup.target
Wants=network.target

[Service]
User=root
Type=simple
LimitNOFILE=999999
WorkingDirectory=/usr/local/v2node/
ExecStart=/usr/local/v2node/v2node server
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


class InstallError(RuntimeError):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message


@dataclass
class InstallProgress:
    step: str
    detail: str


@dataclass
class InstallParams:
    first_node: NodeEntry
    version: str | None = None  # None 表示取 latest


async def _run(ssh: SSHClient, server: Server, cmd: str, step: str) -> str:
    try:
        res = await ssh.run(server, cmd, check=True)
    except SSHError as exc:
        raise InstallError(step, str(exc)) from exc
    return res.stdout


async def _detect_arch(ssh: SSHClient, server: Server) -> str:
    out = await _run(ssh, server, "uname -m", "arch")
    machine = out.strip()
    if machine == "x86_64":
        return "64"
    if machine in ("aarch64", "arm64"):
        return "arm64-v8a"
    raise InstallError("arch", f"暂不支持的 CPU 架构: {machine}")


async def _precheck_and_clean_stale(ssh: SSHClient, server: Server) -> bool:
    """检测前次失败安装遗留的状态;若存在则清理,以便重试。

    本函数只在 bot 端已认定该服务器未安装(server.v2node_installed=False)的
    路径下被调用,因此发现残留即视为前次安装中途失败的产物,直接清理。

    返回 True 表示清理过残留,便于上层产出额外进度提示。
    """
    res = await ssh.run(
        server,
        f"test -e {INSTALL_DIR}/v2node -o -e {SYSTEMD_UNIT_PATH} -o -e {CONFIG_DIR} "
        "&& echo yes || echo no",
    )
    if res.stdout.strip() != "yes":
        return False
    # 残留来自上一次失败的安装,清理后才能重新安装。允许每一步失败:
    # 例如服务从未注册过则 stop / disable 会报错,不应阻塞重试。
    cleanup = (
        "systemctl stop v2node 2>/dev/null || true; "
        "systemctl disable v2node 2>/dev/null || true; "
        f"rm -f {SYSTEMD_UNIT_PATH}; "
        "systemctl daemon-reload 2>/dev/null || true; "
        f"rm -rf {INSTALL_DIR} {CONFIG_DIR}"
    )
    try:
        await ssh.run(server, cleanup, check=True)
    except SSHError as exc:
        raise InstallError("precheck", f"清理上次失败安装的残留失败: {exc}") from exc
    return True


async def _ensure_deps(ssh: SSHClient, server: Server) -> str:
    """检测并安装 curl / unzip。返回使用的下载工具名("curl" 或 "wget")。"""
    res = await ssh.run(
        server,
        "command -v curl >/dev/null 2>&1 && echo curl || "
        "(command -v wget >/dev/null 2>&1 && echo wget || echo none)",
    )
    fetcher = res.stdout.strip()
    unzip_res = await ssh.run(server, "command -v unzip >/dev/null 2>&1 && echo yes || echo no")
    has_unzip = unzip_res.stdout.strip() == "yes"

    if fetcher == "none" or not has_unzip:
        # 通过 apt-get 安装。仅支持 Debian 系。
        pkgs = []
        if fetcher == "none":
            pkgs.append("curl")
            fetcher = "curl"
        if not has_unzip:
            pkgs.append("unzip")
        cmd = (
            "DEBIAN_FRONTEND=noninteractive apt-get update -y && "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(pkgs)}"
        )
        await _run(ssh, server, cmd, "deps")
    return fetcher


async def _resolve_version(ssh: SSHClient, server: Server, fetcher: str, version: str | None) -> str:
    if version:
        return version.lstrip("v") if version.startswith("v") else version
    # 从 GitHub API 取最新 tag
    if fetcher == "curl":
        cmd = f"curl -fsSL {GITHUB_LATEST_API}"
    else:
        cmd = f"wget -qO- {GITHUB_LATEST_API}"
    out = await _run(ssh, server, cmd, "version")
    try:
        data = json.loads(out)
        tag = str(data["tag_name"])
    except (json.JSONDecodeError, KeyError) as exc:
        raise InstallError("version", f"无法从 GitHub API 解析最新版本: {exc}") from exc
    return tag


async def _download_and_extract(
    ssh: SSHClient, server: Server, fetcher: str, tag: str, arch: str
) -> None:
    url = GITHUB_RELEASE_DOWNLOAD.format(tag=tag, arch=arch)
    tmp_zip = "/tmp/v2node-install.zip"
    tmp_dir = "/tmp/v2node-install"

    if fetcher == "curl":
        dl_cmd = f"curl -fL -o {tmp_zip} {url}"
    else:
        dl_cmd = f"wget -O {tmp_zip} {url}"

    await _run(ssh, server, dl_cmd, "download")
    await _run(
        ssh,
        server,
        f"rm -rf {tmp_dir} && mkdir -p {tmp_dir} && unzip -o {tmp_zip} -d {tmp_dir}",
        "unzip",
    )
    await _run(
        ssh,
        server,
        f"mkdir -p {INSTALL_DIR} {CONFIG_DIR} && "
        f"cp -f {tmp_dir}/v2node {INSTALL_DIR}/v2node && "
        f"chmod +x {INSTALL_DIR}/v2node && "
        f"cp -f {tmp_dir}/geoip.dat {CONFIG_DIR}/geoip.dat 2>/dev/null || true && "
        f"cp -f {tmp_dir}/geosite.dat {CONFIG_DIR}/geosite.dat 2>/dev/null || true",
        "deploy",
    )
    # 清理临时文件
    await ssh.run(server, f"rm -rf {tmp_zip} {tmp_dir}")


async def _write_systemd_unit(ssh: SSHClient, server: Server) -> None:
    try:
        await ssh.write_file(server, SYSTEMD_UNIT_PATH, SYSTEMD_UNIT)
    except SSHError as exc:
        raise InstallError("systemd", str(exc)) from exc


async def _write_initial_config(
    ssh: SSHClient, server: Server, first_node: NodeEntry
) -> None:
    cfg = {
        "Log": {"Level": "warning", "Output": "", "Access": "none"},
        "Nodes": [first_node.to_dict()],
    }
    try:
        await ssh.write_file(server, CONFIG_PATH, serialize_config(cfg))
    except SSHError as exc:
        raise InstallError("config", str(exc)) from exc


async def _enable_and_start(ssh: SSHClient, server: Server) -> None:
    await _run(ssh, server, "systemctl daemon-reload", "daemon-reload")
    await _run(ssh, server, "systemctl enable v2node", "enable")
    await _run(ssh, server, "systemctl start v2node", "start")
    # 等待数秒后校验
    await asyncio.sleep(3)
    res = await ssh.run(server, IS_ACTIVE_CMD)
    if res.stdout.strip() != "active":
        raise InstallError(
            "verify",
            f"v2node 启动后非 active({res.stdout.strip() or res.combined})",
        )


async def install_v2node(
    ssh: SSHClient,
    server: Server,
    params: InstallParams,
) -> AsyncIterator[InstallProgress]:
    """执行完整安装流程,以异步生成器方式产出每一步的进度。"""
    yield InstallProgress("precheck", "检查目标服务器现有状态")
    cleaned = await _precheck_and_clean_stale(ssh, server)
    if cleaned:
        yield InstallProgress(
            "precheck",
            "检测到上一次失败安装的残留,已清理(停止服务/移除 systemd 单元/删除安装目录与配置目录)",
        )

    yield InstallProgress("arch", "识别 CPU 架构")
    arch = await _detect_arch(ssh, server)

    yield InstallProgress("deps", "检测并安装依赖(curl / unzip)")
    fetcher = await _ensure_deps(ssh, server)

    yield InstallProgress("version", "确定 v2node 版本")
    tag = await _resolve_version(ssh, server, fetcher, params.version)

    yield InstallProgress("download", f"下载 v2node-linux-{arch}.zip ({tag})")
    await _download_and_extract(ssh, server, fetcher, tag, arch)

    yield InstallProgress("systemd", "写入 systemd 服务单元")
    await _write_systemd_unit(ssh, server)

    yield InstallProgress("config", "写入初始配置")
    await _write_initial_config(ssh, server, params.first_node)

    yield InstallProgress("start", "注册并启动 v2node")
    await _enable_and_start(ssh, server)

    yield InstallProgress("done", f"安装完成,版本 {tag}")
