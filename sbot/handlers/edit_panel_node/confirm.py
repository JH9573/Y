"""收尾:拼 v2board save payload、生成摘要、确认提交、取消。"""
from __future__ import annotations

import json

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ...db import crud
from ...services.v2board_api import V2BoardAPIError, v2node_to_db_row
from ..common import CB_PANEL_NODES, get_ctx
from .options import CONFIRM, KEY, SAVE_FIELDS
from .state import _reply, prompt_for

# ---------- step: CONFIRM ----------

def _compose_payload(data: dict) -> dict[str, Any]:
    """合成最终 save payload:基线 + 用户字段 + advanced。

    v2board V2nodeController::save 的必填字段:group_id, name, host, port,
    server_port, protocol, tls(0/1/2), network(tcp/ws/grpc/...), disable_sni,
    rate。其他按协议特殊处理:
      - shadowsocks: 带 cipher
      - vless: 可选 flow
      - anytls: 服务端会把 tls=0 强制成 1
      - hysteria2: 服务端强制 tls=1,可选 up_mbps / down_mbps
    """
    v = data["values"]
    protocol = v.get("protocol", "shadowsocks")
    if data["mode"] == "edit":
        payload = {k: v0 for k, v0 in data["initial"].items() if k in SAVE_FIELDS}
    else:
        payload = {
            "disable_sni": 0,
            "zero_rtt_handshake": 0,
            "show": 1,
        }

    # 公共字段
    payload.update({
        "protocol": protocol,
        "name": v["name"],
        "host": v["host"],
        "port": v["port"],
        "server_port": v["server_port"],
        "rate": v["rate"],
        "group_id": v["group_id"],
        "parent_id": v.get("parent_id"),
    })

    # 协议分支
    if protocol == "shadowsocks":
        payload["cipher"] = v["cipher"]
        payload["tls"] = v["tls"]
        payload["network"] = v["network"]
    elif protocol == "vless":
        payload["tls"] = v["tls"]
        payload["network"] = v["network"]
        if v.get("flow"):
            payload["flow"] = v["flow"]
        else:
            payload.pop("flow", None)
    elif protocol == "anytls":
        payload["tls"] = 1  # 服务端会强制
        payload["network"] = v["network"]
    elif protocol == "hysteria2":
        payload["tls"] = 1  # 服务端会强制
        payload.setdefault("network", "tcp")  # v2board 校验 network 必填
        if "up_mbps" in v:
            payload["up_mbps"] = v["up_mbps"]
        if "down_mbps" in v:
            payload["down_mbps"] = v["down_mbps"]

    if "tls_settings" in v:
        payload["tls_settings"] = v["tls_settings"]
    if "network_settings" in v:
        payload["network_settings"] = v["network_settings"]
    payload.update(v.get("advanced") or {})
    payload.setdefault("disable_sni", 0)
    payload.setdefault("zero_rtt_handshake", 0)
    return payload


def _summarize(data: dict) -> str:
    payload = _compose_payload(data)
    base_keys = [
        "protocol", "name", "host", "port", "server_port",
        "tls", "network", "rate", "group_id", "parent_id",
    ]
    protocol = payload.get("protocol", "shadowsocks")
    proto_keys: list[str] = {
        "shadowsocks": ["cipher"],
        "vless": ["flow"],
        "anytls": [],
        "hysteria2": ["up_mbps", "down_mbps"],
    }.get(protocol, [])
    show_keys = base_keys[:5] + proto_keys + base_keys[5:]

    lines = ["请确认提交字段:", ""]
    for key in show_keys:
        if key in payload:
            lines.append(f"{key}: {payload.get(key)}")

    extras = {
        k: val for k, val in payload.items()
        if k not in set(show_keys)
    }
    if extras:
        lines.append("")
        lines.append(f"其他: {json.dumps(extras, ensure_ascii=False)}")
    return "\n".join(lines)


@prompt_for("confirm")
async def _prompt_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 提交", callback_data="pnlsave:ok"),
            InlineKeyboardButton("❌ 取消", callback_data="pnlsave:cancel"),
        ]
    ])
    await _reply(update, _summarize(data), reply_markup=kb)
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "pnlsave:cancel":
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消。")
        return ConversationHandler.END

    data = context.user_data[KEY]
    panel_id = data["panel_id"]
    node_id = data.get("node_id")
    action_label = "编辑" if node_id else "新增"
    payload = _compose_payload(data)
    ctx = get_ctx(context)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板已被删除。")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    await query.edit_message_text(f"正在{action_label}…")
    try:
        await ctx.v2board.save_v2node(panel, payload, node_id=node_id)
        ok, msg = True, f"v2node 已{action_label}"
    except V2BoardAPIError as exc:
        ok, msg = False, str(exc)

    sync_info = ""
    if ok:
        # 拉一次新快照同步整张表,顺便拿到新建节点的 id
        try:
            nodes = await ctx.v2board.get_v2nodes(panel)
        except V2BoardAPIError as exc:
            sync_info = f"\n⚠️ 同步缓存失败:{exc}"
        else:
            items = [v2node_to_db_row(n) for n in nodes]
            async with crud.session() as s:
                count = await crud.replace_panel_nodes(s, panel_id, items)
                await s.commit()
            sync_info = f"\n已同步 {count} 个节点到本地缓存。"

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.node.edit" if node_id else "panel.node.add",
            result="success" if ok else "failed",
            detail=(
                f"panel_id={panel_id}, node_id={node_id}, "
                f"protocol=shadowsocks: {msg}"
            ),
        )
        await s.commit()

    prefix = "✅" if ok else "❌"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⬅ 返回列表", callback_data=f"{CB_PANEL_NODES}{panel_id}"
        )]
    ])
    await query.edit_message_text(f"{prefix} {msg}{sync_info}", reply_markup=kb)
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消。")
    return ConversationHandler.END
