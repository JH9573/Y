"""v2node 配置文件(`/etc/v2node/config.json`)读写与节点管理。

核心原则:
- 配置文件的修改仅通过 json 解析 -> 操作对象 -> json.dumps 序列化完成,
  严禁字符串拼接 / 正则替换。
- 每次写回前必须备份;重启失败自动回滚。
- 用户输入只作为 JSON 值写入,基本格式校验由调用方做。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..db.models import Server
from .v2node import IS_ACTIVE_CMD
from ..core.ssh import SSHClient, SSHError


log = logging.getLogger(__name__)

CONFIG_PATH = "/etc/v2node/config.json"
BACKUP_PATH = "/etc/v2node/config.json.bak"
RESTART_CHECK_DELAY = 3  # 重启后等待多少秒再校验


class V2NodeConfigError(RuntimeError):
    """配置 / 节点操作失败。"""


@dataclass(frozen=True)
class NodeEntry:
    """远程 config.json 中 Nodes 数组的一个元素。"""

    api_host: str
    node_id: int
    api_key: str
    timeout: int = 15

    def to_dict(self) -> dict[str, Any]:
        return {
            "ApiHost": self.api_host,
            "NodeID": self.node_id,
            "ApiKey": self.api_key,
            "Timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "NodeEntry":
        try:
            return cls(
                api_host=str(obj["ApiHost"]),
                node_id=int(obj["NodeID"]),
                api_key=str(obj["ApiKey"]),
                timeout=int(obj.get("Timeout", 15)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise V2NodeConfigError(f"节点对象字段缺失或类型错误: {exc}") from exc


# ---------- 输入校验 ----------

_URL_PATTERN = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def validate_api_host(value: str) -> str:
    value = value.strip()
    if not _URL_PATTERN.match(value):
        raise V2NodeConfigError("ApiHost 必须是 http:// 或 https:// 开头的 URL")
    return value


def validate_node_id(value: str | int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise V2NodeConfigError("NodeID 必须是整数") from exc
    if n <= 0:
        raise V2NodeConfigError("NodeID 必须为正整数")
    return n


def validate_api_key(value: str) -> str:
    value = value.strip()
    if not value:
        raise V2NodeConfigError("ApiKey 不能为空")
    return value


# ---------- 远程读写 ----------

async def read_config(ssh: SSHClient, server: Server) -> dict[str, Any]:
    """读取并解析远程 config.json。"""
    raw = await ssh.read_file(server, CONFIG_PATH)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise V2NodeConfigError(f"远程 config.json 解析失败: {exc}") from exc


def serialize_config(cfg: dict[str, Any]) -> str:
    """统一的序列化策略:UTF-8、保留中文、4 空格缩进。"""
    return json.dumps(cfg, ensure_ascii=False, indent=4)


async def backup_remote_config(ssh: SSHClient, server: Server) -> None:
    """在远程把 config.json 复制为 config.json.bak。"""
    # 用 cp -f 确保覆盖;命令固定,无注入面。
    await ssh.run(server, f"cp -f {CONFIG_PATH} {BACKUP_PATH}", check=True)


async def restore_remote_backup(ssh: SSHClient, server: Server) -> None:
    """回滚:用远程 .bak 覆盖 config.json,并重启 v2node。"""
    await ssh.run(server, f"cp -f {BACKUP_PATH} {CONFIG_PATH}", check=True)
    await ssh.run(server, "systemctl restart v2node")


async def write_config(ssh: SSHClient, server: Server, cfg: dict[str, Any]) -> None:
    """序列化并写回远程 config.json。"""
    await ssh.write_file(server, CONFIG_PATH, serialize_config(cfg))


async def restart_and_verify(ssh: SSHClient, server: Server) -> tuple[bool, str]:
    """重启 v2node 并校验运行状态。

    返回 (是否健康, 状态文本)。
    """
    await ssh.run(server, "systemctl restart v2node")
    await asyncio.sleep(RESTART_CHECK_DELAY)
    result = await ssh.run(server, IS_ACTIVE_CMD)
    state = result.stdout.strip()
    return state == "active", state or result.combined


# ---------- 节点操作 ----------

def _find_index(nodes: list[dict[str, Any]], api_host: str, node_id: int) -> int:
    for i, item in enumerate(nodes):
        try:
            if str(item.get("ApiHost")) == api_host and int(item.get("NodeID")) == node_id:
                return i
        except (TypeError, ValueError):
            continue
    return -1


async def add_node_to_config(
    ssh: SSHClient,
    server: Server,
    entry: NodeEntry,
) -> tuple[bool, str]:
    """节点添加:读 -> 改 -> 备份 -> 写回 -> 重启校验 -> 失败回滚。

    返回 (是否成功, 状态文本)。
    """
    cfg = await read_config(ssh, server)
    nodes = cfg.setdefault("Nodes", [])
    if not isinstance(nodes, list):
        raise V2NodeConfigError("config.json 的 Nodes 字段不是数组")

    if _find_index(nodes, entry.api_host, entry.node_id) >= 0:
        raise V2NodeConfigError(
            f"节点 ({entry.api_host}, NodeID={entry.node_id}) 已存在"
        )

    nodes.append(entry.to_dict())

    await backup_remote_config(ssh, server)
    try:
        await write_config(ssh, server, cfg)
    except SSHError:
        # 写入阶段失败,远程文件未必损坏,但保守起见尝试回滚一次
        await _safe_rollback(ssh, server)
        raise

    ok, state = await restart_and_verify(ssh, server)
    if not ok:
        await _safe_rollback(ssh, server)
        return False, f"v2node 启动校验失败({state}),已自动回滚到上一个配置"
    return True, "节点已添加,v2node 重启成功"


async def remove_node_from_config(
    ssh: SSHClient,
    server: Server,
    api_host: str,
    node_id: int,
) -> tuple[bool, str]:
    """节点删除:与添加对称,同样的原子性保证。"""
    cfg = await read_config(ssh, server)
    nodes = cfg.setdefault("Nodes", [])
    if not isinstance(nodes, list):
        raise V2NodeConfigError("config.json 的 Nodes 字段不是数组")

    idx = _find_index(nodes, api_host, node_id)
    if idx < 0:
        raise V2NodeConfigError(
            f"节点 ({api_host}, NodeID={node_id}) 不存在"
        )
    nodes.pop(idx)

    await backup_remote_config(ssh, server)
    try:
        await write_config(ssh, server, cfg)
    except SSHError:
        await _safe_rollback(ssh, server)
        raise

    ok, state = await restart_and_verify(ssh, server)
    if not ok:
        await _safe_rollback(ssh, server)
        return False, f"v2node 启动校验失败({state}),已自动回滚"
    return True, "节点已删除,v2node 重启成功"


async def _safe_rollback(ssh: SSHClient, server: Server) -> None:
    try:
        await restore_remote_backup(ssh, server)
    except Exception:  # noqa: BLE001
        log.exception("回滚 config.json 时再次失败,服务器: %s", server.name)


async def read_remote_nodes(
    ssh: SSHClient,
    server: Server,
) -> list[NodeEntry]:
    """只读:读取远程 Nodes 数组,转成 NodeEntry 列表。

    若 config.json 不存在 / 无法解析,返回空列表(供"导入"场景使用)。
    """
    try:
        cfg = await read_config(ssh, server)
    except FileNotFoundError:
        return []
    except V2NodeConfigError:
        return []
    nodes = cfg.get("Nodes") or []
    if not isinstance(nodes, list):
        return []
    result: list[NodeEntry] = []
    for item in nodes:
        if not isinstance(item, dict):
            continue
        try:
            result.append(NodeEntry.from_dict(item))
        except V2NodeConfigError:
            log.warning("跳过无法解析的节点项: %s", item)
            continue
    return result
