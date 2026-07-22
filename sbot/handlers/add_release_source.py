"""添加安装包分发仓库对话流程(GitHub 私有仓库)。

/addrepo 或主菜单 → 安装包分发 → ➕ 添加仓库

流程: 仓库(owner/name) → PAT token → 调 GitHub API 校验可读 → 入库。
"""
from __future__ import annotations

import logging
from contextlib import suppress

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from ..db import crud
from ..db.models import ReleaseSource
from ..services.github_release import GitHubAPIError, validate_repo
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_MENU_REL_ADD,
    CB_REL_SRC,
    NON_MENU_TEXT_FILTER,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
)


log = logging.getLogger(__name__)


REPO, TOKEN = range(2)

KEY = "addrepo"


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加分发仓库(GitHub)。任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
        "请输入仓库(格式 owner/name,例如 JH9573/myapp):",
        reply_markup=cancel_only_kb(),
    )
    return REPO


async def step_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    try:
        repo = validate_repo(raw)
    except GitHubAPIError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return REPO
    async with crud.session() as s:
        existing = await crud.get_release_source_by_repo(s, repo)
    if existing is not None:
        await update.message.reply_text(f"仓库「{repo}」已登记,请换一个:")
        return REPO
    context.user_data[KEY]["repo"] = repo
    await update.message.reply_text(
        "请输入 GitHub Token(建议 fine-grained PAT,只授予该仓库的 "
        "Contents:Read 权限)。\n"
        "获取入口: github.com → Settings → Developer settings → "
        "Fine-grained tokens。\n"
        "(收到后 bot 会立即从聊天中删除该消息并加密入库)"
    )
    return TOKEN


async def step_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    # 立刻删除聊天中明文 token
    with suppress(BadRequest):
        await update.message.delete()
    if not raw:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Token 不能为空,请重新输入:",
        )
        return TOKEN
    ctx = get_ctx(context)
    context.user_data[KEY]["token"] = ctx.crypto.encrypt(raw)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="已收到 Token,聊天中明文已删除。正在校验仓库访问权限…",
    )
    return await _finalize(update, context)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    chat_id = update.effective_chat.id

    trial = ReleaseSource(repo=data["repo"], token=data["token"])
    try:
        info = await ctx.github.verify_repo(trial)
        releases = await ctx.github.list_releases(trial, limit=1)
    except GitHubAPIError as exc:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ 校验失败,仓库未登记:{exc}",
            reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    async with crud.session() as s:
        source = await crud.create_release_source(
            s, repo=data["repo"], token=data["token"],
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="release.source.add",
            result="success",
            detail=f"source_id={source.id}, repo={source.repo}",
        )
        await s.commit()
        source_id = source.id
        source_repo = source.repo

    visibility = "私有" if info.get("private") else "公开"
    latest = f"最新 Release: {releases[0]['tag']}" if releases else "暂无 Release"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "进入仓库", callback_data=f"{CB_REL_SRC}{source_id}",
    )]])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ 分发仓库「{source_repo}」已添加({visibility}仓库,{latest})。\n"
            f"点下方按钮进入仓库选择版本发布。"
        ),
        reply_markup=kb,
    )
    await context.bot.send_message(
        chat_id=chat_id, text="(回到主菜单)", reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消添加分发仓库。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("addrepo", cmd_add),
            CallbackQueryHandler(cmd_add, pattern=f"^{CB_MENU_REL_ADD}$"),
        ],
        states={
            REPO: [MessageHandler(NON_MENU_TEXT_FILTER, step_repo)],
            TOKEN: [MessageHandler(NON_MENU_TEXT_FILTER, step_token)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="addrepo",
        persistent=False,
    )
    application.add_handler(conv)
