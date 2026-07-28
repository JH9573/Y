"""各协议共用的步骤:协议、名称、地址、端口、传输、倍率、父节点、权限组、高级 JSON。"""
from __future__ import annotations

import json

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ...db import crud
from ...services.v2board_api import V2BoardAPIError
from ..common import get_ctx, pager_row, paginate
from .options import (
    ADVANCED, GROUPS, HOST, HOST_MANUAL_CB, HOST_PICK_CB, KEEP_CB, KEY,
    NAME, NETWORK, NETWORK_OPTIONS_FULL, NETWORK_OPTIONS_SS, NET_SETTINGS,
    NS_CLEAR_CB, PARENT, PARENT_PAGE_CB, PORT, PROTOCOL, PROTOCOL_OPTIONS,
    PROTOCOL_PICK_CB, PROTOCOL_VALUES, RATE, SAVE_FIELDS, SERVER_PORT,
)
from .state import _advance, _is_edit, _keep_kb, _reply, prompt_for

# ---------- step: PROTOCOL ----------

@prompt_for("protocol")
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


@prompt_for("name")
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


@prompt_for("host")
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


@prompt_for("port")
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


@prompt_for("server_port")
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
# ---------- step: NETWORK ----------

@prompt_for("network")
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


@prompt_for("net_settings")
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
# ---------- step: RATE ----------

@prompt_for("rate")
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

@prompt_for("parent")
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

    view = paginate(candidates, data.get("parent_page"))
    data["parent_page"] = view.number

    rows: list[list[InlineKeyboardButton]] = []
    for n in view.items:
        show = "✅" if n.show else "❌"
        label = f"{show} #{n.node_id} {n.name}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"pnlsave:p:{n.node_id}"
        )])

    pager = pager_row(PARENT_PAGE_CB, view)
    if pager:
        rows.append(pager)

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
    elif view.multi:
        lines.append(view.label)

    await _reply(
        update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )
    return PARENT


async def step_parent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]

    if query.data.startswith(PARENT_PAGE_CB):
        # 翻页不算选定,记下页码后原地重画
        data["parent_page"] = int(query.data.rsplit(":", 1)[1])
        return await _prompt_parent(update, context)

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

@prompt_for("groups")
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

@prompt_for("advanced")
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
