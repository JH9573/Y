"""添加远程配置文件对话流程。

主菜单 → 🛠 远程配置 → ➕ 添加文件

流程: 输入 COS 上的 object 路径 → 拉取校验(存在且为合法 JSON)→ 入库。
文件不存在时可选择创建(写入空对象 {})。
"""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from ..db import crud
from ..services.cos_api import COSAPIError
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_MENU_RCFG_ADD,
    CB_RCFG_ADD_DROP,
    CB_RCFG_ADD_FORCE,
    CB_RCFG_FILE,
    NON_MENU_TEXT_FILTER,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
    truncate,
)
from .cos_config import load_cos


log = logging.getLogger(__name__)


PATH, CONFIRM = range(2)

KEY = "addrcfg"


def _normalize_path(raw: str) -> str | None:
    path = raw.strip().lstrip("/")
    if not path or ".." in path or path.endswith("/"):
        return None
    if not path.lower().endswith(".json"):
        return None
    return path


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    ctx = get_ctx(context)
    try:
        cos = await load_cos(ctx)
    except COSAPIError as exc:
        await update.effective_message.reply_text(f"⚠️ COS 配置异常:{exc}")
        return ConversationHandler.END
    if cos is None:
        await update.effective_message.reply_text(
            "COS 尚未配置,请先在「远程配置 → ⚙️ COS 配置」中录入。"
        )
        return ConversationHandler.END
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加远程配置文件。任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
        "请输入文件在 COS 上的路径(.json 结尾,如 config/app.json):",
        reply_markup=cancel_only_kb(),
    )
    return PATH


async def step_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = _normalize_path(update.message.text or "")
    if path is None:
        await update.message.reply_text(
            "路径不合法(需以 .json 结尾,不含 ..),请重新输入:"
        )
        return PATH
    async with crud.session() as s:
        existing = await crud.get_remote_file_by_path(s, path)
    if existing is not None:
        await update.message.reply_text(f"「{path}」已在管理列表中,请换一个:")
        return PATH
    context.user_data[KEY]["path"] = path
    await update.message.reply_text("正在拉取文件校验…")

    ctx = get_ctx(context)
    try:
        cos = await load_cos(ctx)
        if cos is None:
            await update.message.reply_text("COS 尚未配置。")
            context.user_data.pop(KEY, None)
            return ConversationHandler.END
        content = await cos.get_object_text(path)
    except COSAPIError as exc:
        if exc.status == 404:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "创建新文件", callback_data=CB_RCFG_ADD_FORCE,
                ),
                InlineKeyboardButton("放弃", callback_data=CB_RCFG_ADD_DROP),
            ]])
            await update.message.reply_text(
                f"COS 上不存在「{path}」。可以创建它(内容初始化为 {{}}),"
                "或放弃添加。",
                reply_markup=kb,
            )
            return CONFIRM
        await update.message.reply_text(truncate(f"❌ 拉取失败:{exc}"))
        return PATH

    try:
        json.loads(content)
    except ValueError:
        await update.message.reply_text(
            f"⚠️ 「{path}」存在但内容不是合法 JSON,仍已登记;"
            "可进入文件用「替换全文」修复。"
        )
    return await _save(update, context)


async def cb_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx = get_ctx(context)
    path = context.user_data[KEY]["path"]
    try:
        cos = await load_cos(ctx)
        await cos.put_object_text(path, "{}")
    except COSAPIError as exc:
        await query.edit_message_text(truncate(f"❌ 创建文件失败:{exc}"))
        context.user_data.pop(KEY, None)
        return ConversationHandler.END
    await query.edit_message_text(f"已在 COS 创建「{path}」。")
    return await _save(update, context)


async def cb_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(KEY, None)
    await query.edit_message_text("已放弃添加。")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="(回到主菜单)",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def _save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = context.user_data[KEY]["path"]
    async with crud.session() as s:
        file = await crud.create_remote_file(s, path=path)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="remote.file.add",
            result="success",
            detail=f"path={path}",
        )
        await s.commit()
        file_id = file.id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "查看文件", callback_data=f"{CB_RCFG_FILE}{file_id}",
    )]])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ 已添加远程配置文件「{path}」。",
        reply_markup=kb,
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="(回到主菜单)",
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消添加远程配置文件。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("addrcfg", cmd_add),
            CallbackQueryHandler(cmd_add, pattern=f"^{CB_MENU_RCFG_ADD}$"),
        ],
        states={
            PATH: [MessageHandler(NON_MENU_TEXT_FILTER, step_path)],
            CONFIRM: [
                CallbackQueryHandler(
                    cb_create, pattern=f"^{CB_RCFG_ADD_FORCE}$",
                ),
                CallbackQueryHandler(cb_drop, pattern=f"^{CB_RCFG_ADD_DROP}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="addrcfg",
        persistent=False,
    )
    application.add_handler(conv)
