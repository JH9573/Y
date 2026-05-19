"""v2node 卸载对话流程。

最高级别防护:用户必须**手动输入完全一致的服务器别名**,才会继续执行卸载。
卸载前自动备份远程 config.json 到 bot 本地 backups/。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node_uninstall import UninstallError, uninstall_v2node
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_SERVER_PREFIX,
    CB_UNINSTALL_START,
    NON_MENU_TEXT_FILTER,
    get_ctx,
)


log = logging.getLogger(__name__)


CONFIRM_NAME = 0

KEY = "uninstall"


async def cb_uninstall_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return ConversationHandler.END

    context.user_data[KEY] = {
        "server_id": server_id,
        "expected_name": server.name,
    }
    await query.edit_message_text(
        f"⚠️ 警告:即将卸载 {server.name} 上的 v2node。\n\n"
        f"将删除:\n"
        f"  • /usr/local/v2node/(程序目录)\n"
        f"  • /etc/v2node/(配置目录,包含全部节点)\n"
        f"  • /etc/systemd/system/v2node.service(服务单元)\n\n"
        f"操作前 bot 会先把远程 config.json 备份到本地 backups/。\n"
        f"该操作**不可恢复**。\n\n"
        f"如确认卸载,请**输入该服务器的别名**「{server.name}」(完全一致):\n"
        f"任意时刻发送 /cancel 中止。"
    )
    return CONFIRM_NAME


async def step_confirm_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    typed = (update.message.text or "").strip()
    data = context.user_data[KEY]
    if typed != data["expected_name"]:
        await update.message.reply_text("输入不匹配,已取消卸载。")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    server_id = data["server_id"]
    ctx = get_ctx(context)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await update.message.reply_text("服务器不存在。")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    progress_lines = [f"开始卸载 {server.name} 上的 v2node…"]
    msg = await update.message.reply_text("\n".join(progress_lines))

    failure_detail: str | None = None
    try:
        async for step in uninstall_v2node(ctx.ssh, server):
            progress_lines.append(f"• {step.step}: {step.detail}")
            try:
                await msg.edit_text("\n".join(progress_lines))
            except Exception:  # noqa: BLE001
                pass
        success = True
        progress_lines.append("\n✅ v2node 已卸载,服务器仍保留在列表中。")
    except UninstallError as exc:
        success = False
        failure_detail = str(exc)
        progress_lines.append(f"\n❌ 失败:{exc}")
    except SSHError as exc:
        success = False
        failure_detail = str(exc)
        progress_lines.append(f"\n❌ SSH 错误:{exc}")

    async with crud.session() as s:
        if success:
            await crud.clear_nodes(s, server_id)
            await crud.set_v2node_installed(s, server_id, False)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="v2node.uninstall",
            result="success" if success else "failed",
            detail=failure_detail or f"server={server.name}",
        )
        await s.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ 返回菜单", callback_data=f"{CB_SERVER_PREFIX}{server_id}")]]
    )
    try:
        await msg.edit_text("\n".join(progress_lines), reply_markup=kb)
    except Exception:  # noqa: BLE001
        await update.message.reply_text("\n".join(progress_lines), reply_markup=kb)
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消卸载。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_uninstall_start, pattern=f"^{CB_UNINSTALL_START}\\d+$"),
        ],
        states={
            CONFIRM_NAME: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_confirm_name),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="uninstall",
        persistent=False,
    )
    application.add_handler(conv)
