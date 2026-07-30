"""发布 / 编辑面板公告的对话流。

入口:
- 公告列表 [➕ 发布公告] -> 新建
- 公告详情 [✏️ 编辑]     -> 编辑

逐字段询问,编辑模式每步可点「保留」沿用旧值,可选字段可「跳过」/「清空」。
正文按纯文本录入,提交前转成 HTML(换行 -> <br/>);想手写 HTML 直接贴即可。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from ..db import crud
from ..services.v2board_api import V2BoardAPIError, validate_img_url
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_PANEL_NOTICE,
    CB_PANEL_NOTICE_ADD,
    CB_PANEL_NOTICE_EDIT,
    CB_PANEL_NOTICES,
    NON_MENU_TEXT_FILTER,
    get_ctx,
    html_to_text,
    text_to_html,
    truncate,
)


log = logging.getLogger(__name__)


TITLE, CONTENT, IMG_URL, TAGS, CONFIRM = range(5)

KEY = "pnlnotice"
KEEP_CB = "pnlnotice:keep"
SKIP_CB = "pnlnotice:skip"
CLEAR_CB = "pnlnotice:clear"
OK_CB = "pnlnotice:ok"
CANCEL_CB = "pnlnotice:cancel"

TITLE_LIMIT = 255
# Telegram 单条消息上限 4096,正文留出确认页其余字段的空间
CONTENT_LIMIT = 3000
TAG_LIMIT = 32
TAGS_MAX = 10

# 标签分隔符:半角逗号 / 全角逗号 / 顿号 / 空白
_TAG_SPLIT_PATTERN = re.compile(r"[,，、\s]+")


# ---------- 通用 helper ----------

async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """callback 上下文 edit 原消息;文本上下文回复新消息。"""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


def _is_edit(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data[KEY]["mode"] == "edit"


def _preview(value: Any, limit: int = 40) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "空"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_tags(tags: Any) -> str:
    if isinstance(tags, list) and tags:
        return ", ".join(str(t) for t in tags)
    return "-"


def _parse_tags(raw: str) -> list[str]:
    seen: list[str] = []
    for piece in _TAG_SPLIT_PATTERN.split(raw.strip()):
        piece = piece.strip()
        if piece and piece not in seen:
            seen.append(piece[:TAG_LIMIT])
    return seen[:TAGS_MAX]


def _optional_kb(
    context: ContextTypes.DEFAULT_TYPE, current: Any
) -> InlineKeyboardMarkup:
    """可选字段的按钮:新建给「跳过」,编辑给「保留」(有值时再给「清空」)。"""
    if not _is_edit(context):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("跳过", callback_data=SKIP_CB)]]
        )
    rows = [[InlineKeyboardButton(
        f"保留 ({_preview(current)})", callback_data=KEEP_CB
    )]]
    if current:
        rows.append([InlineKeyboardButton("🗑 清空", callback_data=CLEAR_CB)])
    return InlineKeyboardMarkup(rows)


# ---------- 入口 ----------

async def cb_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return ConversationHandler.END

    context.user_data[KEY] = {
        "mode": "add",
        "panel_id": panel_id,
        "panel_name": panel.name,
        "page": 1,
        "notice_id": None,
        "initial": {},
        "values": {},
    }
    await query.edit_message_text(
        f"在面板「{panel.name}」发布新公告。任意时刻可发送 /cancel 中止。\n\n"
        "请输入公告标题:"
    )
    return TITLE


async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, page_s, notice_id_s = payload.split(":", 2)
    panel_id, page, notice_id = int(panel_id_s), int(page_s), int(notice_id_s)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return ConversationHandler.END

    ctx = get_ctx(context)
    await query.edit_message_text(f"正在拉取公告 #{notice_id}…")
    try:
        item = await ctx.v2board.get_notice(panel, notice_id)
    except V2BoardAPIError as exc:
        await query.edit_message_text(f"❌ 拉取公告失败:{exc}")
        return ConversationHandler.END
    if item is None:
        await query.edit_message_text(f"公告 #{notice_id} 不存在或已被删除。")
        return ConversationHandler.END

    tags = item.get("tags")
    initial = {
        "title": str(item.get("title") or ""),
        "content": str(item.get("content") or ""),
        "img_url": str(item.get("img_url") or ""),
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
    }
    context.user_data[KEY] = {
        "mode": "edit",
        "panel_id": panel_id,
        "panel_name": panel.name,
        "page": page,
        "notice_id": notice_id,
        "initial": initial,
        "values": {},
    }
    await query.edit_message_text(
        f"编辑面板「{panel.name}」的公告 #{notice_id}。\n"
        "任意时刻可发送 /cancel 中止;每步可点「保留」沿用旧值。\n\n"
        f"请输入公告标题(当前: {initial['title']}):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                f"保留 ({_preview(initial['title'])})", callback_data=KEEP_CB
            )]]
        ),
    )
    return TITLE


# ---------- TITLE ----------

async def step_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        title = str(data["initial"].get("title") or "")
    else:
        title = (update.message.text or "").strip()
    if not title:
        await update.effective_message.reply_text("标题不能为空,请重新输入:")
        return TITLE
    if len(title) > TITLE_LIMIT:
        await update.effective_message.reply_text(
            f"标题过长(最多 {TITLE_LIMIT} 字符),请重新输入:"
        )
        return TITLE
    data["values"]["title"] = title
    return await _prompt_content(update, context)


# ---------- CONTENT ----------

async def _prompt_content(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    tip = (
        "请输入公告正文(纯文本即可,换行会自动转成 <br/>;"
        "想自己排版可直接贴 HTML):"
    )
    if _is_edit(context):
        current = html_to_text(str(data["initial"].get("content") or ""))
        await _reply(
            update,
            f"{tip}\n\n当前正文:\n{truncate(current or '(空)', 600)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    f"保留 ({_preview(current)})", callback_data=KEEP_CB
                )]]
            ),
        )
    else:
        await _reply(update, tip)
    return CONTENT


async def step_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        data["values"]["content"] = str(data["initial"].get("content") or "")
        return await _prompt_img_url(update, context)

    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("正文不能为空,请重新输入:")
        return CONTENT
    if len(raw) > CONTENT_LIMIT:
        await update.message.reply_text(
            f"正文过长(最多 {CONTENT_LIMIT} 字符),请精简后重新输入:"
        )
        return CONTENT
    data["values"]["content"] = text_to_html(raw)
    return await _prompt_img_url(update, context)


# ---------- IMG_URL ----------

async def _prompt_img_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    hint = "或点「保留」沿用旧图" if _is_edit(context) else "或点「跳过」不带图"
    await _reply(
        update,
        f"可选:请输入公告配图地址(http/https),{hint}:",
        reply_markup=_optional_kb(context, data["initial"].get("img_url")),
    )
    return IMG_URL


async def step_img_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == KEEP_CB:
            data["values"]["img_url"] = data["initial"].get("img_url") or None
        elif query.data == CLEAR_CB:
            data["values"]["img_url"] = None
        # 跳过:新建时不带该字段,编辑时沿用面板上的旧值
        return await _prompt_tags(update, context)

    try:
        img_url = validate_img_url(update.message.text or "")
    except V2BoardAPIError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return IMG_URL
    data["values"]["img_url"] = img_url
    return await _prompt_tags(update, context)


# ---------- TAGS ----------

async def _prompt_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    current = data["initial"].get("tags") or []
    hint = "或点「保留」沿用旧标签" if _is_edit(context) else "或点「跳过」不带标签"
    await _reply(
        update,
        f"可选:请输入公告标签(逗号或空格分隔,最多 {TAGS_MAX} 个),{hint}:",
        reply_markup=_optional_kb(context, _fmt_tags(current) if current else ""),
    )
    return TAGS


async def step_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == KEEP_CB:
            data["values"]["tags"] = list(data["initial"].get("tags") or [])
        elif query.data == CLEAR_CB:
            data["values"]["tags"] = []
        return await _prompt_confirm(update, context)

    tags = _parse_tags(update.message.text or "")
    if not tags:
        await update.message.reply_text(
            "没解析出有效标签,请重新输入或点上一条消息的「跳过」:"
        )
        return TAGS
    data["values"]["tags"] = tags
    return await _prompt_confirm(update, context)


# ---------- CONFIRM ----------

def _compose_payload(data: dict) -> dict[str, Any]:
    """合成 notice/save 的 payload。

    编辑模式以面板上的旧值打底,用户改过的字段覆盖;新建模式只带填过的字段。
    img_url 显式置 None 表示清空(v2board 侧规则是 nullable|url,不能传空串)。
    """
    v = data["values"]
    if data["mode"] == "edit":
        payload: dict[str, Any] = {
            "title": data["initial"].get("title") or "",
            "content": data["initial"].get("content") or "",
            "img_url": data["initial"].get("img_url") or None,
            "tags": list(data["initial"].get("tags") or []),
        }
    else:
        payload = {}
    payload["title"] = v["title"]
    payload["content"] = v["content"]
    if "img_url" in v:
        payload["img_url"] = v["img_url"] or None
    if "tags" in v:
        payload["tags"] = v["tags"]
    return payload


async def _prompt_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    payload = _compose_payload(data)
    action = "更新" if _is_edit(context) else "发布"
    lines = [
        f"请确认要{action}的公告:",
        "",
        f"标题: {payload['title']}",
        f"图片: {payload.get('img_url') or '-'}",
        f"标签: {_fmt_tags(payload.get('tags'))}",
        "",
        "正文:",
        html_to_text(str(payload.get("content") or "")) or "(空)",
    ]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 提交", callback_data=OK_CB),
        InlineKeyboardButton("❌ 取消", callback_data=CANCEL_CB),
    ]])
    await _reply(update, truncate("\n".join(lines)), reply_markup=kb)
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == CANCEL_CB:
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消。")
        return ConversationHandler.END

    data = context.user_data[KEY]
    panel_id = data["panel_id"]
    page = data["page"]
    notice_id = data["notice_id"]
    is_edit = notice_id is not None
    action = "更新" if is_edit else "发布"
    payload = _compose_payload(data)
    ctx = get_ctx(context)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板已被删除。")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    await query.edit_message_text(f"正在{action}公告…")
    try:
        await ctx.v2board.save_notice(panel, payload, notice_id=notice_id)
        ok, msg = True, f"公告已{action}"
    except V2BoardAPIError as exc:
        ok, msg = False, str(exc)

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.notice.edit" if is_edit else "panel.notice.add",
            result="success" if ok else "failed",
            detail=(
                f"panel_id={panel_id}, notice_id={notice_id}, "
                f"title={payload['title']!r}: {msg}"
            ),
        )
        await s.commit()

    if ok and is_edit:
        back = InlineKeyboardButton(
            "⬅ 返回公告",
            callback_data=f"{CB_PANEL_NOTICE}{panel_id}:{page}:{notice_id}",
        )
        tail = ""
    else:
        back = InlineKeyboardButton(
            "⬅ 返回列表", callback_data=f"{CB_PANEL_NOTICES}{panel_id}:1"
        )
        # 新公告的初始发布状态由面板决定,列表里的 ✅/❌ 才是准
        tail = (
            "\n列表里若显示 ❌ 未发布,进详情点「🔺 发布」即可对外可见。"
            if ok else ""
        )

    await query.edit_message_text(
        f"{'✅' if ok else '❌'} {msg}{tail}",
        reply_markup=InlineKeyboardMarkup([[back]]),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_add_entry, pattern=f"^{CB_PANEL_NOTICE_ADD}\\d+$"
            ),
            CallbackQueryHandler(
                cb_edit_entry,
                pattern=f"^{CB_PANEL_NOTICE_EDIT}\\d+:\\d+:\\d+$",
            ),
        ],
        states={
            TITLE: [
                CallbackQueryHandler(step_title, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_title),
            ],
            CONTENT: [
                CallbackQueryHandler(step_content, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_content),
            ],
            IMG_URL: [
                CallbackQueryHandler(
                    step_img_url,
                    pattern=f"^({SKIP_CB}|{KEEP_CB}|{CLEAR_CB})$",
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_img_url),
            ],
            TAGS: [
                CallbackQueryHandler(
                    step_tags,
                    pattern=f"^({SKIP_CB}|{KEEP_CB}|{CLEAR_CB})$",
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_tags),
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    step_confirm, pattern=f"^({OK_CB}|{CANCEL_CB})$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="pnlnotice",
        persistent=False,
    )
    application.add_handler(conv)
