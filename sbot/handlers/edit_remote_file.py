"""远程配置文件的编辑对话:修改单个字段 / 替换全文。

入口都在文件详情页(remote_config.cb_file_detail 渲染的按钮):
- ✏️ 修改字段: 键路径(点号分隔,支持嵌套与数组下标)→ 新值 → 上传
- 📄 替换全文: 粘贴完整 JSON → 校验 → 上传

值解析规则: 输入先按 JSON 解析(true/false/null/数字/带引号字符串/
对象/数组),解析失败则按普通字符串处理。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from telegram import Update
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
    CB_RCFG_REPLACE,
    CB_RCFG_SET,
    NON_MENU_TEXT_FILTER,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
    truncate,
)
from .cos_config import load_cos
from .remote_config import build_detail_text, detail_keyboard

log = logging.getLogger(__name__)


KEYPATH, VALUE, CONTENT = range(3)

KEY = "editrcfg"


def parse_value(raw: str) -> Any:
    """输入值解析:优先按 JSON,失败按字符串。"""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def set_json_path(data: Any, path: str, value: Any) -> None:
    """按点号路径写入值。中间缺失的层级按对象创建;数组用数字下标。"""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict):
            if not isinstance(node.get(part), (dict, list)):
                node[part] = {}
            node = node[part]
        else:
            raise ValueError(f"路径 {part} 处不是对象/数组,无法继续深入")
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    elif isinstance(node, dict):
        node[last] = value
    else:
        raise ValueError("目标位置不是对象/数组")


async def _load_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[int, str] | None:
    """从入口 callback_data 解析文件,返回 (file_id, path);无效返回 None。"""
    file_id = int(update.callback_query.data.split(":", 1)[1])
    async with crud.session() as s:
        file = await crud.get_remote_file(s, file_id)
    if file is None:
        await update.callback_query.edit_message_text("文件不存在,可能已被移除。")
        return None
    return file_id, file.path


# ---------- 修改字段 ----------

async def entry_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    target = await _load_target(update, context)
    if target is None:
        return ConversationHandler.END
    context.user_data[KEY] = {"file_id": target[0], "path": target[1]}
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"修改「{target[1]}」。\n"
            "请输入要修改的键路径(点号分隔,如 app.version 或 list.0.name):"
        ),
        reply_markup=cancel_only_kb(),
    )
    return KEYPATH


async def step_keypath(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keypath = (update.message.text or "").strip()
    if not keypath:
        await update.message.reply_text("键路径不能为空,请重新输入:")
        return KEYPATH
    context.user_data[KEY]["keypath"] = keypath
    await update.message.reply_text(
        "请输入新值(true/false/null/数字/对象/数组按 JSON 解析,"
        "其余按字符串):"
    )
    return VALUE


async def step_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    value = parse_value((update.message.text or "").strip())
    ctx = get_ctx(context)
    path = data["path"]
    try:
        cos = await load_cos(ctx)
        if cos is None:
            await update.message.reply_text("COS 尚未配置。")
            context.user_data.pop(KEY, None)
            return ConversationHandler.END
        content = await cos.get_object_text(path)
        try:
            obj = json.loads(content)
        except ValueError:
            await update.message.reply_text(
                "❌ 当前文件不是合法 JSON,无法按字段修改;"
                "请回到文件页用「替换全文」修复。",
                reply_markup=main_menu_kb(),
            )
            context.user_data.pop(KEY, None)
            return ConversationHandler.END
        try:
            set_json_path(obj, data["keypath"], value)
        except (ValueError, IndexError, KeyError) as exc:
            await update.message.reply_text(
                f"键路径应用失败:{exc}\n请重新输入键路径:"
            )
            return KEYPATH
        new_text = json.dumps(obj, ensure_ascii=False, indent=2)
        await cos.put_object_text(path, new_text)
    except COSAPIError as exc:
        await update.message.reply_text(
            truncate(f"❌ 更新失败:{exc}"), reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    await _log_edit(update, path, f"set {data['keypath']}")
    await update.message.reply_text(
        f"✅ 已更新 {data['keypath']}。", reply_markup=main_menu_kb(),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_detail_text(path, new_text),
        reply_markup=detail_keyboard(data["file_id"]),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


# ---------- 替换全文 ----------

async def entry_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    target = await _load_target(update, context)
    if target is None:
        return ConversationHandler.END
    context.user_data[KEY] = {"file_id": target[0], "path": target[1]}
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"替换「{target[1]}」的全部内容。\n请发送完整 JSON:",
        reply_markup=cancel_only_kb(),
    )
    return CONTENT


async def step_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        await update.message.reply_text(
            f"内容不是合法 JSON({exc}),请重新发送:"
        )
        return CONTENT
    data = context.user_data[KEY]
    path = data["path"]
    new_text = json.dumps(obj, ensure_ascii=False, indent=2)
    ctx = get_ctx(context)
    try:
        cos = await load_cos(ctx)
        if cos is None:
            await update.message.reply_text("COS 尚未配置。")
            context.user_data.pop(KEY, None)
            return ConversationHandler.END
        await cos.put_object_text(path, new_text)
    except COSAPIError as exc:
        await update.message.reply_text(
            truncate(f"❌ 上传失败:{exc}"), reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    await _log_edit(update, path, "replace")
    await update.message.reply_text("✅ 已替换全文。", reply_markup=main_menu_kb())
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_detail_text(path, new_text),
        reply_markup=detail_keyboard(data["file_id"]),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def _log_edit(update: Update, path: str, detail: str) -> None:
    try:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="remote.config.edit",
                result="success",
                detail=f"path={path}, {detail}",
            )
            await s.commit()
    except Exception:  # noqa: BLE001
        log.exception("写操作日志失败")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消编辑。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv_set = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_set, pattern=rf"^{CB_RCFG_SET}\d+$"),
        ],
        states={
            KEYPATH: [MessageHandler(NON_MENU_TEXT_FILTER, step_keypath)],
            VALUE: [MessageHandler(NON_MENU_TEXT_FILTER, step_value)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="rcfgset",
        persistent=False,
    )
    conv_replace = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                entry_replace, pattern=rf"^{CB_RCFG_REPLACE}\d+$",
            ),
        ],
        states={
            CONTENT: [MessageHandler(NON_MENU_TEXT_FILTER, step_content)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="rcfgreplace",
        persistent=False,
    )
    application.add_handler(conv_set)
    application.add_handler(conv_replace)
