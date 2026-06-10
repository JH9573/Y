"""添加跳板机对话流程。

跳板机管理菜单「➕ 添加跳板机」或 /addjump 进入:
  → 别名 → host → port(可跳过)→ username → 认证方式 → 凭据
  → SSH 连通性测试 → 写库
"""
from __future__ import annotations

import logging
from contextlib import suppress

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from ..db import crud
from ..db.models import JumpHost
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_JUMP_ADD,
    NON_MENU_TEXT_FILTER,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
)


log = logging.getLogger(__name__)


# 对话状态
NAME, HOST, PORT, USERNAME, AUTH_TYPE, CREDENTIAL = range(6)

KEY = "addjump"


async def cmd_addjump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加跳板机。任意时候可点「❌ 取消」或发送 /cancel 中止。\n\n"
        "请输入跳板机别名(例如 香港中转):",
        reply_markup=cancel_only_kb(),
    )
    return NAME


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("别名不能为空,请重新输入:")
        return NAME
    if len(name) > 64:
        await update.message.reply_text("别名过长(最多 64 字符),请重新输入:")
        return NAME
    async with crud.session() as s:
        existing = await crud.get_jump_host_by_name(s, name)
    if existing is not None:
        await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
        return NAME
    context.user_data[KEY]["name"] = name
    await update.message.reply_text("请输入跳板机地址(IP 或域名):")
    return HOST


async def step_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    host = (update.message.text or "").strip()
    if not host:
        await update.message.reply_text("地址不能为空,请重新输入:")
        return HOST
    context.user_data[KEY]["host"] = host
    await update.message.reply_text("请输入 SSH 端口(直接回车或发送 / 使用默认 22):")
    return PORT


async def step_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if raw in ("", "/"):
        port = 22
    else:
        try:
            port = int(raw)
        except ValueError:
            await update.message.reply_text("端口必须是整数,请重新输入:")
            return PORT
        if not (1 <= port <= 65535):
            await update.message.reply_text("端口范围 1-65535,请重新输入:")
            return PORT
    context.user_data[KEY]["port"] = port
    await update.message.reply_text("请输入 SSH 登录用户名:")
    return USERNAME


async def step_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("用户名不能为空,请重新输入:")
        return USERNAME
    context.user_data[KEY]["username"] = username
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("密钥", callback_data="jhauth:key"),
                InlineKeyboardButton("密码", callback_data="jhauth:password"),
            ]
        ]
    )
    await update.message.reply_text("选择认证方式:", reply_markup=kb)
    return AUTH_TYPE


async def step_auth_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    context.user_data[KEY]["auth_type"] = choice
    if choice == "key":
        await query.edit_message_text(
            "请输入私钥文件在 **bot 服务器上**的绝对路径(密钥内容不入库)。"
        )
    else:
        await query.edit_message_text(
            "请输入 SSH 登录密码。\n"
            "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
        )
    return CREDENTIAL


async def step_credential(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    raw = (update.message.text or "").strip()

    if data["auth_type"] == "password":
        with suppress(BadRequest):
            await update.message.delete()
        if not raw:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="密码不能为空,请重新输入:",
            )
            return CREDENTIAL
        credential = get_ctx(context).crypto.encrypt(raw)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="已收到密码,聊天记录中的明文消息已删除。开始测试连通性…",
        )
    else:
        if not raw:
            await update.message.reply_text("路径不能为空,请重新输入:")
            return CREDENTIAL
        credential = raw
        await update.message.reply_text("已记录密钥路径,开始测试连通性…")

    data["credential"] = credential
    return await _finalize(update, context)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    chat_id = update.effective_chat.id

    trial = JumpHost(
        name=data["name"],
        host=data["host"],
        port=data["port"],
        username=data["username"],
        auth_type=data["auth_type"],
        credential=data["credential"],
    )
    ok = await ctx.ssh.check_jump_connectivity(trial)
    if not ok:
        await context.bot.send_message(
            chat_id=chat_id,
            text="SSH 连通性测试失败,跳板机未登记。请检查地址、端口、用户名、凭据后重试。",
            reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    async with crud.session() as s:
        jump = await crud.create_jump_host(
            s,
            name=data["name"],
            host=data["host"],
            port=data["port"],
            username=data["username"],
            auth_type=data["auth_type"],
            credential=data["credential"],
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="jump.add",
            result="success",
            detail=f"jump={jump.name} ({jump.username}@{jump.host}:{jump.port})",
        )
        await s.commit()
        jump_name = jump.name

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ 跳板机「{jump_name}」已登记。\n"
            "现在可以在添加服务器或修改服务器信息时选用它。"
        ),
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消添加跳板机。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("addjump", cmd_addjump),
            CallbackQueryHandler(cmd_addjump, pattern=f"^{CB_JUMP_ADD}$"),
        ],
        states={
            NAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_name)],
            HOST: [MessageHandler(NON_MENU_TEXT_FILTER, step_host)],
            PORT: [MessageHandler(NON_MENU_TEXT_FILTER, step_port)],
            USERNAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_username)],
            AUTH_TYPE: [
                CallbackQueryHandler(step_auth_type, pattern=r"^jhauth:(key|password)$")
            ],
            CREDENTIAL: [MessageHandler(NON_MENU_TEXT_FILTER, step_credential)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="addjump",
        persistent=False,
    )
    application.add_handler(conv)
