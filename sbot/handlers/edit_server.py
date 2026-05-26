"""修改服务器信息对话流程。

从服务器菜单点「✏️ 修改服务器信息」进入,可逐项修改:
  名称 / 用户名 / 密码 / SSH 端口

改 用户名 / 密码 / 端口 后会立即做一次 SSH 连通性测试,失败仅提示,改动照常保存。
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
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_EDIT_SERVER,
    NON_MENU_TEXT_FILTER,
    get_ctx,
    main_menu_kb,
)
from .server import _render_server_menu


log = logging.getLogger(__name__)

# 对话状态
CHOOSE_FIELD, INPUT_NAME, INPUT_USERNAME, INPUT_PASSWORD, INPUT_PORT = range(5)

KEY = "editserver"

# 字段选择按钮 callback
CB_FIELD = "edsf:"  # edsf:name | edsf:username | edsf:password | edsf:port | edsf:back


def _field_menu_markup(server) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("改名称", callback_data=f"{CB_FIELD}name"),
            InlineKeyboardButton("改端口", callback_data=f"{CB_FIELD}port"),
        ],
        [
            InlineKeyboardButton("改用户名", callback_data=f"{CB_FIELD}username"),
            InlineKeyboardButton("改密码", callback_data=f"{CB_FIELD}password"),
        ],
        [InlineKeyboardButton("⬅ 返回服务器", callback_data=f"{CB_FIELD}back")],
    ])


def _field_menu_text(server) -> str:
    auth = "密码" if server.auth_type == "password" else "密钥"
    return (
        f"✏️ 修改服务器信息 — {server.name}\n"
        f"地址: {server.host}:{server.port}\n"
        f"用户名: {server.username}\n"
        f"认证方式: {auth}\n\n"
        "选择要修改的项:"
    )


async def _show_field_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> int:
    server_id = context.user_data[KEY]["server_id"]
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        msg = "服务器不存在(可能已被删除)。"
        if update.callback_query and not new_message:
            await update.callback_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    text = _field_menu_text(server)
    markup = _field_menu_markup(server)
    if update.callback_query and not new_message:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=markup
        )
    return CHOOSE_FIELD


async def cb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    context.user_data[KEY] = {"server_id": server_id}
    return await _show_field_menu(update, context)


async def cb_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    prompts = {
        "name": ("请输入新的服务器别名:", INPUT_NAME),
        "username": ("请输入新的 SSH 登录用户名:", INPUT_USERNAME),
        "port": ("请输入新的 SSH 端口 (1-65535):", INPUT_PORT),
        "password": (
            "请输入新的 SSH 登录密码。\n"
            "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)",
            INPUT_PASSWORD,
        ),
    }
    prompt, state = prompts[field]
    await query.edit_message_text(f"{prompt}\n(发送 /cancel 或点「❌ 取消」中止)")
    return state


async def _log_edit(update: Update, server_id: int, detail: str) -> None:
    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="server.edit",
            result="success",
            detail=detail,
        )
        await s.commit()


async def _test_and_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE, server_id: int
) -> None:
    """改完凭据/端口后测一次 SSH;失败只提示,不回滚。"""
    ctx = get_ctx(context)
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        return
    try:
        ok = await ctx.ssh.check_connectivity(server)
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ 改动已保存,但用新信息做 SSH 连通性测试未通过,请确认填写无误。",
        )


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    server_id = context.user_data[KEY]["server_id"]
    if not name:
        await update.message.reply_text("别名不能为空,请重新输入:")
        return INPUT_NAME
    if len(name) > 64:
        await update.message.reply_text("别名过长(最多 64 字符),请重新输入:")
        return INPUT_NAME
    async with crud.session() as s:
        existing = await crud.get_server_by_name(s, name)
        if existing is not None and existing.id != server_id:
            await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
            return INPUT_NAME
        await crud.update_server(s, server_id, name=name)
        await s.commit()
    await _log_edit(update, server_id, f"name={name}")
    await update.message.reply_text(f"✅ 别名已改为「{name}」。")
    return await _show_field_menu(update, context, new_message=True)


async def step_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or "").strip()
    server_id = context.user_data[KEY]["server_id"]
    if not username:
        await update.message.reply_text("用户名不能为空,请重新输入:")
        return INPUT_USERNAME
    async with crud.session() as s:
        await crud.update_server(s, server_id, username=username)
        await s.commit()
    await _log_edit(update, server_id, f"username={username}")
    await update.message.reply_text(f"✅ 用户名已改为「{username}」,正在测试 SSH…")
    await _test_and_notify(update, context, server_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    server_id = context.user_data[KEY]["server_id"]
    raw = (update.message.text or "").strip()
    # 立刻删除聊天中明文密码
    with suppress(BadRequest):
        await update.message.delete()
    if not raw:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="密码不能为空,请重新输入:"
        )
        return INPUT_PASSWORD
    ctx = get_ctx(context)
    credential = ctx.crypto.encrypt(raw)
    async with crud.session() as s:
        await crud.update_server(
            s, server_id, auth_type="password", credential=credential
        )
        await s.commit()
    await _log_edit(update, server_id, "password updated")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✅ 密码已更新(明文消息已删除),正在测试 SSH…",
    )
    await _test_and_notify(update, context, server_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    server_id = context.user_data[KEY]["server_id"]
    try:
        port = int(raw)
    except ValueError:
        await update.message.reply_text("端口必须是整数,请重新输入:")
        return INPUT_PORT
    if not (1 <= port <= 65535):
        await update.message.reply_text("端口范围 1-65535,请重新输入:")
        return INPUT_PORT
    async with crud.session() as s:
        await crud.update_server(s, server_id, port=port)
        await s.commit()
    await _log_edit(update, server_id, f"port={port}")
    await update.message.reply_text(f"✅ 端口已改为 {port},正在测试 SSH…")
    await _test_and_notify(update, context, server_id)
    return await _show_field_menu(update, context, new_message=True)


async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    server_id = context.user_data.get(KEY, {}).get("server_id")
    context.user_data.pop(KEY, None)
    if server_id is not None:
        await _render_server_menu(update, context, server_id, edit=True)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已退出修改服务器信息。", reply_markup=main_menu_kb()
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_entry, pattern=f"^{CB_EDIT_SERVER}\\d+$"),
        ],
        states={
            CHOOSE_FIELD: [
                CallbackQueryHandler(
                    cb_choose_field,
                    pattern=rf"^{CB_FIELD}(name|username|password|port)$",
                ),
                CallbackQueryHandler(cb_back, pattern=rf"^{CB_FIELD}back$"),
            ],
            INPUT_NAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_name)],
            INPUT_USERNAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_username)],
            INPUT_PASSWORD: [MessageHandler(NON_MENU_TEXT_FILTER, step_password)],
            INPUT_PORT: [MessageHandler(NON_MENU_TEXT_FILTER, step_port)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="editserver",
        persistent=False,
    )
    application.add_handler(conv)
