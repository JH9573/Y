"""修改服务器信息对话流程。

从服务器菜单点「✏️ 修改服务器信息」进入,可逐项修改:
  名称 / 地址 / 用户名 / 凭据(密码或密钥路径,按当前认证方式) / SSH 端口
  切换认证方式(密码 ⇄ 密钥) / 跳板机(设置 / 修改 / 移除)

改 凭据 / 端口 / 跳板机 后会立即做一次 SSH 连通性测试,失败仅提示,改动照常保存。
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
(
    CHOOSE_FIELD,
    INPUT_NAME,
    INPUT_HOST,
    INPUT_USERNAME,
    INPUT_CREDENTIAL,
    INPUT_PORT,
    INPUT_SWITCH_CRED,
    JUMP_MENU,
    INPUT_JUMP_HOST,
    INPUT_JUMP_PORT,
    INPUT_JUMP_USERNAME,
    CHOOSE_JUMP_AUTH,
    INPUT_JUMP_CREDENTIAL,
) = range(13)

KEY = "editserver"

# 字段选择按钮 callback
# edsf:name | edsf:host | edsf:username | edsf:credential | edsf:port
# | edsf:switch | edsf:jump | edsf:back
CB_FIELD = "edsf:"
# 跳板机子菜单 callback: edjp:set | edjp:remove | edjp:back
CB_JUMP = "edjp:"
# 跳板机认证方式选择: ejauth:key | ejauth:password
CB_JUMP_AUTH = "ejauth:"


def _auth_label(auth_type: str | None) -> str:
    return "密码" if auth_type == "password" else "密钥"


def _jump_desc(server) -> str:
    if not server.jump_host:
        return "未配置(直连)"
    return (
        f"{server.jump_username}@{server.jump_host}:{server.jump_port}"
        f"({_auth_label(server.jump_auth_type)}认证)"
    )


def _field_menu_markup(server) -> InlineKeyboardMarkup:
    if server.auth_type == "password":
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
        [
            InlineKeyboardButton("🪜 跳板机", callback_data=f"{CB_FIELD}jump"),
        ],
        [InlineKeyboardButton("⬅ 返回服务器", callback_data=f"{CB_FIELD}back")],
    ])


def _field_menu_text(server) -> str:
    return (
        f"✏️ 修改服务器信息 — {server.name}\n"
        f"地址: {server.host}:{server.port}\n"
        f"用户名: {server.username}\n"
        f"认证方式: {_auth_label(server.auth_type)}\n"
        f"跳板机: {_jump_desc(server)}\n\n"
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
    server_id = context.user_data[KEY]["server_id"]
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在(可能已被删除)。")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    if field == "credential":
        # 只更新当前认证方式的凭据,不切换认证方式
        context.user_data[KEY]["cred_mode"] = server.auth_type
        if server.auth_type == "password":
            prompt = (
                "请输入新的 SSH 登录密码。\n"
                "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
            )
        else:
            prompt = "请输入新的私钥文件在 **bot 服务器上**的绝对路径(密钥内容不入库):"
        await query.edit_message_text(f"{prompt}\n(发送 /cancel 或点「❌ 取消」中止)")
        return INPUT_CREDENTIAL

    if field == "switch":
        target = "key" if server.auth_type == "password" else "password"
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

    if field == "jump":
        return await _show_jump_menu(update, context)

    prompts = {
        "name": ("请输入新的服务器别名:", INPUT_NAME),
        "host": ("请输入新的服务器地址 (IP 或域名):", INPUT_HOST),
        "username": ("请输入新的 SSH 登录用户名:", INPUT_USERNAME),
        "port": ("请输入新的 SSH 端口 (1-65535):", INPUT_PORT),
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


async def _read_credential_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, auth_type: str
) -> str | None:
    """按认证方式读取并处理凭据输入;密码会删聊天记录并加密。

    输入为空时返回 None,调用方应留在当前状态重新提示。
    """
    raw = (update.message.text or "").strip()
    if auth_type == "password":
        # 立刻删除聊天中明文密码
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
    """更新当前认证方式的凭据(密码或密钥路径),不切换认证方式。"""
    server_id = context.user_data[KEY]["server_id"]
    mode = context.user_data[KEY].get("cred_mode", "password")
    credential = await _read_credential_input(update, context, mode)
    if credential is None:
        return INPUT_CREDENTIAL
    async with crud.session() as s:
        await crud.update_server(s, server_id, credential=credential)
        await s.commit()
    if mode == "password":
        await _log_edit(update, server_id, "password updated")
        text = "✅ 密码已更新(明文消息已删除),正在测试 SSH…"
    else:
        await _log_edit(update, server_id, "key path updated")
        text = "✅ 密钥路径已更新,正在测试 SSH…"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
    await _test_and_notify(update, context, server_id)
    return await _show_field_menu(update, context, new_message=True)


async def step_switch_credential(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """切换认证方式:录入新方式的凭据后,认证方式与凭据一并更新。"""
    server_id = context.user_data[KEY]["server_id"]
    target = context.user_data[KEY].get("switch_target", "password")
    credential = await _read_credential_input(update, context, target)
    if credential is None:
        return INPUT_SWITCH_CRED
    async with crud.session() as s:
        await crud.update_server(
            s, server_id, auth_type=target, credential=credential
        )
        await s.commit()
    await _log_edit(update, server_id, f"auth_type switched to {target}")
    text = (
        f"✅ 认证方式已切换为{_auth_label(target)},正在测试 SSH…"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
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


# ---------- 跳板机 ----------

async def _show_jump_menu(
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

    configured = bool(server.jump_host)
    text = (
        f"🪜 跳板机 — {server.name}\n"
        f"当前: {_jump_desc(server)}\n\n"
        "选择操作:"
    )
    rows = [[InlineKeyboardButton(
        "修改跳板机" if configured else "设置跳板机",
        callback_data=f"{CB_JUMP}set",
    )]]
    if configured:
        rows.append([InlineKeyboardButton("移除跳板机(改为直连)", callback_data=f"{CB_JUMP}remove")])
    rows.append([InlineKeyboardButton("⬅ 返回", callback_data=f"{CB_JUMP}back")])
    markup = InlineKeyboardMarkup(rows)

    if update.callback_query and not new_message:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=markup
        )
    return JUMP_MENU


async def cb_jump_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    server_id = context.user_data[KEY]["server_id"]

    if action == "back":
        await query.answer()
        return await _show_field_menu(update, context)

    if action == "remove":
        async with crud.session() as s:
            await crud.clear_server_jump(s, server_id)
            await s.commit()
        await _log_edit(update, server_id, "jump host removed")
        await query.answer("已移除跳板机,恢复直连")
        return await _show_field_menu(update, context)

    # set / 修改:走录入子流程
    await query.answer()
    context.user_data[KEY]["jump"] = {}
    await query.edit_message_text(
        "请输入跳板机地址(IP 或域名):\n(发送 /cancel 或点「❌ 取消」中止)"
    )
    return INPUT_JUMP_HOST


async def step_jump_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    host = (update.message.text or "").strip()
    if not host:
        await update.message.reply_text("跳板机地址不能为空,请重新输入:")
        return INPUT_JUMP_HOST
    context.user_data[KEY]["jump"]["host"] = host
    await update.message.reply_text("请输入跳板机 SSH 端口(直接回车或发送 / 使用默认 22):")
    return INPUT_JUMP_PORT


async def step_jump_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if raw in ("", "/"):
        port = 22
    else:
        try:
            port = int(raw)
        except ValueError:
            await update.message.reply_text("端口必须是整数,请重新输入:")
            return INPUT_JUMP_PORT
        if not (1 <= port <= 65535):
            await update.message.reply_text("端口范围 1-65535,请重新输入:")
            return INPUT_JUMP_PORT
    context.user_data[KEY]["jump"]["port"] = port
    await update.message.reply_text("请输入跳板机 SSH 登录用户名:")
    return INPUT_JUMP_USERNAME


async def step_jump_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("用户名不能为空,请重新输入:")
        return INPUT_JUMP_USERNAME
    context.user_data[KEY]["jump"]["username"] = username
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("密钥", callback_data=f"{CB_JUMP_AUTH}key"),
                InlineKeyboardButton("密码", callback_data=f"{CB_JUMP_AUTH}password"),
            ]
        ]
    )
    await update.message.reply_text("选择跳板机认证方式:", reply_markup=kb)
    return CHOOSE_JUMP_AUTH


async def cb_jump_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    context.user_data[KEY]["jump"]["auth_type"] = choice
    if choice == "key":
        await query.edit_message_text(
            "请输入跳板机私钥文件在 **bot 服务器上**的绝对路径(密钥内容不入库)。"
        )
    else:
        await query.edit_message_text(
            "请输入跳板机 SSH 登录密码。\n"
            "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
        )
    return INPUT_JUMP_CREDENTIAL


async def step_jump_credential(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    server_id = context.user_data[KEY]["server_id"]
    jump = context.user_data[KEY]["jump"]
    credential = await _read_credential_input(update, context, jump["auth_type"])
    if credential is None:
        return INPUT_JUMP_CREDENTIAL
    async with crud.session() as s:
        await crud.set_server_jump(
            s,
            server_id,
            host=jump["host"],
            port=jump["port"],
            username=jump["username"],
            auth_type=jump["auth_type"],
            credential=credential,
        )
        await s.commit()
    await _log_edit(
        update,
        server_id,
        f"jump={jump['username']}@{jump['host']}:{jump['port']} ({jump['auth_type']})",
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✅ 跳板机已保存,正在测试 SSH…",
    )
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
                    pattern=rf"^{CB_FIELD}(name|host|username|credential|port|switch|jump)$",
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
            JUMP_MENU: [
                CallbackQueryHandler(
                    cb_jump_action, pattern=rf"^{CB_JUMP}(set|remove|back)$"
                ),
            ],
            INPUT_JUMP_HOST: [MessageHandler(NON_MENU_TEXT_FILTER, step_jump_host)],
            INPUT_JUMP_PORT: [MessageHandler(NON_MENU_TEXT_FILTER, step_jump_port)],
            INPUT_JUMP_USERNAME: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_jump_username)
            ],
            CHOOSE_JUMP_AUTH: [
                CallbackQueryHandler(
                    cb_jump_auth, pattern=rf"^{CB_JUMP_AUTH}(key|password)$"
                ),
            ],
            INPUT_JUMP_CREDENTIAL: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_jump_credential)
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
