"""handler 共享的工具与上下文容器。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Config
from ..core.crypto import Crypto
from ..core.ssh import SSHClient
from ..services.v2board_api import V2BoardClient


@dataclass
class AppContext:
    """注入到 Telegram bot_data 中,供所有 handler 访问。"""

    config: Config
    crypto: Crypto
    ssh: SSHClient
    v2board: V2BoardClient


CTX_KEY = "app_ctx"


def get_ctx(context) -> AppContext:
    return context.application.bot_data[CTX_KEY]


# 通用 callback_data 前缀,集中管理避免冲突
CB_SERVER_PREFIX = "srv:"  # srv:<id> -> 进入服务器菜单
CB_OPS_PREFIX = "ops:"  # ops:<id>:<action>
CB_OPS_CONFIRM = "opsc:"  # opsc:<id>:<action> -> 二次确认后真正执行
CB_DEL_SERVER = "delsrv:"  # delsrv:<id>
CB_DEL_SERVER_OK = "delsrvok:"  # delsrvok:<id>
CB_INSTALL_START = "inst:"  # inst:<id>
CB_UNINSTALL_START = "uninst:"  # uninst:<id>
CB_NODE_MENU = "nodes:"  # nodes:<server_id>
CB_NODE_ADD = "nodeadd:"  # nodeadd:<server_id>
CB_NODE_DEL = "nodedel:"  # nodedel:<server_id>:<node_pk>
CB_NODE_DEL_OK = "nodedelok:"  # nodedelok:<server_id>:<node_pk>
CB_NODE_SYNC = "nodesync:"  # nodesync:<server_id>
CB_BACK_SERVERS = "back:servers"
CB_PANEL_PREFIX = "pnl:"  # pnl:<id> -> 进入面板菜单
CB_DEL_PANEL = "delpnl:"  # delpnl:<id>
CB_DEL_PANEL_OK = "delpnlok:"  # delpnlok:<id>
CB_BACK_PANELS = "back:panels"
CB_PANEL_NODES = "pnln:"  # pnln:<panel_id> -> v2node 列表
CB_PANEL_NODE = "pnldd:"  # pnldd:<panel_id>:<node_id> -> 节点详情
CB_PANEL_NODE_SHOW = "pnlsh:"  # pnlsh:<panel_id>:<node_id>:<0|1> -> 切换上下架
CB_PANEL_NODE_DROP = "pnldrop:"  # pnldrop:<panel_id>:<node_id> -> 删除二次确认
CB_PANEL_NODE_DROP_OK = "pnldropok:"  # pnldropok:<panel_id>:<node_id> -> 真正删除
CB_PANEL_NODE_SYNC = "pnlsync:"  # pnlsync:<panel_id> -> 从面板同步节点
CB_PANEL_NODE_ADD = "pnladd:"  # pnladd:<panel_id> -> 添加 shadowsocks 节点
CB_PANEL_NODE_EDIT = "pnledit:"  # pnledit:<panel_id>:<node_id> -> 编辑 shadowsocks 节点
CB_NOOP = "noop"


def truncate(text: str, limit: int = 3500) -> str:
    """长输出截断,保护 Telegram 消息长度上限(4096)。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(已截断,共 {len(text)} 字符)"


def humanize_age(when: datetime | None) -> str:
    """把 UTC 时间点转换为相对当前的中文描述(刚刚 / X 分钟前 / X 小时前 / X 天前)。"""
    if when is None:
        return "从未"
    sec = int((datetime.utcnow() - when).total_seconds())
    if sec < 0:
        sec = 0
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"
