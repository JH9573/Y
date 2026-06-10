"""修改跳板机信息对话流程。

从跳板机详情点「✏️ 修改跳板机信息」进入,可逐项修改:
  名称 / 地址 / 端口 / 用户名 / 凭据(按当前认证方式) / 切换认证方式

凭据与认证方式的改动会同步影响所有引用该跳板机的服务器。
改完连接信息后会立即对跳板机本身做一次 SSH 连通性测试,失败仅提示,改动照常保存。
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
    CB_EDIT_JUMP,
    CB_JUMP_PREFIX,
    NON_MENU_TEXT_FILTER,
    get_ctx,
    main_menu_kb,
)


log = logging.getLogger(__name__)

# 对话状态
(
    CHOOSE_FIELD,
    INPUT_NAME,
    INPUT_HOST,
    INPUT_USERNAME,
    INPUT_CREDENTIAL,
    INPUT_PORT,
    INPUT_SWITCH_CRED,
) = range(7)

KEY = "editjump"

# 字段选择按钮 callback
# ejf:name | ejf:host | ejf:username | ejf:credential | ejf:port | ejf:switch | ejf:back
CB_FIELD = "ejf:"


def _auth_label(auth_type: str) -> str:
    return "密码" if auth_type == "password" else "密钥"


def _field_menu_markup(jump) -> InlineKeyboardMarkup:
    if jump.auth_type == "password":
        cred_label = "改密码"
        switch_label = "切换为密钥认证"
    else:
        cred_label = "改密钥路径"
        switch_label = "切换为密码认证"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("改名称", callback_data=f"{CB_FIELD}name"),
            InlineKeyboardButton("改地址", callback_data=f"{CB_FIELD}host"),
        ],
        [
            InlineKeyboardButton("改端口", callback_data=f"{CB_FIELD}port"),
            InlineKeyboardButton("改用户名", callback_data=f"{CB_FIELD}username"),
        ],
        [
            InlineKeyboardButton(cred_label, callback_data=f"{CB_FIELD}credential"),
            InlineKeyboardButton(switch_label, callback_data=f"{CB_FIELD}switch"),
        ],
        [InlineKeyboardButton("⬅ 返回跳板机", callback_data=f"{CB_FIELD}back")],
    ])


def _field_menu_text(jump, used_count: int) -> str:
    return (
        f"✏️ 修改跳板机信息 — {jump.name}\n"
        f"地址: {jump.host}:{jump.port}\n"
        f"用户名: {jump.username}\n"
        f"认证方式: {_auth_label(jump.auth_type)}\n"
        f"使用中的服务器: {used_count} 台\n\n"
        "选择要修改的项:"
    )


async def _show_field_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool = False
) -> int:
    jump_id = context.user_data[KEY]["jump_id"]
    async with crud.session() as s:
        jump = await crud.get_jump_host(s, jump_id)
        users = await crud.servers_using_jump_host(s, jump_id) if jump else []
    if jump is None:
        msg = "跳板机不存在(可能已被删除)。"
        if update.callback_query and not new_message:
            await update.callback_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    text = _field_menu_text(jump, len(users))
    markup = _field_menu_markup(jump)
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
    jump_id = int(query.data.split(":", 1)[1])
    context.user_data[KEY] = {"jump_id": jump_id}
    return await _show_field_menu(update, context)


async def cb_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    jump_id = context.user_data[KEY]["jump_id"]
    async with crud.session() as s:
        jump = await crud.get_jump_host(s, jump_id)
    if jump is None:
        await query.edit_message_text("跳板机不存在(可能已被删除)。")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    if field == "credential":
        context.user_data[KEY]["cred_mode"] = jump.auth_type
        if jump.auth_type == "password":
            prompt = (
                "请输入新的 SSH 登录密码。\n"
                "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
            )
        else:
            prompt = "请输入新的私钥文件在 **bot 服务器上**的绝对路径(密钥内容不入库):"
        await query.edit_message_text(f"{prompt}\n(发送 /cancel 或点「❌ 取消」中止)")
        return INPUT_CREDENTIAL

    if field == "switch":
        target = "key" if jump.auth_type == "password" else "password"
        context.user_data[KEY]["switch_target"] = target
        if target == "password":
            prompt = (
                "切换为密码认证。请输入 SSH 登录密码。\n"
                "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
            )
        else:
            prompt = (
                "切换为密钥认证。请输入私钥文件在 **bot 服务器上**的绝对路径"
                "(密钥内容不入库):"
            )
        await query.edit_message_text(f"{prompt}\n(发送 /cancel 或点「❌ 取消」中止)")
        return INPUT_SWITCH_CRED

    prompts = {
        "name": ("请输入新的跳板机别名:", INPUT_NAME),
        "host": ("请输入新的跳板机地址 (IP 或域名):", INPUT_HOST),
        "username": ("请输入新的 SSH 登录用户名:", INPUT_USERNAME),
        "port": ("请输入新的 SSH 端口 (1-65535):", INPUT_PORT),
    }
    prompt, state = prompts[field]
    await query.edit_message_text(f"{prompt}\n(发送 /cancel 或点「❌ 取消」中止)")
    return state


async def _log_edit(update: Update, detail: str) -> None:
    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="jump.edit",
            result="success",
            detail=detail,
        )
        await s.commit()


async def _test_and_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE, jump_id: int
) -> None:
    """改完连接信息后测一次跳板机 SSH;失败只提示,不回滚。"""
    ctx = get_ctx(context)
    async with crud.session() as s:
        jump = await crud.get_jump_host(s, jump_id)
    if jump is None:
        return
    ok = await ctx.ssh.check_jump_connectivity(jump)
    if not ok:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ 改动已保存,但用新信息连接跳板机未通过,请确认填写无误。",
        )


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    jump_id = context.user_data[KEY]["jump_id"]
    if not name:
        await update.message.reply_text("别名不能为空,请重新输入:")
        return INPUT_NAME
    if len(name) > 64:
        await update.message.reply_text("别名过长(最多 64 字符),请重新输入:")
        return INPUT_NAME
    async with crud.session() as s:
        existing = await crud.get_jump_host_by_name(s, name)
        if existing is not None and existing.id != jump_id:
            await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
            return INPUT_NAME
        await crud.update_jump_host(s, jump_id, name=name)
        await s.commit()
    await _log_edit(update, f"jump name={name}")
    await update.message.reply_text(f"✅ 别名已改为「{name}」。")
    return await _show_field_menu(update, context, new_message=True)


async def step_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    host = (update.message.text or "").strip()
    jump_id = context.user_data[KEY]["jump_id"]
    if not host:
        await update.message.reply_text("地址不能为空,请重新输入:")
        return INPUT_HOST
    async with crud.session() as s:
        await crud.update_jump_host(s, jump_id, host=host)
        await s.commit()
    await _log_edit(update, f"jump host={host}")
    await update.message.reply_text(f"✅ 地址已改为「{host}」,正在测试 SSH…")
    await _test_and_notify(update, context, jump_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or "").strip()
    jump_id = context.user_data[KEY]["jump_id"]
    if not username:
        await update.message.reply_text("用户名不能为空,请重新输入:")
        return INPUT_USERNAME
    async with crud.session() as s:
        await crud.update_jump_host(s, jump_id, username=username)
        await s.commit()
    await _log_edit(update, f"jump username={username}")
    await update.message.reply_text(f"✅ 用户名已改为「{username}」,正在测试 SSH…")
    await _test_and_notify(update, context, jump_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    jump_id = context.user_data[KEY]["jump_id"]
    try:
        port = int(raw)
    except ValueError:
        await update.message.reply_text("端口必须是整数,请重新输入:")
        return INPUT_PORT
    if not (1 <= port <= 65535):
        await update.message.reply_text("端口范围 1-65535,请重新输入:")
        return INPUT_PORT
    async with crud.session() as s:
        await crud.update_jump_host(s, jump_id, port=port)
        await s.commit()
    await _log_edit(update, f"jump port={port}")
    await update.message.reply_text(f"✅ 端口已改为 {port},正在测试 SSH…")
    await _test_and_notify(update, context, jump_id)
    return await _show_field_menu(update, context, new_message=True)


async def _read_credential_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, auth_type: str
) -> str | None:
    """按认证方式读取并处理凭据输入;密码会删聊天记录并加密。"""
    raw = (update.message.text or "").strip()
    if auth_type == "password":
        with suppress(BadRequest):
            await update.message.delete()
        if not raw:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text="密码不能为空,请重新输入:"
            )
            return None
        return get_ctx(context).crypto.encrypt(raw)
    if not raw:
        await update.message.reply_text("路径不能为空,请重新输入:")
        return None
    return raw


async def step_credential(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    jump_id = context.user_data[KEY]["jump_id"]
    mode = context.user_data[KEY].get("cred_mode", "password")
    credential = await _read_credential_input(update, context, mode)
    if credential is None:
        return INPUT_CREDENTIAL
    async with crud.session() as s:
        await crud.update_jump_host(s, jump_id, credential=credential)
        await s.commit()
    if mode == "password":
        await _log_edit(update, "jump password updated")
        text = "✅ 密码已更新(明文消息已删除),正在测试 SSH…"
    else:
        await _log_edit(update, "jump key path updated")
        text = "✅ 密钥路径已更新,正在测试 SSH…"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
    await _test_and_notify(update, context, jump_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_switch_credential(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    jump_id = context.user_data[KEY]["jump_id"]
    target = context.user_data[KEY].get("switch_target", "password")
    credential = await _read_credential_input(update, context, target)
    if credential is None:
        return INPUT_SWITCH_CRED
    async with crud.session() as s:
        await crud.update_jump_host(
            s, jump_id, auth_type=target, credential=credential
        )
        await s.commit()
    await _log_edit(update, f"jump auth_type switched to {target}")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ 认证方式已切换为{_auth_label(target)},正在测试 SSH…",
    )
    await _test_and_notify(update, context, jump_id)
    return await _show_field_menu(update, context, new_message=True)


async def cb_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from .jump_host import _render_jump_menu

    query = update.callback_query
    await query.answer()
    jump_id = context.user_data.get(KEY, {}).get("jump_id")
    context.user_data.pop(KEY, None)
    if jump_id is not None:
        await _render_jump_menu(update, context, jump_id)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已退出修改跳板机信息。", reply_markup=main_menu_kb()
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_entry, pattern=f"^{CB_EDIT_JUMP}\\d+$"),
        ],
        states={
            CHOOSE_FIELD: [
                CallbackQueryHandler(
                    cb_choose_field,
                    pattern=rf"^{CB_FIELD}(name|host|username|credential|port|switch)$",
                ),
                CallbackQueryHandler(cb_back, pattern=rf"^{CB_FIELD}back$"),
            ],
            INPUT_NAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_name)],
            INPUT_HOST: [MessageHandler(NON_MENU_TEXT_FILTER, step_host)],
            INPUT_USERNAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_username)],
            INPUT_CREDENTIAL: [MessageHandler(NON_MENU_TEXT_FILTER, step_credential)],
            INPUT_PORT: [MessageHandler(NON_MENU_TEXT_FILTER, step_port)],
            INPUT_SWITCH_CRED: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_switch_credential)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="editjump",
        persistent=False,
    )
    application.add_handler(conv)
