"""v2node 安装对话流程。

从已登记但未安装 v2node 的服务器菜单进入。收集首个节点信息后,
分步执行安装并实时反馈进度。
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
    validate_api_host,
    validate_api_key,
    validate_node_id,
)
from ..services.v2node_install import InstallError, InstallParams, install_v2node
from .common import CB_INSTALL_START, CB_SERVER_PREFIX, get_ctx


log = logging.getLogger(__name__)


API_HOST, NODE_ID, API_KEY, CONFIRM = range(4)

KEY = "install"


async def cb_install_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        f"将在 {server.name} 上安装 v2node。\n"
        f"bot 会按步骤检测依赖 → 下载二进制 → 注册 systemd 服务 → 启动。\n\n"
        f"首先需要配置一个节点。请输入面板 API 地址(ApiHost):"
    )
    return API_HOST


async def step_api_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        host = validate_api_host(update.message.text or "")
    except V2NodeConfigError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return API_HOST
    context.user_data[KEY]["api_host"] = host
    await update.message.reply_text("请输入节点 ID(NodeID):")
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
    masked = api_key[:4] + "***" + api_key[-4:] if len(api_key) > 8 else "***"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("开始安装", callback_data="install:ok"),
                InlineKeyboardButton("取消", callback_data="install:cancel"),
            ]
        ]
    )
    await update.message.reply_text(
        f"请确认安装配置:\n"
        f"ApiHost: {data['api_host']}\n"
        f"NodeID: {data['node_id']}\n"
        f"ApiKey: {masked}\n\n"
        f"点击「开始安装」后将连接服务器并按步骤执行。",
        reply_markup=kb,
    )
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "install:cancel":
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消安装。")
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

    first_node = NodeEntry(
        api_host=data["api_host"],
        node_id=data["node_id"],
        api_key=data["api_key"],
        timeout=15,
    )
    params = InstallParams(first_node=first_node)

    progress_lines = [f"在 {server.name} 上安装 v2node…"]
    await query.edit_message_text("\n".join(progress_lines))

    failure_detail: str | None = None
    try:
        async for step in install_v2node(ctx.ssh, server, params):
            progress_lines.append(f"• {step.step}: {step.detail}")
            try:
                await query.edit_message_text("\n".join(progress_lines))
            except Exception:  # noqa: BLE001
                # Telegram 偶尔会拒绝相同内容的编辑,忽略
                pass
        success = True
        result_text = "\n".join(progress_lines) + "\n\n✅ 安装完成,v2node 已启动。"
    except InstallError as exc:
        success = False
        failure_detail = str(exc)
        progress_lines.append(f"❌ 失败:{exc}")
        result_text = "\n".join(progress_lines)
    except SSHError as exc:
        success = False
        failure_detail = str(exc)
        progress_lines.append(f"❌ SSH 错误:{exc}")
        result_text = "\n".join(progress_lines)

    async with crud.session() as s:
        if success:
            await crud.set_v2node_installed(s, server_id, True)
            await crud.add_node(
                s,
                server_id=server_id,
                api_host=first_node.api_host,
                node_id=first_node.node_id,
                api_key=ctx.crypto.encrypt(first_node.api_key),
                timeout=first_node.timeout,
            )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="v2node.install",
            result="success" if success else "failed",
            detail=failure_detail or f"node={first_node.api_host}/NodeID={first_node.node_id}",
        )
        await s.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ 返回菜单", callback_data=f"{CB_SERVER_PREFIX}{server_id}")]]
    )
    await query.edit_message_text(result_text, reply_markup=kb)
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消安装。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_install_start, pattern=f"^{CB_INSTALL_START}\\d+$"),
        ],
        states={
            API_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_api_host)],
            NODE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_node_id)],
            API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_api_key)],
            CONFIRM: [CallbackQueryHandler(step_confirm, pattern=r"^install:(ok|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="install",
        persistent=False,
    )
    application.add_handler(conv)
