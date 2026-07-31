"""对话入口与 ConversationHandler 装配。

import 各 steps_* 模块的副作用之一,是让 @prompt_for 把 _prompt_* 登记进
state.PROMPT_BY_FIELD,所以本模块必须在 register 之前完成这些 import。
"""
from __future__ import annotations

import json

from typing import Any

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler,
)

from ...db import crud
from ..common import (
    ANY_MENU_TEXT_FILTER, CB_PANEL_NODE_ADD, CB_PANEL_NODE_EDIT,
    NON_MENU_TEXT_FILTER,
)
from .confirm import cmd_cancel, step_confirm
from .options import (
    ADVANCED, CERT_MODE_CB, CIPHER, CONFIRM, DOWN_MBPS, DOWN_MBPS_SKIP_CB,
    ECH_CB, FLOW, FLOW_PICK_CB, GROUPS, HOST, HOST_MANUAL_CB, HOST_PICK_CB,
    KEEP_CB, KEY, NAME, NETWORK, NET_SETTINGS, NS_CLEAR_CB, PARENT, PORT,
    PROTOCOL, PROTOCOL_PICK_CB, PROTOCOL_VALUES, RATE, SAVE_FIELDS,
    SERVER_PORT, TLS, TS_CERT_FILE, TS_CERT_MODE, TS_DEST, TS_DNS_ENV,
    TS_ECH, TS_KEY_FILE, TS_PRIVATE_KEY, TS_PROVIDER, TS_SERVER_NAME,
    TS_SHORT_ID, TS_SKIP_CB, TS_XVER, UP_MBPS, UP_MBPS_SKIP_CB, XVER_CB,
)
from .state import _keep_kb
from .steps_basic import (
    _prompt_protocol, step_advanced_skip, step_advanced_text, step_groups,
    step_host, step_host_manual, step_host_pick_server, step_name,
    step_net_settings_clear, step_net_settings_skip, step_net_settings_text,
    step_network, step_parent, step_port, step_protocol, step_rate,
    step_server_port,
)
from .steps_proto import step_cipher, step_down_mbps, step_flow, step_up_mbps
from .steps_tls import (
    step_tls, step_ts_cert_file, step_ts_cert_mode, step_ts_dest,
    step_ts_dns_env, step_ts_ech, step_ts_key_file, step_ts_private_key,
    step_ts_provider, step_ts_server_name, step_ts_short_id, step_ts_xver,
)

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
            TS_ECH: [
                CallbackQueryHandler(
                    step_ts_ech, pattern=rf"^{ECH_CB}(off|cloudflare)$"
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
                    pattern=r"^pnlsave:(p:(none|\d+)|pp:\d+|keep)$",
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
