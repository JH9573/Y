"""跳板机列表 / 详情 / 删除。

入口在「服务器管理」二级菜单的「🪜 跳板机管理」。
跳板机统一在此登记,各服务器在添加 / 修改流程里从已登记列表中选用。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..db import crud
from .common import (
    CB_BACK_JUMPS,
    CB_DEL_JUMP,
    CB_DEL_JUMP_OK,
    CB_EDIT_JUMP,
    CB_JUMP_ADD,
    CB_JUMP_PREFIX,
    CB_MENU_JUMP_LIST,
)


log = logging.getLogger(__name__)


def _auth_label(auth_type: str) -> str:
    return "密码" if auth_type == "password" else "密钥"


async def _render_jump_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool
) -> None:
    async with crud.session() as s:
        jumps = await crud.list_jump_hosts(s)

    buttons = [
        [InlineKeyboardButton(j.name, callback_data=f"{CB_JUMP_PREFIX}{j.id}")]
        for j in jumps
    ]
    buttons.append(
        [InlineKeyboardButton("➕ 添加跳板机", callback_data=CB_JUMP_ADD)]
    )
    markup = InlineKeyboardMarkup(buttons)
    text = (
        "🪜 跳板机管理\n已登记的跳板机:" if jumps
        else "🪜 跳板机管理\n当前没有已登记的跳板机。"
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def cb_jump_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_jump_list(update, context, edit=True)


async def cb_open_jump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    jump_id = int(query.data.split(":", 1)[1])
    await _render_jump_menu(update, context, jump_id)


async def _render_jump_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, jump_id: int
) -> None:
    async with crud.session() as s:
        jump = await crud.get_jump_host(s, jump_id)
        users = await crud.servers_using_jump_host(s, jump_id) if jump else []
    if jump is None:
        await update.callback_query.edit_message_text("跳板机不存在(可能已被删除)。")
        return

    used_by = "、".join(srv.name for srv in users) if users else "(无)"
    header = (
        f"🪜 跳板机 {jump.name}\n"
        f"地址: {jump.username}@{jump.host}:{jump.port}\n"
        f"认证方式: {_auth_label(jump.auth_type)}\n"
        f"使用中的服务器: {used_by}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✏️ 修改跳板机信息", callback_data=f"{CB_EDIT_JUMP}{jump.id}"
        )],
        [InlineKeyboardButton(
            "🗑 删除跳板机", callback_data=f"{CB_DEL_JUMP}{jump.id}"
        )],
        [InlineKeyboardButton("⬅ 返回列表", callback_data=CB_BACK_JUMPS)],
    ])
    await update.callback_query.edit_message_text(header, reply_markup=kb)


async def cb_back_jumps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_jump_list(update, context, edit=True)


async def cb_delete_jump_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    jump_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        jump = await crud.get_jump_host(s, jump_id)
        users = await crud.servers_using_jump_host(s, jump_id) if jump else []
    if jump is None:
        await query.edit_message_text("跳板机不存在。")
        return
    warn = ""
    if users:
        names = "、".join(srv.name for srv in users)
        warn = f"\n⚠️ 有 {len(users)} 台服务器正在使用它({names}),删除后这些服务器将恢复直连。"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认删除", callback_data=f"{CB_DEL_JUMP_OK}{jump.id}"
                ),
                InlineKeyboardButton(
                    "取消", callback_data=f"{CB_JUMP_PREFIX}{jump.id}"
                ),
            ]
        ]
    )
    await query.edit_message_text(
        f"⚠️ 确认删除跳板机「{jump.name}」?{warn}",
        reply_markup=kb,
    )


async def cb_delete_jump_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    jump_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        jump = await crud.get_jump_host(s, jump_id)
        if jump is None:
            await query.edit_message_text("跳板机不存在。")
            return
        name = jump.name
        await crud.delete_jump_host(s, jump_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="jump.delete",
            result="success",
            detail=f"jump={name}",
        )
        await s.commit()
    await query.edit_message_text(f"已删除跳板机「{name}」,原引用它的服务器已恢复直连。")


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(cb_jump_list, pattern=f"^{CB_MENU_JUMP_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_open_jump, pattern=f"^{CB_JUMP_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_back_jumps, pattern=f"^{CB_BACK_JUMPS}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_delete_jump_confirm, pattern=f"^{CB_DEL_JUMP}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_delete_jump_do, pattern=f"^{CB_DEL_JUMP_OK}\\d+$")
    )
