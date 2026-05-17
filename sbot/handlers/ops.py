"""v2node 服务级操作:状态/启动/重启/停止/日志/版本。

危险操作(重启 / 停止)需点击二次确认按钮。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node import ACTIONS, get_action
from .common import (
    CB_OPS_CONFIRM,
    CB_OPS_PREFIX,
    CB_SERVER_PREFIX,
    get_ctx,
    truncate,
)


log = logging.getLogger(__name__)


async def cb_ops_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """点击某个服务操作:危险操作弹确认,安全操作直接执行。"""
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_str, action = payload.split(":", 1)
    server_id = int(server_id_str)

    try:
        action_def = get_action(action)
    except KeyError:
        await query.edit_message_text("未知操作。")
        return

    if action_def.dangerous:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "确认", callback_data=f"{CB_OPS_CONFIRM}{server_id}:{action}"
                    ),
                    InlineKeyboardButton(
                        "取消", callback_data=f"{CB_SERVER_PREFIX}{server_id}"
                    ),
                ]
            ]
        )
        async with crud.session() as s:
            server = await crud.get_server(s, server_id)
        if server is None:
            await query.edit_message_text("服务器不存在。")
            return
        await query.edit_message_text(
            f"⚠️ 确认对 {server.name} 执行 v2node {action_def.label}?",
            reply_markup=kb,
        )
        return

    # 非危险操作直接执行
    await _execute(update, context, server_id, action)


async def cb_ops_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_str, action = payload.split(":", 1)
    server_id = int(server_id_str)
    await _execute(update, context, server_id, action)


async def _execute(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: int,
    action: str,
) -> None:
    query = update.callback_query
    ctx = get_ctx(context)

    try:
        action_def = get_action(action)
    except KeyError:
        await query.edit_message_text("未授权的操作。")
        return

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return

    await query.edit_message_text(f"执行中: {server.name} · {action_def.label}…")

    result_text: str
    success: bool
    try:
        result = await ctx.ssh.run(server, action_def.command, timeout=30)
        success = result.ok
        # status / logs / version 不一定零退出,但输出仍有用
        output = result.combined or "(无输出)"
        result_text = (
            f"{server.name} · {action_def.label}\n"
            f"exit={result.exit_status}\n"
            f"---\n{truncate(output)}"
        )
    except SSHError as exc:
        success = False
        result_text = f"{server.name} · {action_def.label} 失败:{exc}"

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action=action,
            result="success" if success else "failed",
            detail=truncate(result_text, 500),
        )
        await s.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ 返回菜单", callback_data=f"{CB_SERVER_PREFIX}{server_id}")]]
    )
    await query.edit_message_text(result_text, reply_markup=kb)


def register(application, ctx) -> None:
    action_pattern = "|".join(a.replace(".", r"\.") for a in ACTIONS)
    application.add_handler(
        CallbackQueryHandler(
            cb_ops_click,
            pattern=rf"^{CB_OPS_PREFIX}\d+:({action_pattern})$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_ops_confirm,
            pattern=rf"^{CB_OPS_CONFIRM}\d+:({action_pattern})$",
        )
    )
