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

from ..core.ssh import (
    HostKeyMismatch,
    SSHError,
    check_key_passphrase,
    key_needs_passphrase,
    key_permission_warning,
    validate_key_path,
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
(
    CHOOSE_FIELD,
    INPUT_NAME,
    INPUT_HOST,
    INPUT_USERNAME,
    INPUT_PASSWORD,
    INPUT_PORT,
    INPUT_KEY_PATH,
    INPUT_KEY_PASSPHRASE,
) = range(8)

KEY = "editserver"

# 字段选择按钮 callback
# edsf:name | host | username | password | key | port | hostkey | back
CB_FIELD = "edsf:"


def _field_menu_markup(server) -> InlineKeyboardMarkup:
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
            InlineKeyboardButton("改密码", callback_data=f"{CB_FIELD}password"),
            InlineKeyboardButton("改私钥", callback_data=f"{CB_FIELD}key"),
        ],
        [InlineKeyboardButton("🔑 重置主机密钥", callback_data=f"{CB_FIELD}hostkey")],
        [InlineKeyboardButton("⬅ 返回服务器", callback_data=f"{CB_FIELD}back")],
    ])


def _field_menu_text(server) -> str:
    auth = "密码" if server.auth_type == "password" else "密钥"
    if server.auth_type == "key":
        auth += f" ({server.credential}"
        auth += ",带口令)" if server.key_passphrase else ")"
    fingerprint = server.host_key or "(尚未记录,下次连接时记录)"
    return (
        f"✏️ 修改服务器信息 — {server.name}\n"
        f"地址: {server.host}:{server.port}\n"
        f"用户名: {server.username}\n"
        f"认证方式: {auth}\n"
        f"主机密钥: {fingerprint}\n\n"
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
        "host": ("请输入新的服务器地址 (IP 或域名):", INPUT_HOST),
        "username": ("请输入新的 SSH 登录用户名:", INPUT_USERNAME),
        "port": ("请输入新的 SSH 端口 (1-65535):", INPUT_PORT),
        "password": (
            "请输入新的 SSH 登录密码。\n"
            "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)",
            INPUT_PASSWORD,
        ),
        "key": (
            "请输入私钥文件在 bot 服务器上的绝对路径(密钥内容不入库)。\n"
            "若该私钥有口令保护,下一步会让你输入口令。",
            INPUT_KEY_PATH,
        ),
    }
    if field == "hostkey":
        return await _reset_host_key(update, context)
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
    except HostKeyMismatch as exc:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ 改动已保存,但主机密钥校验没过:\n{exc}",
        )
        return
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


async def step_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    host = (update.message.text or "").strip()
    server_id = context.user_data[KEY]["server_id"]
    if not host:
        await update.message.reply_text("地址不能为空,请重新输入:")
        return INPUT_HOST
    async with crud.session() as s:
        await crud.update_server(s, server_id, host=host)
        await s.commit()
    await _log_edit(update, server_id, f"host={host}")
    await update.message.reply_text(f"✅ 地址已改为「{host}」,正在测试 SSH…")
    await _test_and_notify(update, context, server_id)
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


async def _reset_host_key(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """清掉已记录的主机密钥指纹,下次连接重新 TOFU。

    服务器重装 / 换机后指纹必然变化,此时连接会被拒;走这里显式确认一次,
    比默默接受新指纹安全。
    """
    server_id = context.user_data[KEY]["server_id"]
    async with crud.session() as s:
        await crud.clear_server_host_key(s, server_id)
        await s.commit()
    await _log_edit(update, server_id, "host key reset")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✅ 已清除记录的主机密钥指纹,下次连接会重新记录当前服务器的指纹。",
    )
    return await _show_field_menu(update, context, new_message=True)


async def step_key_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    server_id = context.user_data[KEY]["server_id"]
    try:
        path = validate_key_path(raw)
        needs_pass = key_needs_passphrase(path)
    except SSHError as exc:
        await update.message.reply_text(f"{exc}\n请重新输入私钥路径:")
        return INPUT_KEY_PATH
    warning = key_permission_warning(path)
    if warning:
        await update.message.reply_text(warning)
    if needs_pass:
        context.user_data[KEY]["pending_key_path"] = path
        await update.message.reply_text(
            "该私钥有口令保护,请输入口令。\n"
            "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
        )
        return INPUT_KEY_PASSPHRASE

    async with crud.session() as s:
        await crud.update_server(
            s, server_id, auth_type="key", credential=path,
            clear_key_passphrase=True,
        )
        await s.commit()
    await _log_edit(update, server_id, f"key path updated: {path}")
    await update.message.reply_text("✅ 私钥已更新,正在测试 SSH…")
    await _test_and_notify(update, context, server_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_key_passphrase(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    server_id = data["server_id"]
    raw = (update.message.text or "").strip()
    with suppress(BadRequest):
        await update.message.delete()
    chat_id = update.effective_chat.id
    path = data.get("pending_key_path")
    if not raw:
        await context.bot.send_message(chat_id=chat_id, text="口令不能为空,请重新输入:")
        return INPUT_KEY_PASSPHRASE
    try:
        check_key_passphrase(path, raw)
    except SSHError as exc:
        await context.bot.send_message(chat_id=chat_id, text=f"{exc}\n请重新输入口令:")
        return INPUT_KEY_PASSPHRASE
    ctx = get_ctx(context)
    async with crud.session() as s:
        await crud.update_server(
            s, server_id, auth_type="key", credential=path,
            key_passphrase=ctx.crypto.encrypt(raw),
        )
        await s.commit()
    data.pop("pending_key_path", None)
    await _log_edit(update, server_id, f"key path updated: {path} (with passphrase)")
    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ 私钥与口令已更新(明文消息已删除),正在测试 SSH…",
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
                    pattern=rf"^{CB_FIELD}(name|host|username|password|key|port|hostkey)$",
                ),
                CallbackQueryHandler(cb_back, pattern=rf"^{CB_FIELD}back$"),
            ],
            INPUT_NAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_name)],
            INPUT_HOST: [MessageHandler(NON_MENU_TEXT_FILTER, step_host)],
            INPUT_USERNAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_username)],
            INPUT_PASSWORD: [MessageHandler(NON_MENU_TEXT_FILTER, step_password)],
            INPUT_PORT: [MessageHandler(NON_MENU_TEXT_FILTER, step_port)],
            INPUT_KEY_PATH: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_key_path)
            ],
            INPUT_KEY_PASSPHRASE: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_key_passphrase)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="editserver",
        persistent=False,
    )
    application.add_handler(conv)
