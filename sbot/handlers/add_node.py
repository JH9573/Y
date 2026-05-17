"""添加节点对话流程。

入口为节点管理菜单中的 [➕ 添加节点] 按钮。
ApiHost → NodeID → ApiKey → 确认 → 远程写配置 + 重启 + 校验。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node_config import (
    NodeEntry,
    V2NodeConfigError,
    add_node_to_config,
    validate_api_host,
    validate_api_key,
    validate_node_id,
)
from .common import CB_NODE_ADD, CB_NODE_MENU, get_ctx


log = logging.getLogger(__name__)


API_HOST, NODE_ID, API_KEY, CONFIRM = range(4)

KEY = "addnode"


async def cb_addnode_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return ConversationHandler.END

    context.user_data[KEY] = {"server_id": server_id}
    await query.edit_message_text(
        f"在 {server.name} 上添加节点。任意时刻可发送 /cancel 中止。\n\n"
        "请输入面板 API 地址(ApiHost,以 http:// 或 https:// 开头):"
    )
    return API_HOST


async def step_api_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        host = validate_api_host(update.message.text or "")
    except V2NodeConfigError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return API_HOST
    context.user_data[KEY]["api_host"] = host
    await update.message.reply_text("请输入节点 ID(NodeID,正整数):")
    return NODE_ID


async def step_node_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        node_id = validate_node_id(update.message.text or "")
    except V2NodeConfigError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return NODE_ID
    context.user_data[KEY]["node_id"] = node_id
    await update.message.reply_text("请输入节点通讯密钥(ApiKey):")
    return API_KEY


async def step_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        api_key = validate_api_key(update.message.text or "")
    except V2NodeConfigError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return API_KEY
    context.user_data[KEY]["api_key"] = api_key

    data = context.user_data[KEY]
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("确认添加", callback_data="addnode:ok"),
                InlineKeyboardButton("取消", callback_data="addnode:cancel"),
            ]
        ]
    )
    masked = api_key[:4] + "***" + api_key[-4:] if len(api_key) > 8 else "***"
    await update.message.reply_text(
        f"请确认新增节点信息:\n"
        f"ApiHost: {data['api_host']}\n"
        f"NodeID: {data['node_id']}\n"
        f"ApiKey: {masked}\n"
        f"Timeout: 15(默认)",
        reply_markup=kb,
    )
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "addnode:cancel":
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消添加节点。")
        return ConversationHandler.END

    data = context.user_data[KEY]
    server_id = data["server_id"]
    ctx = get_ctx(context)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        if server is None:
            await query.edit_message_text("服务器不存在。")
            context.user_data.pop(KEY, None)
            return ConversationHandler.END
        # 库里如果已存在同 (server, api_host, node_id) 也拒绝
        dup = await crud.find_node(s, server_id, data["api_host"], data["node_id"])
        if dup is not None:
            await query.edit_message_text(
                "bot 数据库中已存在同 ApiHost / NodeID 的节点。"
                "若实际不一致,请先点击「同步」校正后重试。"
            )
            context.user_data.pop(KEY, None)
            return ConversationHandler.END

    await query.edit_message_text(f"正在写入 {server.name} 的配置并重启 v2node…")

    entry = NodeEntry(
        api_host=data["api_host"],
        node_id=data["node_id"],
        api_key=data["api_key"],
        timeout=15,
    )
    try:
        ok, msg = await add_node_to_config(ctx.ssh, server, entry)
    except (V2NodeConfigError, SSHError) as exc:
        ok, msg = False, str(exc)

    async with crud.session() as s:
        if ok:
            await crud.add_node(
                s,
                server_id=server_id,
                api_host=entry.api_host,
                node_id=entry.node_id,
                api_key=ctx.crypto.encrypt(entry.api_key),
                timeout=entry.timeout,
            )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="node.add",
            result="success" if ok else "failed",
            detail=f"{entry.api_host}/NodeID={entry.node_id}: {msg}",
        )
        await s.commit()

    prefix = "✅" if ok else "❌"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("查看节点", callback_data=f"{CB_NODE_MENU}{server_id}")]]
    )
    await query.edit_message_text(f"{prefix} {msg}", reply_markup=kb)
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消添加节点。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_addnode_start, pattern=f"^{CB_NODE_ADD}\\d+$"),
        ],
        states={
            API_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_api_host)],
            NODE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_node_id)],
            API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_api_key)],
            CONFIRM: [CallbackQueryHandler(step_confirm, pattern=r"^addnode:(ok|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="addnode",
        persistent=False,
    )
    application.add_handler(conv)
