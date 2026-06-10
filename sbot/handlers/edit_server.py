"""修改服务器信息对话流程。

从服务器菜单点「✏️ 修改服务器信息」进入,可逐项修改:
  名称 / 地址 / 用户名 / 凭据(密码或密钥路径,按当前认证方式) / SSH 端口
  切换认证方式(密码 ⇄ 密钥) / 跳板机(从已登记列表中选用或改为直连)

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
) = range(8)

KEY = "editserver"

# 字段选择按钮 callback
# edsf:name | edsf:host | edsf:username | edsf:credential | edsf:port
# | edsf:switch | edsf:jump | edsf:back
CB_FIELD = "edsf:"
# 跳板机子菜单 callback: edjp:sel:<id> | edjp:direct | edjp:back
CB_JUMP = "edjp:"


def _auth_label(auth_type: str | None) -> str:
    return "密码" if auth_type == "password" else "密钥"


def _jump_desc(server) -> str:
    if server.jump is None:
        return "直连(未使用跳板机)"
    jump = server.jump
    return f"{jump.name} ({jump.username}@{jump.host}:{jump.port})"


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
        jumps = await crud.list_jump_hosts(s)
    if server is None:
        msg = "服务器不存在(可能已被删除)。"
        if update.callback_query and not new_message:
            await update.callback_query.edit_message_text(msg)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    text = (
        f"🪜 跳板机 — {server.name}\n"
        f"当前: {_jump_desc(server)}\n\n"
    )
    rows = []
    if jumps:
        text += "选择要使用的跳板机:"
        for j in jumps:
            mark = "✅ " if server.jump_host_id == j.id else ""
            rows.append([InlineKeyboardButton(
                f"{mark}🪜 {j.name} ({j.username}@{j.host})",
                callback_data=f"{CB_JUMP}sel:{j.id}",
            )])
    else:
        text += "尚未登记任何跳板机,请到 服务器管理 → 跳板机管理 中添加。"
    direct_mark = "✅ " if server.jump_host_id is None else ""
    rows.append([InlineKeyboardButton(
        f"{direct_mark}直连(不使用跳板机)", callback_data=f"{CB_JUMP}direct"
    )])
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
    payload = query.data.split(":", 1)[1]
    server_id = context.user_data[KEY]["server_id"]

    if payload == "back":
        await query.answer()
        return await _show_field_menu(update, context)

    if payload == "direct":
        async with crud.session() as s:
            server = await crud.get_server(s, server_id)
            changed = server is not None and server.jump_host_id is not None
            if changed:
                await crud.set_server_jump_host(s, server_id, None)
                await s.commit()
        if not changed:
            await query.answer("已是直连")
            return JUMP_MENU
        await _log_edit(update, server_id, "jump=direct")
        await query.answer("已改为直连")
        await query.edit_message_text("✅ 已改为直连,正在测试 SSH…")
        await _test_and_notify(update, context, server_id)
        return await _show_field_menu(update, context, new_message=True)

    jump_id = int(payload.split(":", 1)[1])
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        jump = await crud.get_jump_host(s, jump_id)
        if jump is None:
            await query.answer("跳板机不存在(可能刚被删除)")
            return await _show_jump_menu(update, context)
        if server is not None and server.jump_host_id == jump_id:
            await query.answer("已在使用该跳板机")
            return JUMP_MENU
        await crud.set_server_jump_host(s, server_id, jump_id)
        await s.commit()
        jump_name = jump.name
    await _log_edit(update, server_id, f"jump={jump_name}")
    await query.answer()
    await query.edit_message_text(f"✅ 已选用跳板机「{jump_name}」,正在测试 SSH…")
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
                    cb_jump_action, pattern=rf"^{CB_JUMP}(sel:\d+|direct|back)$"
                ),
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
