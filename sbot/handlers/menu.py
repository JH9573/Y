"""Reply keyboard 顶层菜单 routing。

一级菜单(reply keyboard):服务器管理 / 面板管理 / 操作日志 / 取消
点前两个时在对话里弹出 inline 二级菜单,后续所有操作都走 inline。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import dns, logs, panel, server
from .common import (
    CB_MENU_DNS_ADD,
    CB_MENU_DNS_LIST,
    CB_MENU_PNL_LIST,
    CB_MENU_SRV_LIST,
    CB_MENU_PNL_ADD,
    CB_MENU_SRV_ADD,
    MENU_CANCEL,
    MENU_DNS_GROUP,
    MENU_LOGS,
    MENU_PANEL_GROUP,
    MENU_SERVER_GROUP,
    main_menu_kb,
)


log = logging.getLogger(__name__)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "请选择功能:", reply_markup=main_menu_kb(),
    )


async def show_server_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 服务器列表", callback_data=CB_MENU_SRV_LIST),
            InlineKeyboardButton("➕ 添加服务器", callback_data=CB_MENU_SRV_ADD),
        ],
    ])
    await update.effective_message.reply_text("服务器管理:", reply_markup=kb)


async def show_panel_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 面板列表", callback_data=CB_MENU_PNL_LIST),
            InlineKeyboardButton("➕ 添加面板", callback_data=CB_MENU_PNL_ADD),
        ],
    ])
    await update.effective_message.reply_text("面板管理:", reply_markup=kb)


async def show_dns_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 账户列表", callback_data=CB_MENU_DNS_LIST),
            InlineKeyboardButton("➕ 添加账户", callback_data=CB_MENU_DNS_ADD),
        ],
    ])
    await update.effective_message.reply_text("DNS 管理:", reply_markup=kb)


async def cb_menu_server_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    await server.cmd_server_list(update, context)


async def cb_menu_panel_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    await panel.cmd_panel_list(update, context)


async def cb_menu_dns_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    await dns.cmd_dns_list(update, context)


async def cancel_outside_conv(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """非对话期间点了「取消」,回到主菜单作为提示。"""
    await update.effective_message.reply_text(
        "(没有正在进行的操作)", reply_markup=main_menu_kb(),
    )


def register(application, ctx) -> None:
    # 一级菜单按钮(reply keyboard 文本)
    application.add_handler(
        MessageHandler(filters.Text([MENU_SERVER_GROUP]), show_server_group)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_PANEL_GROUP]), show_panel_group)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_DNS_GROUP]), show_dns_group)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_LOGS]), logs.cmd_logs)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_CANCEL]), cancel_outside_conv)
    )
    # 二级菜单 inline 按钮(添加按钮的 callback 由 add_server / add_panel 注册)
    application.add_handler(
        CallbackQueryHandler(cb_menu_server_list, pattern=f"^{CB_MENU_SRV_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_menu_panel_list, pattern=f"^{CB_MENU_PNL_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_menu_dns_list, pattern=f"^{CB_MENU_DNS_LIST}$")
    )
