"""面板列表 / 详情菜单 / 删除面板。"""
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
    CB_BACK_PANELS,
    CB_DEL_PANEL,
    CB_DEL_PANEL_OK,
    CB_EDIT_PANEL,
    CB_PANEL_NODES,
    CB_PANEL_PREFIX,
    CB_SYNC_PANEL_CREDS,
    humanize_age,
)


log = logging.getLogger(__name__)


async def cmd_panel_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with crud.session() as s:
        panels = await crud.list_panels(s)

    if not panels:
        await update.effective_message.reply_text(
            "当前没有已登记的面板,使用 /addpanel 添加一个。"
        )
        return

    buttons = [
        [InlineKeyboardButton(p.name, callback_data=f"{CB_PANEL_PREFIX}{p.id}")]
        for p in panels
    ]
    await update.effective_message.reply_text(
        "选择一个面板:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_open_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    await _render_panel_menu(update, context, panel_id)


async def cb_back_panels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    async with crud.session() as s:
        panels = await crud.list_panels(s)
    if not panels:
        await query.edit_message_text("当前没有已登记的面板。")
        return
    buttons = [
        [InlineKeyboardButton(p.name, callback_data=f"{CB_PANEL_PREFIX}{p.id}")]
        for p in panels
    ]
    await query.edit_message_text(
        "选择一个面板:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _render_panel_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    panel_id: int,
) -> None:
    query = update.callback_query
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        if panel is None:
            await query.edit_message_text("面板不存在(可能已被删除)。")
            return
        nodes = await crud.list_panel_nodes(s, panel_id)
        latest_sync = await crud.latest_node_sync_at(s, panel_id)

    auth_state = "已登录" if panel.auth_data else "未登录"
    creds_state = "已记录" if (panel.api_host and panel.api_key) else "未记录"
    header = (
        f"面板 {panel.name}\n"
        f"地址: {panel.base_url}\n"
        f"后台路径: {panel.secure_path}\n"
        f"管理员: {panel.email}\n"
        f"状态: {auth_state}\n"
        f"通信凭据: {creds_state}"
        + (f" (api_host={panel.api_host})" if panel.api_host else "")
        + "\n"
        f"已缓存 v2node: {len(nodes)} 个\n"
        f"最近同步: {humanize_age(latest_sync)}"
    )

    kb = [
        [
            InlineKeyboardButton(
                "📋 节点列表", callback_data=f"{CB_PANEL_NODES}{panel.id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "✏️ 编辑面板", callback_data=f"{CB_EDIT_PANEL}{panel.id}"
            ),
            InlineKeyboardButton(
                "🔄 同步通信凭据",
                callback_data=f"{CB_SYNC_PANEL_CREDS}{panel.id}",
            ),
        ],
        [InlineKeyboardButton("🗑 删除面板", callback_data=f"{CB_DEL_PANEL}{panel.id}")],
        [InlineKeyboardButton("⬅ 返回列表", callback_data=CB_BACK_PANELS)],
    ]
    await query.edit_message_text(header, reply_markup=InlineKeyboardMarkup(kb))


# ---------- 删除面板 ----------

async def cb_delete_panel_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认删除", callback_data=f"{CB_DEL_PANEL_OK}{panel.id}"
                ),
                InlineKeyboardButton(
                    "取消", callback_data=f"{CB_PANEL_PREFIX}{panel.id}"
                ),
            ]
        ]
    )
    await query.edit_message_text(
        f"⚠️ 确认从 bot 中删除面板「{panel.name}」?\n"
        f"该操作只从 bot 移除登记,不会触及面板本身的数据。",
        reply_markup=kb,
    )


async def cb_delete_panel_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        if panel is None:
            await query.edit_message_text("面板不存在。")
            return
        name = panel.name
        await crud.delete_panel(s, panel_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.delete",
            result="success",
            detail=f"panel_id={panel_id}, name={name}",
        )
        await s.commit()
    await query.edit_message_text(f"已从 bot 删除面板「{name}」。")


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("panel", cmd_panel_list))
    application.add_handler(
        CallbackQueryHandler(cb_open_panel, pattern=f"^{CB_PANEL_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_back_panels, pattern=f"^{CB_BACK_PANELS}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_delete_panel_confirm, pattern=f"^{CB_DEL_PANEL}\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_delete_panel_do, pattern=f"^{CB_DEL_PANEL_OK}\\d+$"
        )
    )
