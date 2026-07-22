"""远程配置:管理存放在腾讯云 COS 上的 JSON 配置文件。

主菜单 → 🛠 远程配置 → 文件列表 → 选文件 → 查看内容 /
修改单个字段 / 替换全文(后两者的对话在 edit_remote_file.py)。

文件的「移除」只是移出 bot 管理列表,不会删除 COS 上的对象。
"""
from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ..db import crud
from ..services.cos_api import COSAPIError
from .common import (
    CB_BACK_RCFG_LIST,
    CB_MENU_COS_CFG,
    CB_MENU_RCFG_ADD,
    CB_RCFG_DEL,
    CB_RCFG_DEL_OK,
    CB_RCFG_FILE,
    CB_RCFG_REPLACE,
    CB_RCFG_SET,
    get_ctx,
    truncate,
)
from .cos_config import load_cos


log = logging.getLogger(__name__)


def format_json(text: str) -> tuple[str, bool]:
    """尝试格式化 JSON,返回 (展示文本, 是否合法 JSON)。"""
    try:
        data = json.loads(text)
    except ValueError:
        return text, False
    return json.dumps(data, ensure_ascii=False, indent=2), True


def detail_keyboard(file_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✏️ 修改字段", callback_data=f"{CB_RCFG_SET}{file_id}",
            ),
            InlineKeyboardButton(
                "📄 替换全文", callback_data=f"{CB_RCFG_REPLACE}{file_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 刷新", callback_data=f"{CB_RCFG_FILE}{file_id}",
            ),
            InlineKeyboardButton(
                "🗑 移除", callback_data=f"{CB_RCFG_DEL}{file_id}",
            ),
        ],
        [InlineKeyboardButton("« 返回列表", callback_data=CB_BACK_RCFG_LIST)],
    ])


def build_detail_text(path: str, content: str) -> str:
    pretty, valid = format_json(content)
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    head = f"🛠 {path}\n拉取于 {now} (UTC)"
    if not valid:
        head += "\n⚠️ 当前内容不是合法 JSON,「修改字段」不可用,请用「替换全文」修复"
    return truncate(f"{head}\n————————————\n{pretty}")


async def _reply_or_edit(update: Update, text: str, reply_markup=None) -> None:
    if update.callback_query:
        with suppress(BadRequest):  # 刷新时内容可能毫无变化
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup,
            )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=reply_markup,
        )


# ---------- 文件列表 ----------

async def show_file_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    async with crud.session() as s:
        files = await crud.list_remote_files(s)
        cos_row = await crud.get_cos_config(s)
    if cos_row is None:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⚙️ 去配置 COS", callback_data=CB_MENU_COS_CFG,
        )]])
        await _reply_or_edit(
            update, "COS 尚未配置,请先完成配置。", reply_markup=kb,
        )
        return
    rows = [
        [InlineKeyboardButton(f.path, callback_data=f"{CB_RCFG_FILE}{f.id}")]
        for f in files
    ]
    rows.append([InlineKeyboardButton(
        "➕ 添加文件", callback_data=CB_MENU_RCFG_ADD,
    )])
    text = "远程配置文件列表:" if files else "还没有登记远程配置文件。"
    await _reply_or_edit(update, text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_back_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await show_file_list(update, context)


# ---------- 文件详情 ----------

async def cb_file_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    file_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        file = await crud.get_remote_file(s, file_id)
    if file is None:
        await query.edit_message_text("文件不存在,可能已被移除。")
        return
    ctx = get_ctx(context)
    try:
        cos = await load_cos(ctx)
        if cos is None:
            await query.edit_message_text("COS 尚未配置,请先完成配置。")
            return
        content = await cos.get_object_text(file.path)
    except COSAPIError as exc:
        await _reply_or_edit(
            update,
            truncate(f"❌ 拉取 {file.path} 失败:{exc}"),
            reply_markup=detail_keyboard(file_id),
        )
        return
    await _reply_or_edit(
        update,
        build_detail_text(file.path, content),
        reply_markup=detail_keyboard(file_id),
    )


# ---------- 移除文件 ----------

async def cb_file_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    file_id = int(query.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "确认移除", callback_data=f"{CB_RCFG_DEL_OK}{file_id}",
        ),
        InlineKeyboardButton("取消", callback_data=f"{CB_RCFG_FILE}{file_id}"),
    ]])
    await query.edit_message_text(
        "将把该文件移出管理列表(COS 上的文件本身不受影响),确认?",
        reply_markup=kb,
    )


async def cb_file_del_ok(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    file_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        file = await crud.get_remote_file(s, file_id)
        path = file.path if file else f"id={file_id}"
        await crud.delete_remote_file(s, file_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="remote.file.remove",
            result="success",
            detail=f"path={path}",
        )
        await s.commit()
    await query.edit_message_text(f"✅ 已移除「{path}」。")


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("rcfg", show_file_list))
    application.add_handler(
        CallbackQueryHandler(cb_back_list, pattern=f"^{CB_BACK_RCFG_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_file_detail, pattern=rf"^{CB_RCFG_FILE}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_file_del, pattern=rf"^{CB_RCFG_DEL}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_file_del_ok, pattern=rf"^{CB_RCFG_DEL_OK}\d+$")
    )
