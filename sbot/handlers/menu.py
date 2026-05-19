"""Reply keyboard 顶层菜单 routing。

非对话状态下的菜单按钮在这里捕获;对话内部的 state filter 已经
排除了菜单按钮文本,避免误吃。
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from . import logs, panel, server
from .common import (
    MENU_BACK_MAIN,
    MENU_CANCEL,
    MENU_LOGS,
    MENU_PANEL_GROUP,
    MENU_PANEL_LIST,
    MENU_SERVER_GROUP,
    MENU_SERVER_LIST,
    main_menu_kb,
    panel_menu_kb,
    server_menu_kb,
)


log = logging.getLogger(__name__)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "请选择功能:", reply_markup=main_menu_kb(),
    )


async def show_server_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "服务器管理:", reply_markup=server_menu_kb(),
    )


async def show_panel_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "面板管理:", reply_markup=panel_menu_kb(),
    )


async def cancel_outside_conv(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """非对话期间点了「取消」,回到主菜单作为提示。"""
    await update.effective_message.reply_text(
        "(没有正在进行的操作)", reply_markup=main_menu_kb(),
    )


def register(application, ctx) -> None:
    # 主菜单 / 子菜单切换
    application.add_handler(
        MessageHandler(filters.Text([MENU_SERVER_GROUP]), show_server_group)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_PANEL_GROUP]), show_panel_group)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_BACK_MAIN]), show_main)
    )
    # 子菜单 → 调用对应已有的 cmd_*。
    # 注意:「添加服务器」「添加面板」是 ConversationHandler 的 entry,
    # 不在这里注册以免重复消费。
    application.add_handler(
        MessageHandler(filters.Text([MENU_SERVER_LIST]), server.cmd_server_list)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_PANEL_LIST]), panel.cmd_panel_list)
    )
    application.add_handler(
        MessageHandler(filters.Text([MENU_LOGS]), logs.cmd_logs)
    )
    # 非对话状态下的「取消」
    application.add_handler(
        MessageHandler(filters.Text([MENU_CANCEL]), cancel_outside_conv)
    )
