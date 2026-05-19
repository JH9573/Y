"""添加 / 编辑面板 v2node (shadowsocks) 节点的对话流。

入口:
- 节点列表 [➕ 添加 shadowsocks]  -> 新增
- 节点详情 [✏️ 编辑] (仅 protocol==shadowsocks 节点)-> 编辑

简单字段对话引导,可选高级字段统一贴 JSON。提交成功后调用一次
get_v2nodes 同步整张缓存表,保证本地与面板一致。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..db import crud
from ..services.v2board_api import (
    V2BoardAPIError,
    v2node_to_db_row,
)
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_PANEL_NODE_ADD,
    CB_PANEL_NODE_EDIT,
    CB_PANEL_NODES,
    NON_MENU_TEXT_FILTER,
    get_ctx,
)


log = logging.getLogger(__name__)


(
    NAME,
    HOST,
    PORT,
    SERVER_PORT,
    CIPHER,
    TLS,
    NETWORK,
    NET_SETTINGS,
    RATE,
    PARENT,
    GROUPS,
    ADVANCED,
    CONFIRM,
) = range(13)

# 父节点选择按钮一屏最多展示这么多;v2board 一般够用
PARENT_LIST_LIMIT = 30

KEY = "pnlsave"

CIPHER_OPTIONS = [
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-256-gcm",
    "chacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
]
TLS_OPTIONS: list[tuple[int, str]] = [(0, "关闭"), (1, "TLS")]
# shadowsocks 只有两种传输:原生 tcp 或 http 伪装
NETWORK_OPTIONS: list[tuple[str, str]] = [("tcp", "tcp"), ("http", "http伪装")]
NETWORK_VALUES = {v for v, _ in NETWORK_OPTIONS}

# v2board V2nodeController::save 接受的字段白名单
SAVE_FIELDS = {
    "group_id", "route_id", "name", "parent_id", "host", "listen_ip",
    "port", "server_port", "protocol", "tls", "tls_settings", "flow",
    "network", "network_settings", "encryption", "encryption_settings",
    "disable_sni", "udp_relay_mode", "zero_rtt_handshake",
    "congestion_control", "cipher", "up_mbps", "down_mbps", "obfs",
    "obfs_password", "padding_scheme", "tags", "rate", "show", "sort",
}

KEEP_CB = "pnlsave:keep"
HOST_PICK_CB = "pnlsave:hs:"  # pnlsave:hs:<server_id>
HOST_MANUAL_CB = "pnlsave:hm"


# ---------- 通用 helper ----------

async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """callback 上下文 edit 原消息;文本上下文回复新消息。"""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=reply_markup
        )


def _keep_kb(value: Any) -> InlineKeyboardMarkup:
    """文本 state 的"保留当前值"按钮。"""
    label = f"保留 ({value})" if value not in (None, "") else "保留 (空)"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=KEEP_CB)]]
    )


def _is_edit(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data[KEY]["mode"] == "edit"


# ---------- 入口 ----------

async def cb_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return ConversationHandler.END

    context.user_data[KEY] = {
        "mode": "add",
        "panel_id": panel_id,
        "node_id": None,
        "initial": {},
        "values": {},
    }
    await query.edit_message_text(
        f"在面板「{panel.name}」上添加 shadowsocks v2node 节点。\n"
        "任意时刻可发送 /cancel 中止。\n\n"
        "请输入节点名称:"
    )
    return NAME


async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, node_id_s = payload.split(":", 1)
    panel_id, node_id = int(panel_id_s), int(node_id_s)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        node = await crud.get_panel_node(s, panel_id, node_id)
    if panel is None or node is None:
        await query.edit_message_text("面板或节点不存在。")
        return ConversationHandler.END
    if node.protocol != "shadowsocks":
        await query.edit_message_text(
            f"当前仅支持编辑 shadowsocks 协议,该节点为 {node.protocol}。"
        )
        return ConversationHandler.END

    try:
        raw = json.loads(node.raw_json or "{}")
        if not isinstance(raw, dict):
            raw = {}
    except (json.JSONDecodeError, TypeError):
        raw = {}

    # 用 raw_json 作为基线,补齐结构化字段以防 raw 缺失
    initial: dict[str, Any] = {k: v for k, v in raw.items() if k in SAVE_FIELDS}
    initial.setdefault("name", node.name)
    initial.setdefault("host", node.host)
    initial.setdefault("port", node.port)
    initial.setdefault("server_port", node.server_port)
    initial.setdefault("cipher", "aes-128-gcm")
    initial.setdefault("tls", node.tls if node.tls is not None else 0)
    initial.setdefault("network", node.network or "tcp")
    initial.setdefault("rate", node.rate if node.rate is not None else "1")
    initial.setdefault("group_id", [])
    initial.setdefault("parent_id", node.parent_id)

    context.user_data[KEY] = {
        "mode": "edit",
        "panel_id": panel_id,
        "node_id": node_id,
        "initial": initial,
        "values": {},
    }
    await query.edit_message_text(
        f"编辑面板「{panel.name}」的 v2node #{node_id} (shadowsocks)。\n"
        "任意时刻可发送 /cancel 中止;\n"
        "每步可点「保留 (xxx)」沿用当前值。\n\n"
        f"请输入节点名称(当前: {initial['name']}):",
        reply_markup=_keep_kb(initial["name"]),
    )
    return NAME


# ---------- step: NAME ----------

async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        text = str(data["initial"].get("name", "")).strip()
    else:
        text = (update.message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("名称不能为空,请重新输入:")
        return NAME
    if len(text) > 128:
        await update.effective_message.reply_text("名称过长(最多 128 字符):")
        return NAME
    data["values"]["name"] = text
    return await _prompt_host(update, context)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _prompt_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    async with crud.session() as s:
        servers = await crud.list_servers(s)

    rows: list[list[InlineKeyboardButton]] = []
    for srv in servers:
        if not srv.host:
            continue
        label = _truncate(f"🖥 {srv.name} ({srv.host})", 60)
        rows.append([InlineKeyboardButton(
            label, callback_data=f"{HOST_PICK_CB}{srv.id}"
        )])
    if _is_edit(context):
        val = data["initial"].get("host", "")
        keep_label = f"保留 ({val})" if val not in (None, "") else "保留 (空)"
        rows.append([InlineKeyboardButton(keep_label, callback_data=KEEP_CB)])
    rows.append([InlineKeyboardButton("✏️ 手动输入", callback_data=HOST_MANUAL_CB)])

    if _is_edit(context):
        val = data["initial"].get("host", "")
        prompt = f"请选择节点地址 host(当前: {val}),或手动输入:"
    else:
        prompt = "请选择节点地址 host(IP 或域名),或手动输入:"
    if not servers:
        prompt += "\n(暂无已登记服务器,请点「✏️ 手动输入」)"

    await _reply(update, prompt, reply_markup=InlineKeyboardMarkup(rows))
    return HOST


# ---------- step: HOST ----------

async def step_host_pick_server(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":")[-1])
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None or not server.host:
        await query.edit_message_text("该服务器已被删除或地址为空,请重新选择。")
        return await _prompt_host(update, context)
    data = context.user_data[KEY]
    data["values"]["host"] = server.host
    return await _prompt_port(update, context)


async def step_host_manual(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请输入节点地址 host(IP 或域名):")
    return HOST


async def step_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        text = str(data["initial"].get("host", "")).strip()
    else:
        text = (update.message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("地址不能为空,请重新输入:")
        return HOST
    data["values"]["host"] = text
    return await _prompt_port(update, context)


async def _prompt_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if _is_edit(context):
        val = data["initial"].get("port", "")
        await _reply(
            update,
            f"请输入连接端口 port(当前: {val}):",
            reply_markup=_keep_kb(val),
        )
    else:
        await _reply(update, "请输入连接端口 port(1-65535):")
    return PORT


# ---------- step: PORT ----------

def _parse_port(text: str) -> int | None:
    try:
        n = int(text)
    except ValueError:
        return None
    if not (1 <= n <= 65535):
        return None
    return n


async def step_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        port = _parse_port(str(data["initial"].get("port", "")))
        if port is None:
            await update.callback_query.message.reply_text(
                "当前 port 值不合法,请重新输入:"
            )
            return PORT
    else:
        port = _parse_port(update.message.text or "")
        if port is None:
            await update.message.reply_text("端口必须是 1-65535 的整数,请重新输入:")
            return PORT
    data["values"]["port"] = port
    return await _prompt_server_port(update, context)


async def _prompt_server_port(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    if _is_edit(context):
        val = data["initial"].get("server_port", "")
        await _reply(
            update,
            f"请输入后端端口 server_port(当前: {val}):",
            reply_markup=_keep_kb(val),
        )
    else:
        await _reply(update, "请输入后端端口 server_port(1-65535):")
    return SERVER_PORT


# ---------- step: SERVER_PORT ----------

async def step_server_port(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        port = _parse_port(str(data["initial"].get("server_port", "")))
        if port is None:
            await update.callback_query.message.reply_text(
                "当前 server_port 值不合法,请重新输入:"
            )
            return SERVER_PORT
    else:
        port = _parse_port(update.message.text or "")
        if port is None:
            await update.message.reply_text("端口必须是 1-65535 的整数,请重新输入:")
            return SERVER_PORT
    data["values"]["server_port"] = port
    return await _prompt_cipher(update, context)


# ---------- step: CIPHER ----------

async def _prompt_cipher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    rows = [
        [InlineKeyboardButton(c, callback_data=f"pnlsave:c:{c}")]
        for c in CIPHER_OPTIONS
    ]
    if _is_edit(context):
        current = data["initial"].get("cipher", "aes-128-gcm")
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update,
        "请选择加密方式 cipher:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return CIPHER


async def step_cipher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == KEEP_CB:
        cipher = str(data["initial"].get("cipher", "aes-128-gcm"))
    else:
        cipher = query.data.split(":", 2)[2]
    if cipher not in CIPHER_OPTIONS:
        await query.message.reply_text("无效的加密方式,请重新选择:")
        return CIPHER
    data["values"]["cipher"] = cipher
    return await _prompt_tls(update, context)


# ---------- step: TLS ----------

async def _prompt_tls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    rows = [[
        InlineKeyboardButton(f"{label} ({v})", callback_data=f"pnlsave:t:{v}")
        for v, label in TLS_OPTIONS
    ]]
    if _is_edit(context):
        current = data["initial"].get("tls", 0)
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update, "请选择 TLS:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return TLS


async def step_tls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == KEEP_CB:
        tls = int(data["initial"].get("tls", 0))
    else:
        tls = int(query.data.split(":", 2)[2])
    if tls not in (0, 1):
        await query.message.reply_text("TLS 必须是 0 或 1。")
        return TLS
    data["values"]["tls"] = tls
    return await _prompt_network(update, context)


# ---------- step: NETWORK ----------

async def _prompt_network(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rows = [[
        InlineKeyboardButton(label, callback_data=f"pnlsave:n:{value}")
        for value, label in NETWORK_OPTIONS
    ]]
    if _is_edit(context):
        current = data["initial"].get("network", "tcp")
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update,
        "请选择传输协议 network:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return NETWORK


async def step_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == KEEP_CB:
        # 兼容旧数据,保留路径直接信任 initial 值
        net = str(data["initial"].get("network", "tcp"))
    else:
        net = query.data.split(":", 2)[2]
        if net not in NETWORK_VALUES:
            await query.message.reply_text("无效的 network。")
            return NETWORK
    data["values"]["network"] = net
    return await _prompt_net_settings(update, context)


# ---------- step: NET_SETTINGS ----------

def _format_net_settings(value: Any) -> str:
    if value in (None, "", {}):
        return "(空)"
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


async def _prompt_net_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    net = data["values"].get("network", "tcp")
    if net == "http":
        sample = (
            "{\n"
            '  "header": {\n'
            '    "type": "http",\n'
            '    "request": {\n'
            '      "path": ["/"],\n'
            '      "headers": {"Host": ["www.bing.com"]}\n'
            "    }\n"
            "  }\n"
            "}"
        )
    else:
        sample = "{}"

    buttons = [[InlineKeyboardButton(
        "跳过", callback_data="pnlsave:nsskip"
    )]]
    if _is_edit(context):
        current = data["initial"].get("network_settings")
        buttons.append([InlineKeyboardButton(
            f"保留 ({_format_net_settings(current)})", callback_data=KEEP_CB
        )])

    text = (
        f"可选:贴入 network_settings JSON(network = {net})。\n"
        f"示例:\n\n{sample}\n\n"
        "点「跳过」表示不带该字段。"
    )
    await _reply(update, text, reply_markup=InlineKeyboardMarkup(buttons))
    return NET_SETTINGS


async def step_net_settings_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    # 跳过 / 保留 都不向 values 写入 network_settings:
    # - 新增:payload 不含该字段,由面板使用默认值
    # - 编辑:沿用面板上当前值,不动
    # 之前写 {} 会被 v2board (PHP) 解码成空数组 [] 入库,与按钮文案"不带该字段"不一致。
    return await _prompt_rate(update, context)


async def step_net_settings_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(
            "空白,请重新输入或点上一条消息的「跳过」:"
        )
        return NET_SETTINGS
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        await update.message.reply_text(
            f"JSON 解析失败:{exc}\n请重新输入:"
        )
        return NET_SETTINGS
    if not isinstance(parsed, dict):
        await update.message.reply_text(
            "network_settings 必须是 JSON 对象,请重新输入:"
        )
        return NET_SETTINGS
    context.user_data[KEY]["values"]["network_settings"] = parsed
    return await _prompt_rate(update, context)


# ---------- step: RATE ----------

async def _prompt_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if _is_edit(context):
        val = data["initial"].get("rate", "1")
        await _reply(
            update,
            f"请输入倍率 rate(当前: {val}):",
            reply_markup=_keep_kb(val),
        )
    else:
        await _reply(update, "请输入倍率 rate(例如 1 或 1.5):")
    return RATE


def _parse_rate(text: str) -> float | None:
    try:
        v = float(text)
    except ValueError:
        return None
    if v < 0:
        return None
    return v


async def step_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        rate = _parse_rate(str(data["initial"].get("rate", "1")))
        if rate is None:
            await update.callback_query.message.reply_text(
                "当前 rate 值不合法,请重新输入:"
            )
            return RATE
    else:
        rate = _parse_rate(update.message.text or "")
        if rate is None:
            await update.message.reply_text("rate 必须是 ≥0 的数字,请重新输入:")
            return RATE
    data["values"]["rate"] = rate
    return await _prompt_parent(update, context)


# ---------- step: PARENT ----------

async def _prompt_parent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    panel_id = data["panel_id"]
    self_node_id = data.get("node_id")

    async with crud.session() as s:
        nodes = await crud.list_panel_nodes(s, panel_id)

    # 编辑时排除自己,避免自指
    candidates = [n for n in nodes if n.node_id != self_node_id]

    rows: list[list[InlineKeyboardButton]] = []
    for n in candidates[:PARENT_LIST_LIMIT]:
        relay = "🔁" if n.parent_id else ""
        show = "✅" if n.show else "❌"
        label = f"{show}{relay} #{n.node_id} {n.name}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"pnlsave:p:{n.node_id}"
        )])

    rows.append([InlineKeyboardButton(
        "🚫 不选父节点", callback_data="pnlsave:p:none"
    )])
    if _is_edit(context):
        current = data["initial"].get("parent_id")
        label = (
            f"保留 (#{current})" if current not in (None, "", 0)
            else "保留 (无)"
        )
        rows.append([InlineKeyboardButton(label, callback_data=KEEP_CB)])

    lines = ["请选择父节点(用于中转节点),也可点「🚫 不选父节点」跳过:"]
    if not candidates:
        lines.append("")
        lines.append("(本地暂无其他节点,可直接「不选父节点」)")
    elif len(candidates) > PARENT_LIST_LIMIT:
        lines.append("")
        lines.append(
            f"(共 {len(candidates)} 个,仅显示前 {PARENT_LIST_LIMIT};"
            "如需更精确请用「跳过」并在高级 JSON 中填写 parent_id。)"
        )

    await _reply(
        update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )
    return PARENT


async def step_parent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]

    if query.data == KEEP_CB:
        initial = data["initial"].get("parent_id")
        parent_id: int | None
        if initial in (None, "", 0):
            parent_id = None
        else:
            try:
                parent_id = int(initial)
            except (TypeError, ValueError):
                parent_id = None
    else:
        token = query.data.split(":", 2)[2]
        if token == "none":
            parent_id = None
        else:
            try:
                parent_id = int(token)
            except ValueError:
                await query.message.reply_text("无效的父节点选择,请重选:")
                return PARENT
            if parent_id == data.get("node_id"):
                await query.answer("不能选择自身作为父节点", show_alert=True)
                return PARENT

    data["values"]["parent_id"] = parent_id
    return await _prompt_groups(update, context)


# ---------- step: GROUPS ----------

async def _prompt_groups(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    async with crud.session() as s:
        panel = await crud.get_panel(s, data["panel_id"])
    try:
        groups = await ctx.v2board.get_groups(panel)
    except V2BoardAPIError as exc:
        await update.effective_message.reply_text(
            f"❌ 拉取权限组失败:{exc}\n请稍后重试。"
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    if not groups:
        await update.effective_message.reply_text(
            "面板上未配置权限组,请先在面板上创建后再添加节点。"
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    # 用 v2board 返回的顺序展示。初值来自 initial.group_id(编辑模式)。
    initial_gids: set[int] = set()
    for gid in data["initial"].get("group_id") or []:
        try:
            initial_gids.add(int(gid))
        except (TypeError, ValueError):
            continue

    data["groups"] = [
        {"id": int(g["id"]), "name": str(g.get("name") or g["id"])}
        for g in groups if g.get("id") is not None
    ]
    data["selected_groups"] = {
        gid for gid in initial_gids
        if gid in {g["id"] for g in data["groups"]}
    }

    await update.effective_message.reply_text(
        "请选择权限组(可多选,至少选 1 个),完成后点「✅ 完成」:",
        reply_markup=InlineKeyboardMarkup(_groups_buttons(data)),
    )
    return GROUPS


def _groups_buttons(data: dict) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    selected: set[int] = data["selected_groups"]
    for g in data["groups"]:
        mark = "☑" if g["id"] in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} #{g['id']} {g['name']}",
            callback_data=f"pnlsave:g:{g['id']}",
        )])
    rows.append([InlineKeyboardButton(
        "✅ 完成", callback_data="pnlsave:gdone"
    )])
    return rows


async def step_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    parts = query.data.split(":")

    if parts[1] == "gdone":
        if not data["selected_groups"]:
            await query.answer("请至少选择一个权限组", show_alert=True)
            return GROUPS
        data["values"]["group_id"] = sorted(data["selected_groups"])
        return await _prompt_advanced(update, context)

    gid = int(parts[2])
    if gid in data["selected_groups"]:
        data["selected_groups"].remove(gid)
    else:
        data["selected_groups"].add(gid)
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup(_groups_buttons(data))
    )
    return GROUPS


# ---------- step: ADVANCED ----------

async def _prompt_advanced(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    sample = (
        "{\n"
        '  "tags": ["hk"],\n'
        '  "parent_id": null,\n'
        '  "show": 1,\n'
        '  "sort": 1\n'
        "}"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("跳过", callback_data="pnlsave:advskip")]]
    )
    text = (
        "可选:贴入高级字段 JSON 对象(覆盖任何同名字段),例如:\n\n"
        f"{sample}\n\n"
        "或点「跳过」使用默认值。"
    )
    await _reply(update, text, reply_markup=kb)
    return ADVANCED


async def step_advanced_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data[KEY]["values"]["advanced"] = {}
    return await _prompt_confirm(update, context)


async def step_advanced_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("空白,请重新输入或点上一条消息的「跳过」:")
        return ADVANCED
    try:
        adv = json.loads(text)
    except json.JSONDecodeError as exc:
        await update.message.reply_text(f"JSON 解析失败:{exc}\n请重新输入:")
        return ADVANCED
    if not isinstance(adv, dict):
        await update.message.reply_text("必须是 JSON 对象,请重新输入:")
        return ADVANCED
    unknown = [k for k in adv if k not in SAVE_FIELDS]
    if unknown:
        await update.message.reply_text(
            f"含未识别字段: {', '.join(unknown)}\n"
            f"允许字段: {', '.join(sorted(SAVE_FIELDS))}\n"
            "请去掉后重新输入:"
        )
        return ADVANCED
    context.user_data[KEY]["values"]["advanced"] = adv
    return await _prompt_confirm(update, context)


# ---------- step: CONFIRM ----------

def _compose_payload(data: dict) -> dict[str, Any]:
    """合成最终 save payload:基线 + 用户字段 + advanced。"""
    if data["mode"] == "edit":
        payload = {k: v for k, v in data["initial"].items() if k in SAVE_FIELDS}
    else:
        payload = {
            "protocol": "shadowsocks",
            "disable_sni": 0,
            "zero_rtt_handshake": 0,
            "show": 1,
        }
    v = data["values"]
    payload.update({
        "protocol": "shadowsocks",
        "name": v["name"],
        "host": v["host"],
        "port": v["port"],
        "server_port": v["server_port"],
        "cipher": v["cipher"],
        "tls": v["tls"],
        "network": v["network"],
        "rate": v["rate"],
        "group_id": v["group_id"],
        "parent_id": v.get("parent_id"),
    })
    if "network_settings" in v:
        payload["network_settings"] = v["network_settings"]
    payload.update(v.get("advanced") or {})
    payload.setdefault("disable_sni", 0)
    payload.setdefault("zero_rtt_handshake", 0)
    return payload


def _summarize(data: dict) -> str:
    payload = _compose_payload(data)
    lines = ["请确认提交字段:", ""]
    for key in (
        "protocol", "name", "host", "port", "server_port",
        "cipher", "tls", "network", "rate", "group_id", "parent_id",
    ):
        lines.append(f"{key}: {payload.get(key)}")
    extras = {
        k: v for k, v in payload.items()
        if k not in {
            "protocol", "name", "host", "port", "server_port",
            "cipher", "tls", "network", "rate", "group_id", "parent_id",
        }
    }
    if extras:
        lines.append("")
        lines.append(f"其他: {json.dumps(extras, ensure_ascii=False)}")
    return "\n".join(lines)


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


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_add_entry, pattern=f"^{CB_PANEL_NODE_ADD}\\d+$"
            ),
            CallbackQueryHandler(
                cb_edit_entry, pattern=f"^{CB_PANEL_NODE_EDIT}\\d+:\\d+$"
            ),
        ],
        states={
            NAME: [
                CallbackQueryHandler(step_name, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_name),
            ],
            HOST: [
                CallbackQueryHandler(
                    step_host_pick_server, pattern=rf"^{HOST_PICK_CB}\d+$"
                ),
                CallbackQueryHandler(
                    step_host_manual, pattern=f"^{HOST_MANUAL_CB}$"
                ),
                CallbackQueryHandler(step_host, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_host),
            ],
            PORT: [
                CallbackQueryHandler(step_port, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_port),
            ],
            SERVER_PORT: [
                CallbackQueryHandler(step_server_port, pattern=f"^{KEEP_CB}$"),
                MessageHandler(
                    NON_MENU_TEXT_FILTER, step_server_port
                ),
            ],
            CIPHER: [
                CallbackQueryHandler(
                    step_cipher,
                    pattern=r"^pnlsave:(c:[a-z0-9\-]+|keep)$",
                ),
            ],
            TLS: [
                CallbackQueryHandler(
                    step_tls, pattern=r"^pnlsave:(t:[01]|keep)$"
                ),
            ],
            NETWORK: [
                CallbackQueryHandler(
                    step_network,
                    pattern=r"^pnlsave:(n:(tcp|http)|keep)$",
                ),
            ],
            NET_SETTINGS: [
                CallbackQueryHandler(
                    step_net_settings_skip,
                    pattern=r"^pnlsave:(nsskip|keep)$",
                ),
                MessageHandler(
                    NON_MENU_TEXT_FILTER, step_net_settings_text
                ),
            ],
            RATE: [
                CallbackQueryHandler(step_rate, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_rate),
            ],
            PARENT: [
                CallbackQueryHandler(
                    step_parent,
                    pattern=r"^pnlsave:(p:(none|\d+)|keep)$",
                ),
            ],
            GROUPS: [
                CallbackQueryHandler(
                    step_groups,
                    pattern=r"^pnlsave:(g:\d+|gdone)$",
                ),
            ],
            ADVANCED: [
                CallbackQueryHandler(
                    step_advanced_skip, pattern=r"^pnlsave:advskip$"
                ),
                MessageHandler(
                    NON_MENU_TEXT_FILTER, step_advanced_text
                ),
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    step_confirm, pattern=r"^pnlsave:(ok|cancel)$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="pnlsave",
        persistent=False,
    )
    application.add_handler(conv)
