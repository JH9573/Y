"""v2node 服务级操作白名单。

将所有可执行命令固化在静态映射中,handler 只允许通过 action 标识查表,
不接受外部传入的裸字符串,从根本上消除注入与越权风险。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class V2NodeAction:
    label: str
    command: str
    dangerous: bool


ACTIONS: dict[str, V2NodeAction] = {
    "v2node.status": V2NodeAction(
        label="状态",
        command="systemctl status v2node --no-pager",
        dangerous=False,
    ),
    "v2node.start": V2NodeAction(
        label="启动",
        command="systemctl start v2node",
        dangerous=False,
    ),
    "v2node.restart": V2NodeAction(
        label="重启",
        command="systemctl restart v2node",
        dangerous=True,
    ),
    "v2node.stop": V2NodeAction(
        label="停止",
        command="systemctl stop v2node",
        dangerous=True,
    ),
    "v2node.logs": V2NodeAction(
        label="日志",
        command="journalctl -u v2node -n 50 --no-pager",
        dangerous=False,
    ),
    "v2node.version": V2NodeAction(
        label="版本",
        command="/usr/local/v2node/v2node version",
        dangerous=False,
    ),
}


# 内部使用:校验服务启动是否成功
IS_ACTIVE_CMD = "systemctl is-active v2node"

# 检查 v2node 是否已安装(二进制存在即认为安装)
INSTALLED_CHECK_CMD = "test -x /usr/local/v2node/v2node && echo installed || echo missing"


def get_action(action: str) -> V2NodeAction:
    if action not in ACTIONS:
        raise KeyError(f"未授权的操作: {action}")
    return ACTIONS[action]
