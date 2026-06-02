"""服务器列表 / 详情菜单 / 删除服务器。"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..db import crud
from .common import (
    CB_BACK_SERVERS,
    CB_DEL_SERVER,
    CB_DEL_SERVER_OK,
    CB_EDIT_SERVER,
    CB_INSTALL_START,
    CB_NODE_MENU,
    CB_OPS_PREFIX,
    CB_SERVER_PREFIX,
    CB_UNINSTALL_START,
    CB_V2NODE_MENU,
    get_ctx,
    main_menu_kb,
)
from ..services.v2node import ACTIONS


log = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "你好,这是 v2node 服务器管理 bot。\n\n"
        "用下方菜单按钮操作,也可继续使用斜杠命令:\n"
        "/server  /addserver  /panel  /addpanel  /logs  /update  /cancel"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu_kb())


async def cmd_server_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with crud.session() as s:
        servers = await crud.list_servers(s)

    if not servers:
        await update.effective_message.reply_text(
            "当前没有已登记的服务器,点「➕ 添加服务器」或发 /addserver 添加。"
        )
        return

    buttons = [
        [InlineKeyboardButton(srv.name, callback_data=f"{CB_SERVER_PREFIX}{srv.id}")]
        for srv in servers
    ]
    await update.effective_message.reply_text(
        "选择一台服务器:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_open_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    await _render_server_menu(update, context, server_id, edit=True)


async def cb_open_v2node_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    await _render_v2node_menu(update, context, server_id)


async def cb_back_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    async with crud.session() as s:
        servers = await crud.list_servers(s)
    if not servers:
        await query.edit_message_text("当前没有已登记的服务器。")
        return
    buttons = [
        [InlineKeyboardButton(srv.name, callback_data=f"{CB_SERVER_PREFIX}{srv.id}")]
        for srv in servers
    ]
    await query.edit_message_text(
        "选择一台服务器:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _render_server_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: int,
    *,
    edit: bool,
) -> None:
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        msg = "服务器不存在(可能已被删除)。"
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return

    installed_text = "已安装" if server.v2node_installed else "未安装"
    header = (
        f"服务器 {server.name} ({server.host}:{server.port})\n"
        f"状态: {server.status}\n"
        f"v2node: {installed_text}"
    )

    kb = [
        [InlineKeyboardButton(
            "🧩 v2node 管理", callback_data=f"{CB_V2NODE_MENU}{server.id}"
        )],
        [InlineKeyboardButton(
            "✏️ 修改服务器信息", callback_data=f"{CB_EDIT_SERVER}{server.id}"
        )],
        [InlineKeyboardButton(
            "🗑 删除服务器", callback_data=f"{CB_DEL_SERVER}{server.id}"
        )],
        [InlineKeyboardButton("⬅ 返回列表", callback_data=CB_BACK_SERVERS)],
    ]
    markup = InlineKeyboardMarkup(kb)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(header, reply_markup=markup)
    else:
        await update.effective_message.reply_text(header, reply_markup=markup)


async def _render_v2node_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    server_id: int,
) -> None:
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        if update.callback_query:
            await update.callback_query.edit_message_text("服务器不存在(可能已被删除)。")
        return

    installed_text = "已安装" if server.v2node_installed else "未安装"
    header = (
        f"v2node 管理 — {server.name}\n"
        f"v2node: {installed_text}"
    )

    if server.v2node_installed:
        kb = [
            [
                InlineKeyboardButton(
                    "v2node 状态", callback_data=f"{CB_OPS_PREFIX}{server.id}:v2node.status"
                ),
                InlineKeyboardButton(
                    "启动", callback_data=f"{CB_OPS_PREFIX}{server.id}:v2node.start"
                ),
                InlineKeyboardButton(
                    "重启", callback_data=f"{CB_OPS_PREFIX}{server.id}:v2node.restart"
                ),
            ],
            [
                InlineKeyboardButton(
                    "停止", callback_data=f"{CB_OPS_PREFIX}{server.id}:v2node.stop"
                ),
                InlineKeyboardButton(
                    "日志", callback_data=f"{CB_OPS_PREFIX}{server.id}:v2node.logs"
                ),
                InlineKeyboardButton(
                    "版本", callback_data=f"{CB_OPS_PREFIX}{server.id}:v2node.version"
                ),
            ],
            [
                InlineKeyboardButton(
                    "节点管理", callback_data=f"{CB_NODE_MENU}{server.id}"
                ),
                InlineKeyboardButton(
                    "卸载 v2node",
                    callback_data=f"{CB_UNINSTALL_START}{server.id}",
                ),
            ],
        ]
    else:
        kb = [
            [InlineKeyboardButton(
                "安装 v2node", callback_data=f"{CB_INSTALL_START}{server.id}"
            )],
        ]
    # 顺便确保白名单 actions 都有(只做一次防御检查)
    for action in [
        "v2node.status",
        "v2node.start",
        "v2node.restart",
        "v2node.stop",
        "v2node.logs",
        "v2node.version",
    ]:
        assert action in ACTIONS

    kb.append([InlineKeyboardButton(
        "⬅ 返回服务器", callback_data=f"{CB_SERVER_PREFIX}{server.id}"
    )])
    markup = InlineKeyboardMarkup(kb)

    if update.callback_query:
        await update.callback_query.edit_message_text(header, reply_markup=markup)
    else:
        await update.effective_message.reply_text(header, reply_markup=markup)


# ---------- 删除服务器 ----------

async def cb_delete_server_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认删除", callback_data=f"{CB_DEL_SERVER_OK}{server.id}"
                ),
                InlineKeyboardButton(
                    "取消", callback_data=f"{CB_SERVER_PREFIX}{server.id}"
                ),
            ]
        ]
    )
    await query.edit_message_text(
        f"⚠️ 确认从 bot 中删除服务器「{server.name}」?\n"
        f"该操作只从 bot 移除登记记录(节点记录一并删除),不会触及远程服务器本身。",
        reply_markup=kb,
    )


async def cb_delete_server_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    ctx = get_ctx(context)
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        if server is None:
            await query.edit_message_text("服务器不存在。")
            return
        name = server.name
        await crud.delete_server(s, server_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="server.delete",
            result="success",
            detail=f"server={name}",
        )
        await s.commit()
    await query.edit_message_text(f"已从 bot 删除服务器「{name}」。")


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("server", cmd_server_list))
    application.add_handler(
        CallbackQueryHandler(cb_open_server, pattern=f"^{CB_SERVER_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_open_v2node_menu, pattern=f"^{CB_V2NODE_MENU}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_back_servers, pattern=f"^{CB_BACK_SERVERS}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_delete_server_confirm, pattern=f"^{CB_DEL_SERVER}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_delete_server_do, pattern=f"^{CB_DEL_SERVER_OK}\\d+$")
    )
