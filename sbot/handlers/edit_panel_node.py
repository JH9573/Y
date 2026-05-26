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
    PROTOCOL,
    NAME,
    HOST,
    PORT,
    SERVER_PORT,
    CIPHER,
    FLOW,
    TLS,
    # tls_settings 引导式子步骤
    TS_SERVER_NAME,
    TS_CERT_MODE,
    TS_CERT_FILE,
    TS_KEY_FILE,
    TS_PROVIDER,
    TS_DNS_ENV,
    TS_DEST,
    TS_PRIVATE_KEY,
    TS_SHORT_ID,
    TS_XVER,
    NETWORK,
    NET_SETTINGS,
    UP_MBPS,
    DOWN_MBPS,
    RATE,
    PARENT,
    GROUPS,
    ADVANCED,
    CONFIRM,
) = range(27)

# 父节点选择按钮一屏最多展示这么多;v2board 一般够用
PARENT_LIST_LIMIT = 30

KEY = "pnlsave"

# v2board V2nodeController::save 接受的协议
PROTOCOL_OPTIONS: list[tuple[str, str]] = [
    ("shadowsocks", "Shadowsocks"),
    ("vless", "VLESS"),
    ("anytls", "AnyTLS"),
    ("hysteria2", "Hysteria2"),
]
PROTOCOL_VALUES = {v for v, _ in PROTOCOL_OPTIONS}

CIPHER_OPTIONS = [
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-256-gcm",
    "chacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
]
# v2board 校验:tls ∈ {0, 1, 2}。1 = TLS, 2 = REALITY (vless 常用)
TLS_OPTIONS: list[tuple[int, str]] = [(0, "关闭"), (1, "TLS"), (2, "REALITY")]
# v2board 校验:network ∈ {tcp, ws, grpc, http, httpupgrade, xhttp}
NETWORK_OPTIONS_SS: list[tuple[str, str]] = [("tcp", "tcp"), ("http", "http伪装")]
NETWORK_OPTIONS_FULL: list[tuple[str, str]] = [
    ("tcp", "tcp"),
    ("ws", "ws"),
    ("grpc", "grpc"),
    ("http", "http"),
    ("httpupgrade", "httpupgrade"),
    ("xhttp", "xhttp"),
]
FLOW_OPTIONS: list[tuple[str, str]] = [
    ("", "(无)"),
    ("xtls-rprx-vision", "xtls-rprx-vision"),
]

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
NS_CLEAR_CB = "pnlsave:nsclear"
PROTOCOL_PICK_CB = "pnlsave:proto:"  # pnlsave:proto:<value>
FLOW_PICK_CB = "pnlsave:flow:"  # pnlsave:flow:none | pnlsave:flow:vision
UP_MBPS_SKIP_CB = "pnlsave:upskip"
DOWN_MBPS_SKIP_CB = "pnlsave:dnskip"
# tls_settings 引导
TS_SKIP_CB = "pnlsave:tssk"  # 各文本子字段通用的「跳过」
CERT_MODE_CB = "pnlsave:cm:"  # pnlsave:cm:<mode>
XVER_CB = "pnlsave:xver:"  # pnlsave:xver:<0|1|2>

# tls=1 时证书获取方式
CERT_MODE_OPTIONS: list[tuple[str, str]] = [
    ("http", "http"),
    ("dns", "dns"),
    ("self", "self"),
    ("file", "file"),
    ("none", "none"),
]
CERT_MODE_VALUES = {v for v, _ in CERT_MODE_OPTIONS}


# ---------- 流程编排 ----------

# 每种协议的字段顺序。`_advance(after, ...)` 据此决定下一步进哪个 _prompt_*。
_FLOW_BY_PROTOCOL: dict[str, list[str]] = {
    "shadowsocks": [
        "protocol", "name", "host", "port", "server_port",
        "cipher", "tls", "tls_settings", "network", "net_settings",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
    "vless": [
        "protocol", "name", "host", "port", "server_port",
        "flow", "tls", "tls_settings", "network", "net_settings",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
    # anytls / hysteria2 服务端会强制 tls=1,bot 不再让用户选,
    # 但 tls_settings 仍然要给用户机会填(证书 / SNI / REALITY 等)。
    "anytls": [
        "protocol", "name", "host", "port", "server_port",
        "tls_settings", "network", "net_settings",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
    "hysteria2": [
        "protocol", "name", "host", "port", "server_port",
        "tls_settings", "up_mbps", "down_mbps",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
}


def _flow_for(data: dict) -> list[str]:
    protocol = data["values"].get("protocol") or data["initial"].get("protocol") or "shadowsocks"
    return _FLOW_BY_PROTOCOL.get(protocol, _FLOW_BY_PROTOCOL["shadowsocks"])


async def _advance(
    after_field: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """跳到该协议下 after_field 的下一步。"""
    data = context.user_data[KEY]
    flow = _flow_for(data)
    try:
        idx = flow.index(after_field)
    except ValueError:
        idx = -1
    next_field = flow[idx + 1] if idx + 1 < len(flow) else "confirm"
    return await _PROMPT_BY_FIELD[next_field](update, context)


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


# ---------- step: PROTOCOL ----------

async def _prompt_protocol(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """新增模式才走;编辑模式从 cb_edit_entry 已经把协议预置进 values。"""
    data = context.user_data[KEY]
    panel_name = data.get("panel_name", "")
    rows = [
        [InlineKeyboardButton(
            label, callback_data=f"{PROTOCOL_PICK_CB}{value}"
        )]
        for value, label in PROTOCOL_OPTIONS
    ]
    text = (
        f"在面板「{panel_name}」上添加 v2node 节点。\n"
        "任意时刻可发送 /cancel 中止。\n\n"
        "请选择节点协议:"
    )
    await _reply(update, text, reply_markup=InlineKeyboardMarkup(rows))
    return PROTOCOL


async def step_protocol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    proto = query.data.split(":", 2)[2]
    if proto not in PROTOCOL_VALUES:
        await query.message.reply_text("无效的协议,请重新选择:")
        return PROTOCOL
    context.user_data[KEY]["values"]["protocol"] = proto
    return await _advance("protocol", update, context)


async def _prompt_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """add 模式协议选完后才用得到;edit 模式从 cb_edit_entry 已直接提示了名称。"""
    data = context.user_data[KEY]
    if _is_edit(context):
        val = data["initial"].get("name", "")
        await _reply(
            update,
            f"请输入节点名称(当前: {val}):",
            reply_markup=_keep_kb(val),
        )
    else:
        await _reply(update, "请输入节点名称:")
    return NAME


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
        "panel_name": panel.name,
    }
    return await _prompt_protocol(update, context)


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
    if node.protocol not in PROTOCOL_VALUES:
        await query.edit_message_text(
            f"当前不支持编辑协议 {node.protocol}。"
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
    initial.setdefault("protocol", node.protocol)
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
        # 编辑模式协议固定,直接预置到 values 里,跳过 PROTOCOL 选择步骤
        "values": {"protocol": node.protocol},
        "panel_name": panel.name,
    }
    await query.edit_message_text(
        f"编辑面板「{panel.name}」的 v2node #{node_id} ({node.protocol})。\n"
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
    return await _advance("name", update, context)


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
    return await _advance("host", update, context)


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
    return await _advance("host", update, context)


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
    return await _advance("port", update, context)


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
    return await _advance("server_port", update, context)


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
    return await _advance("cipher", update, context)


# ---------- step: TLS ----------

def _tls_options_for(data: dict) -> list[tuple[int, str]]:
    """vless 必须在 TLS / REALITY 间二选一,不给「关闭」;其余给 关闭 / TLS。"""
    protocol = data["values"].get("protocol", "shadowsocks")
    if protocol == "vless":
        return [(1, "TLS"), (2, "REALITY")]
    return [(0, "关闭"), (1, "TLS")]


async def _prompt_tls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    options = _tls_options_for(data)
    rows = [[
        InlineKeyboardButton(f"{label} ({v})", callback_data=f"pnlsave:t:{v}")
        for v, label in options
    ]]
    if _is_edit(context):
        current = data["initial"].get("tls", 0)
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update, "请选择安全性:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return TLS


async def step_tls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    allowed = {v for v, _ in _tls_options_for(data)}
    if query.data == KEEP_CB:
        tls = int(data["initial"].get("tls", 0))
    else:
        tls = int(query.data.split(":", 2)[2])
    if tls not in allowed:
        await query.message.reply_text("无效的安全性选择,请重选:")
        return TLS
    data["values"]["tls"] = tls
    return await _advance("tls", update, context)


# ---------- step: NETWORK ----------

async def _prompt_network(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    options = _network_options_for(data)
    rows = [[
        InlineKeyboardButton(label, callback_data=f"pnlsave:n:{value}")
        for value, label in options
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


def _network_options_for(data: dict) -> list[tuple[str, str]]:
    protocol = data["values"].get("protocol", "shadowsocks")
    return NETWORK_OPTIONS_SS if protocol == "shadowsocks" else NETWORK_OPTIONS_FULL


async def step_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    allowed = {v for v, _ in _network_options_for(data)}
    if query.data == KEEP_CB:
        # 兼容旧数据,保留路径直接信任 initial 值
        net = str(data["initial"].get("network", "tcp"))
    else:
        net = query.data.split(":", 2)[2]
        if net not in allowed:
            await query.message.reply_text("无效的 network。")
            return NETWORK
    data["values"]["network"] = net
    return await _advance("network", update, context)


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
        buttons.append([InlineKeyboardButton(
            "🗑 清空 network_settings", callback_data=NS_CLEAR_CB
        )])

    text = (
        f"可选:贴入 network_settings JSON(network = {net})。\n"
        f"示例:\n\n{sample}\n\n"
        "点「跳过」表示不带该字段;"
        + ("「🗑 清空」会把面板上该字段显式置 null。" if _is_edit(context) else "")
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
    return await _advance("net_settings", update, context)


async def step_net_settings_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """编辑模式:显式把 network_settings 置 null,让面板擦掉这个字段。"""
    query = update.callback_query
    await query.answer()
    context.user_data[KEY]["values"]["network_settings"] = None
    return await _advance("net_settings", update, context)


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
    return await _advance("net_settings", update, context)


# ---------- step: TLS_SETTINGS(引导式) ----------

def _resolve_tls(data: dict) -> int:
    """取当前生效的 tls 值。vless/ss 走过选项以 values 为准;
    anytls/hysteria2 服务端强制 1。"""
    tls = data["values"].get("tls")
    if tls is not None:
        return int(tls)
    protocol = data["values"].get("protocol", "shadowsocks")
    if protocol in ("anytls", "hysteria2"):
        return 1
    return int(data["initial"].get("tls", 0) or 0)


def _ts_sequence(data: dict) -> list[str]:
    """根据 tls 值(及已选的 cert_mode)算出 tls_settings 子字段顺序。"""
    tls = _resolve_tls(data)
    if tls == 2:  # REALITY
        return ["dest", "server_name", "private_key", "short_id", "xver"]
    if tls == 1:  # TLS
        seq = ["server_name", "cert_mode"]
        cert_mode = data.get("ts", {}).get("cert_mode")
        if cert_mode in ("file", "self"):
            seq += ["cert_file", "key_file"]
        elif cert_mode == "dns":
            seq += ["provider", "dns_env"]
        return seq
    return []  # tls=0:不配 tls_settings


def _ts_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("跳过", callback_data=TS_SKIP_CB)]]
    )


async def _prompt_tls_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """tls_settings 引导入口:按 tls 值进入对应子流程。"""
    data = context.user_data[KEY]
    tls = _resolve_tls(data)
    initial_tls = int(data["initial"].get("tls", 0) or 0)
    initial_ts = data["initial"].get("tls_settings")
    # 编辑且 tls 没变,用现有值打底,逐项可改;否则从空开始
    if _is_edit(context) and tls == initial_tls and isinstance(initial_ts, dict):
        data["ts"] = dict(initial_ts)
    else:
        data["ts"] = {}

    seq = _ts_sequence(data)
    if not seq:  # tls=0
        return await _advance("tls_settings", update, context)
    return await _TS_PROMPT_BY_FIELD[seq[0]](update, context)


async def _ts_advance(
    after_subfield: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    seq = _ts_sequence(data)
    try:
        idx = seq.index(after_subfield)
    except ValueError:
        idx = len(seq)
    if idx + 1 < len(seq):
        return await _TS_PROMPT_BY_FIELD[seq[idx + 1]](update, context)
    return await _ts_finish(update, context)


async def _ts_finish(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    ts = {k: v for k, v in data.get("ts", {}).items() if v not in (None, "")}
    if ts:
        data["values"]["tls_settings"] = ts
    data.pop("ts", None)
    return await _advance("tls_settings", update, context)


def _ts_cur(data: dict, key: str) -> str:
    val = data.get("ts", {}).get(key)
    return str(val) if val not in (None, "") else ""


async def _ts_text_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    *, prompt: str, key: str, state: int,
) -> int:
    cur = _ts_cur(context.user_data[KEY], key)
    suffix = f"(当前: {cur})" if cur else ""
    await _reply(update, f"{prompt}{suffix},或点「跳过」:", reply_markup=_ts_skip_kb())
    return state


async def _ts_text_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    *, key: str, after: str,
) -> int:
    """文本子字段通用处理:有输入则写入 ts[key],「跳过」按钮保持原值。"""
    data = context.user_data[KEY]
    if update.callback_query:  # 跳过
        await update.callback_query.answer()
    else:
        val = (update.message.text or "").strip()
        if val:
            data["ts"][key] = val
    return await _ts_advance(after, update, context)


# server_name(TLS 与 REALITY 共用)

async def _ts_prompt_server_name(update, context):
    return await _ts_text_prompt(
        update, context,
        prompt="请输入 server_name / SNI 域名",
        key="server_name", state=TS_SERVER_NAME,
    )


async def step_ts_server_name(update, context):
    return await _ts_text_step(
        update, context, key="server_name", after="server_name"
    )


# cert_mode(TLS)

async def _ts_prompt_cert_mode(update, context):
    rows = [[
        InlineKeyboardButton(label, callback_data=f"{CERT_MODE_CB}{val}")
        for val, label in CERT_MODE_OPTIONS
    ]]
    await _reply(
        update,
        "请选择证书获取方式 cert_mode:\n"
        "http=ACME http 验证 / dns=ACME dns 验证 / self=自签 / "
        "file=手动指定证书路径 / none=不自动签发",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TS_CERT_MODE


async def step_ts_cert_mode(update, context):
    query = update.callback_query
    await query.answer()
    cm = query.data.split(":", 2)[2]
    if cm not in CERT_MODE_VALUES:
        await query.message.reply_text("无效的 cert_mode,请重选:")
        return TS_CERT_MODE
    context.user_data[KEY]["ts"]["cert_mode"] = cm
    return await _ts_advance("cert_mode", update, context)


# cert_file / key_file(cert_mode=file/self)

async def _ts_prompt_cert_file(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入证书文件路径 cert_file",
        key="cert_file", state=TS_CERT_FILE,
    )


async def step_ts_cert_file(update, context):
    return await _ts_text_step(
        update, context, key="cert_file", after="cert_file"
    )


async def _ts_prompt_key_file(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入私钥文件路径 key_file",
        key="key_file", state=TS_KEY_FILE,
    )


async def step_ts_key_file(update, context):
    return await _ts_text_step(
        update, context, key="key_file", after="key_file"
    )


# provider / dns_env(cert_mode=dns)

async def _ts_prompt_provider(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入 DNS provider(如 cloudflare / aliyun)",
        key="provider", state=TS_PROVIDER,
    )


async def step_ts_provider(update, context):
    return await _ts_text_step(
        update, context, key="provider", after="provider"
    )


async def _ts_prompt_dns_env(update, context):
    return await _ts_text_prompt(
        update, context,
        prompt="请输入 dns_env(逗号分隔 KEY=VALUE,如 CF_Token=xxx,CF_Account_ID=yyy)",
        key="dns_env", state=TS_DNS_ENV,
    )


async def step_ts_dns_env(update, context):
    return await _ts_text_step(
        update, context, key="dns_env", after="dns_env"
    )


# REALITY 专属:dest / private_key / short_id / xver

async def _ts_prompt_dest(update, context):
    return await _ts_text_prompt(
        update, context,
        prompt="请输入 dest 偷取握手的目标(如 www.cloudflare.com:443)",
        key="dest", state=TS_DEST,
    )


async def step_ts_dest(update, context):
    return await _ts_text_step(update, context, key="dest", after="dest")


async def _ts_prompt_private_key(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入 REALITY private_key",
        key="private_key", state=TS_PRIVATE_KEY,
    )


async def step_ts_private_key(update, context):
    return await _ts_text_step(
        update, context, key="private_key", after="private_key"
    )


async def _ts_prompt_short_id(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入 short_id",
        key="short_id", state=TS_SHORT_ID,
    )


async def step_ts_short_id(update, context):
    return await _ts_text_step(
        update, context, key="short_id", after="short_id"
    )


async def _ts_prompt_xver(update, context):
    cur = _ts_cur(context.user_data[KEY], "xver") or "0"
    rows = [[
        InlineKeyboardButton(str(i), callback_data=f"{XVER_CB}{i}")
        for i in (0, 1, 2)
    ]]
    await _reply(
        update,
        f"请选择 xver(proxy protocol 版本,默认 0;当前: {cur}):",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TS_XVER


async def step_ts_xver(update, context):
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 2)[2]
    # node.go 里 xver 是 json:"xver,string",存成字符串
    context.user_data[KEY]["ts"]["xver"] = token
    return await _ts_advance("xver", update, context)


# 子字段 -> prompt 映射(供 _ts_advance 用)
_TS_PROMPT_BY_FIELD: dict[str, Any] = {
    "server_name": _ts_prompt_server_name,
    "cert_mode": _ts_prompt_cert_mode,
    "cert_file": _ts_prompt_cert_file,
    "key_file": _ts_prompt_key_file,
    "provider": _ts_prompt_provider,
    "dns_env": _ts_prompt_dns_env,
    "dest": _ts_prompt_dest,
    "private_key": _ts_prompt_private_key,
    "short_id": _ts_prompt_short_id,
    "xver": _ts_prompt_xver,
}


# ---------- step: FLOW (vless) ----------

async def _prompt_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    rows = [[
        InlineKeyboardButton(
            label, callback_data=f"{FLOW_PICK_CB}{'vision' if value else 'none'}"
        )
        for value, label in FLOW_OPTIONS
    ]]
    if _is_edit(context):
        current = data["initial"].get("flow") or "(无)"
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update, "请选择 VLESS flow:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return FLOW


async def step_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == KEEP_CB:
        flow = str(data["initial"].get("flow") or "")
    else:
        token = query.data.split(":", 2)[2]
        flow = "xtls-rprx-vision" if token == "vision" else ""
    data["values"]["flow"] = flow
    return await _advance("flow", update, context)


# ---------- step: UP_MBPS / DOWN_MBPS (hysteria2) ----------

def _parse_mbps(text: str) -> float | None:
    try:
        v = float(text)
    except ValueError:
        return None
    if v < 0:
        return None
    return v


async def _prompt_up_mbps(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rows = [[InlineKeyboardButton("跳过", callback_data=UP_MBPS_SKIP_CB)]]
    if _is_edit(context):
        val = data["initial"].get("up_mbps")
        label = f"保留 ({val})" if val not in (None, "") else "保留 (空)"
        rows.append([InlineKeyboardButton(label, callback_data=KEEP_CB)])
    await _reply(
        update,
        "请输入服务端上行带宽 up_mbps(数字,单位 Mbps),或点「跳过」不带:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return UP_MBPS


async def step_up_mbps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == KEEP_CB:
            val = data["initial"].get("up_mbps")
            if val not in (None, ""):
                data["values"]["up_mbps"] = val
        # 跳过:不写 values,payload 不带该字段
        return await _advance("up_mbps", update, context)
    mbps = _parse_mbps((update.message.text or "").strip())
    if mbps is None:
        await update.message.reply_text("up_mbps 必须是 ≥0 的数字,请重新输入:")
        return UP_MBPS
    data["values"]["up_mbps"] = mbps
    return await _advance("up_mbps", update, context)


async def _prompt_down_mbps(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rows = [[InlineKeyboardButton("跳过", callback_data=DOWN_MBPS_SKIP_CB)]]
    if _is_edit(context):
        val = data["initial"].get("down_mbps")
        label = f"保留 ({val})" if val not in (None, "") else "保留 (空)"
        rows.append([InlineKeyboardButton(label, callback_data=KEEP_CB)])
    await _reply(
        update,
        "请输入服务端下行带宽 down_mbps(数字,单位 Mbps),或点「跳过」不带:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return DOWN_MBPS


async def step_down_mbps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == KEEP_CB:
            val = data["initial"].get("down_mbps")
            if val not in (None, ""):
                data["values"]["down_mbps"] = val
        return await _advance("down_mbps", update, context)
    mbps = _parse_mbps((update.message.text or "").strip())
    if mbps is None:
        await update.message.reply_text("down_mbps 必须是 ≥0 的数字,请重新输入:")
        return DOWN_MBPS
    data["values"]["down_mbps"] = mbps
    return await _advance("down_mbps", update, context)


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
    return await _advance("rate", update, context)


# ---------- step: PARENT ----------

async def _prompt_parent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    panel_id = data["panel_id"]
    self_node_id = data.get("node_id")

    async with crud.session() as s:
        nodes = await crud.list_panel_nodes(s, panel_id)

    # 编辑时排除自己,避免自指;同时只保留本身没有父节点的,
    # 不让中转链再往下套一层(已是子节点的不作为候选父节点)
    candidates = [
        n for n in nodes
        if n.node_id != self_node_id and not n.parent_id
    ]

    rows: list[list[InlineKeyboardButton]] = []
    for n in candidates[:PARENT_LIST_LIMIT]:
        show = "✅" if n.show else "❌"
        label = f"{show} #{n.node_id} {n.name}"
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

    lines = ["请选择父节点(用于中转节点,仅列出自身无父节点的),也可点「🚫 不选父节点」跳过:"]
    if not candidates:
        lines.append("")
        lines.append("(暂无可作父节点的节点,可直接「不选父节点」)")
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
    return await _advance("parent", update, context)


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
        return await _advance("groups", update, context)

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
    return await _advance("advanced", update, context)


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
    return await _advance("advanced", update, context)


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


# `_advance` 通过 field 名查这个表跳到下一步。必须等所有 _prompt_* 都定义后再建。
_PROMPT_BY_FIELD: dict[str, Any] = {
    "protocol": _prompt_protocol,
    "name": _prompt_name,
    "host": _prompt_host,
    "port": _prompt_port,
    "server_port": _prompt_server_port,
    "cipher": _prompt_cipher,
    "flow": _prompt_flow,
    "tls": _prompt_tls,
    "tls_settings": _prompt_tls_settings,
    "network": _prompt_network,
    "net_settings": _prompt_net_settings,
    "up_mbps": _prompt_up_mbps,
    "down_mbps": _prompt_down_mbps,
    "rate": _prompt_rate,
    "parent": _prompt_parent,
    "groups": _prompt_groups,
    "advanced": _prompt_advanced,
    "confirm": _prompt_confirm,
}


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
            PROTOCOL: [
                CallbackQueryHandler(
                    step_protocol, pattern=rf"^{PROTOCOL_PICK_CB}\w+$"
                ),
            ],
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
            FLOW: [
                CallbackQueryHandler(
                    step_flow,
                    pattern=rf"^({FLOW_PICK_CB}(none|vision)|{KEEP_CB})$",
                ),
            ],
            TLS: [
                CallbackQueryHandler(
                    step_tls, pattern=r"^pnlsave:(t:[012]|keep)$"
                ),
            ],
            TS_SERVER_NAME: [
                CallbackQueryHandler(
                    step_ts_server_name, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_server_name),
            ],
            TS_CERT_MODE: [
                CallbackQueryHandler(
                    step_ts_cert_mode, pattern=rf"^{CERT_MODE_CB}\w+$"
                ),
            ],
            TS_CERT_FILE: [
                CallbackQueryHandler(
                    step_ts_cert_file, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_cert_file),
            ],
            TS_KEY_FILE: [
                CallbackQueryHandler(
                    step_ts_key_file, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_key_file),
            ],
            TS_PROVIDER: [
                CallbackQueryHandler(
                    step_ts_provider, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_provider),
            ],
            TS_DNS_ENV: [
                CallbackQueryHandler(
                    step_ts_dns_env, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_dns_env),
            ],
            TS_DEST: [
                CallbackQueryHandler(step_ts_dest, pattern=f"^{TS_SKIP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_dest),
            ],
            TS_PRIVATE_KEY: [
                CallbackQueryHandler(
                    step_ts_private_key, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_private_key),
            ],
            TS_SHORT_ID: [
                CallbackQueryHandler(
                    step_ts_short_id, pattern=f"^{TS_SKIP_CB}$"
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ts_short_id),
            ],
            TS_XVER: [
                CallbackQueryHandler(
                    step_ts_xver, pattern=rf"^{XVER_CB}[012]$"
                ),
            ],
            NETWORK: [
                CallbackQueryHandler(
                    step_network,
                    pattern=(
                        r"^pnlsave:(n:(tcp|ws|grpc|http|httpupgrade|xhttp)|keep)$"
                    ),
                ),
            ],
            NET_SETTINGS: [
                CallbackQueryHandler(
                    step_net_settings_skip,
                    pattern=r"^pnlsave:(nsskip|keep)$",
                ),
                CallbackQueryHandler(
                    step_net_settings_clear, pattern=f"^{NS_CLEAR_CB}$"
                ),
                MessageHandler(
                    NON_MENU_TEXT_FILTER, step_net_settings_text
                ),
            ],
            UP_MBPS: [
                CallbackQueryHandler(
                    step_up_mbps,
                    pattern=rf"^({UP_MBPS_SKIP_CB}|{KEEP_CB})$",
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_up_mbps),
            ],
            DOWN_MBPS: [
                CallbackQueryHandler(
                    step_down_mbps,
                    pattern=rf"^({DOWN_MBPS_SKIP_CB}|{KEEP_CB})$",
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_down_mbps),
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
