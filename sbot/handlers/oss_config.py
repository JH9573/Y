"""OSS 分发配置:查看、交互录入、清除。

主菜单 → 📦 安装包分发 → ⚙️ OSS 配置

配置优先级: 数据库(bot 内录入)> .env 的 OSS_*。
录入流程: region → bucket → AccessKey ID → AccessKey Secret →
自定义下载域名(可跳过)→ 实测 OSS 访问 → 入库。
凭据消息收到即从聊天删除,Secret 加密存储。
"""
from __future__ import annotations

import logging
import re
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

from ..core.crypto import mask_secret
from ..db import crud
from ..services.oss_api import OSSAPIError, OSSClient
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_MENU_OSS_CFG,
    CB_OSS_CLEAR,
    CB_OSS_CLEAR_OK,
    CB_OSS_DROP,
    CB_OSS_EDIT,
    CB_OSS_SAVE,
    NON_MENU_TEXT_FILTER,
    AppContext,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
)

log = logging.getLogger(__name__)


REGION, BUCKET, AKID, AKSECRET, BASEURL, CONFIRM = range(6)

KEY = "osscfg"

_REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z0-9-]+$")
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_URL_PATTERN = re.compile(r"^https?://[^\s/]+$")

SKIP_WORDS = ("跳过", "skip", "无")


# ---------- 配置加载(供 release.py 复用) ----------

async def load_oss(ctx: AppContext) -> tuple[OSSClient, str] | None:
    """按优先级构建 OSS 客户端,返回 (client, 路径前缀);未配置返回 None。"""
    async with crud.session() as s:
        row = await crud.get_oss_config(s)
    if row is not None:
        try:
            secret = ctx.crypto.decrypt(row.access_key_secret)
        except ValueError as exc:
            raise OSSAPIError(f"AccessKey Secret 解密失败: {exc}") from exc
        client = OSSClient(
            region=row.region,
            bucket=row.bucket,
            access_key_id=row.access_key_id,
            access_key_secret=secret,
            public_base_url=row.public_base_url,
        )
        return client, row.prefix
    cfg = ctx.config
    if cfg.oss_configured:
        client = OSSClient(
            region=cfg.oss_region,
            bucket=cfg.oss_bucket,
            access_key_id=cfg.oss_access_key_id,
            access_key_secret=cfg.oss_access_key_secret,
            endpoint=cfg.oss_endpoint,
            public_base_url=cfg.oss_public_base_url,
        )
        return client, cfg.oss_prefix
    return None


# ---------- 查看 / 清除 ----------

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ctx = get_ctx(context)
    async with crud.session() as s:
        row = await crud.get_oss_config(s)

    rows = [[InlineKeyboardButton("✏️ 修改配置", callback_data=CB_OSS_EDIT)]]
    if row is not None:
        source = "数据库(bot 内录入)"
        text = (
            "⚙️ OSS 配置\n"
            f"来源: {source}\n"
            f"区域: {row.region}\n"
            f"Bucket: {row.bucket}\n"
            f"AccessKey ID: {mask_secret(row.access_key_id)}\n"
            f"下载域名: {row.public_base_url or '(默认 bucket 域名)'}\n"
            f"路径前缀: {row.prefix}/"
        )
        rows.append([InlineKeyboardButton(
            "🗑 清除配置", callback_data=CB_OSS_CLEAR,
        )])
    elif ctx.config.oss_configured:
        cfg = ctx.config
        text = (
            "⚙️ OSS 配置\n"
            "来源: .env 环境变量\n"
            f"区域: {cfg.oss_region}\n"
            f"Bucket: {cfg.oss_bucket}\n"
            f"AccessKey ID: {mask_secret(cfg.oss_access_key_id)}\n"
            f"下载域名: {cfg.oss_public_base_url or '(默认 bucket 域名)'}\n"
            f"路径前缀: {cfg.oss_prefix}/\n\n"
            "在 bot 内修改后将以 bot 配置为准。"
        )
    else:
        text = "⚙️ OSS 尚未配置,发布功能不可用。点下方按钮开始录入。"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("确认清除", callback_data=CB_OSS_CLEAR_OK),
        InlineKeyboardButton("取消", callback_data=CB_MENU_OSS_CFG),
    ]])
    await query.edit_message_text(
        "⚠️ 将删除 bot 内录入的 OSS 配置(若 .env 配置了 OSS_* 则回退使用),"
        "确认清除?",
        reply_markup=kb,
    )


async def cb_clear_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    async with crud.session() as s:
        await crud.delete_oss_config(s)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="oss.config.clear",
            result="success",
            detail=None,
        )
        await s.commit()
    ctx = get_ctx(context)
    fallback = (
        "已回退到 .env 配置。" if ctx.config.oss_configured
        else "当前无可用 OSS 配置,发布功能不可用。"
    )
    await query.edit_message_text(f"✅ 已清除 bot 内 OSS 配置。{fallback}")


# ---------- 录入对话 ----------

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始录入 OSS 配置。任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
        "请输入 bucket 所在区域 id(如新加坡 ap-southeast-1、"
        "香港 cn-hongkong):",
        reply_markup=cancel_only_kb(),
    )
    return REGION


async def step_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip().lower().removeprefix("oss-")
    if not _REGION_PATTERN.match(value):
        await update.message.reply_text(
            "区域格式不合法(示例: ap-southeast-1),请重新输入:"
        )
        return REGION
    context.user_data[KEY]["region"] = value
    await update.message.reply_text("请输入 bucket 名称:")
    return BUCKET


async def step_bucket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    if not _BUCKET_PATTERN.match(value):
        await update.message.reply_text(
            "bucket 名称不合法(3-63 位小写字母/数字/'-'),请重新输入:"
        )
        return BUCKET
    context.user_data[KEY]["bucket"] = value
    await update.message.reply_text(
        "请输入 AccessKey ID(建议使用 RAM 子账号,只授予该 bucket 读写权限)。\n"
        "(收到后 bot 会立即从聊天中删除该消息)"
    )
    return AKID


async def step_akid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    with suppress(BadRequest):
        await update.message.delete()
    if not value:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="AccessKey ID 不能为空,请重新输入:",
        )
        return AKID
    context.user_data[KEY]["access_key_id"] = value
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "请输入 AccessKey Secret。\n"
            "(收到后 bot 会立即从聊天中删除该消息并加密入库)"
        ),
    )
    return AKSECRET


async def step_aksecret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    with suppress(BadRequest):
        await update.message.delete()
    if not value:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="AccessKey Secret 不能为空,请重新输入:",
        )
        return AKSECRET
    ctx = get_ctx(context)
    context.user_data[KEY]["access_key_secret"] = ctx.crypto.encrypt(value)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "已收到,聊天中明文已删除。\n\n"
            "如有自定义下载域名(CDN,如 https://dl.example.com)请输入,"
            "没有请发「跳过」:"
        ),
    )
    return BASEURL


async def step_baseurl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    if value.lower() in SKIP_WORDS:
        context.user_data[KEY]["public_base_url"] = None
    else:
        if not _URL_PATTERN.match(value.rstrip("/")):
            await update.message.reply_text(
                "域名格式不合法(示例: https://dl.example.com),"
                "请重新输入或发「跳过」:"
            )
            return BASEURL
        context.user_data[KEY]["public_base_url"] = value.rstrip("/")
    await update.message.reply_text("正在测试 OSS 访问…")
    return await _verify_and_save(update, context)


def _build_trial_client(ctx: AppContext, data: dict) -> OSSClient:
    return OSSClient(
        region=data["region"],
        bucket=data["bucket"],
        access_key_id=data["access_key_id"],
        access_key_secret=ctx.crypto.decrypt(data["access_key_secret"]),
        public_base_url=data["public_base_url"],
    )


async def _verify_and_save(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    ctx = get_ctx(context)
    data = context.user_data[KEY]
    try:
        await _build_trial_client(ctx, data).check_access()
    except OSSAPIError as exc:
        # 凭据可能只授予了 PutObject 而无 ListObjects 权限,允许强行保存
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("仍要保存", callback_data=CB_OSS_SAVE),
            InlineKeyboardButton("放弃", callback_data=CB_OSS_DROP),
        ]])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚠️ OSS 访问测试失败:{exc}\n\n"
                "若确认凭据无误(例如 RAM 权限未含 ListObjects),"
                "可选择仍要保存。"
            ),
            reply_markup=kb,
        )
        return CONFIRM
    return await _save(update, context, verified=True)


async def cb_force_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _save(update, context, verified=False)


async def cb_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(KEY, None)
    await query.edit_message_text("已放弃,配置未保存。")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="(回到主菜单)",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def _save(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, verified: bool
) -> int:
    ctx = get_ctx(context)
    data = context.user_data[KEY]
    async with crud.session() as s:
        await crud.upsert_oss_config(
            s,
            region=data["region"],
            bucket=data["bucket"],
            access_key_id=data["access_key_id"],
            access_key_secret=data["access_key_secret"],
            public_base_url=data["public_base_url"],
            prefix=ctx.config.oss_prefix,
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="oss.config.set",
            result="success",
            detail=(
                f"region={data['region']}, bucket={data['bucket']}, "
                f"verified={verified}"
            ),
        )
        await s.commit()
    note = "访问测试通过。" if verified else "未通过访问测试,已按要求强行保存。"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ OSS 配置已保存({note})\n"
            f"bucket: {data['bucket']} @ {data['region']}"
        ),
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消 OSS 配置录入。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("setoss", cmd_edit),
            CallbackQueryHandler(cmd_edit, pattern=f"^{CB_OSS_EDIT}$"),
        ],
        states={
            REGION: [MessageHandler(NON_MENU_TEXT_FILTER, step_region)],
            BUCKET: [MessageHandler(NON_MENU_TEXT_FILTER, step_bucket)],
            AKID: [MessageHandler(NON_MENU_TEXT_FILTER, step_akid)],
            AKSECRET: [MessageHandler(NON_MENU_TEXT_FILTER, step_aksecret)],
            BASEURL: [MessageHandler(NON_MENU_TEXT_FILTER, step_baseurl)],
            CONFIRM: [
                CallbackQueryHandler(cb_force_save, pattern=f"^{CB_OSS_SAVE}$"),
                CallbackQueryHandler(cb_drop, pattern=f"^{CB_OSS_DROP}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="osscfg",
        persistent=False,
    )
    application.add_handler(conv)
    application.add_handler(
        CallbackQueryHandler(show_config, pattern=f"^{CB_MENU_OSS_CFG}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_clear, pattern=f"^{CB_OSS_CLEAR}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_clear_ok, pattern=f"^{CB_OSS_CLEAR_OK}$")
    )
