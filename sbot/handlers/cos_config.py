"""腾讯云 COS 配置:查看、交互录入、清除。

主菜单 → 🛠 远程配置 → ⚙️ COS 配置

录入流程: region → bucket(带 APPID 后缀)→ SecretId → SecretKey →
实测 COS 访问 → 入库。凭据消息收到即从聊天删除,SecretKey 加密存储。
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
from ..services.cos_api import COSAPIError, COSClient
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_COS_CLEAR,
    CB_COS_CLEAR_OK,
    CB_COS_DROP,
    CB_COS_EDIT,
    CB_COS_SAVE,
    CB_MENU_COS_CFG,
    NON_MENU_TEXT_FILTER,
    AppContext,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
)

log = logging.getLogger(__name__)


REGION, BUCKET, SECRETID, SECRETKEY, CONFIRM = range(5)

KEY = "coscfg"

_REGION_PATTERN = re.compile(r"^[a-z]+-[a-z0-9-]+$")
# bucket 必须带 APPID 后缀,如 mycfg-1250000000
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*-[0-9]{6,}$")


# ---------- 配置加载(供 remote_config 等复用) ----------

async def load_cos(ctx: AppContext) -> COSClient | None:
    """从数据库构建 COS 客户端;未配置返回 None。"""
    async with crud.session() as s:
        row = await crud.get_cos_config(s)
    if row is None:
        return None
    try:
        secret = ctx.crypto.decrypt(row.secret_key)
    except ValueError as exc:
        raise COSAPIError(f"SecretKey 解密失败: {exc}") from exc
    return COSClient(
        region=row.region,
        bucket=row.bucket,
        secret_id=row.secret_id,
        secret_key=secret,
    )


# ---------- 查看 / 清除 ----------

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    async with crud.session() as s:
        row = await crud.get_cos_config(s)

    rows = [[InlineKeyboardButton("✏️ 修改配置", callback_data=CB_COS_EDIT)]]
    if row is not None:
        text = (
            "⚙️ COS 配置\n"
            f"区域: {row.region}\n"
            f"Bucket: {row.bucket}\n"
            f"SecretId: {mask_secret(row.secret_id)}"
        )
        rows.append([InlineKeyboardButton(
            "🗑 清除配置", callback_data=CB_COS_CLEAR,
        )])
    else:
        text = "⚙️ COS 尚未配置,远程配置功能不可用。点下方按钮开始录入。"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("确认清除", callback_data=CB_COS_CLEAR_OK),
        InlineKeyboardButton("取消", callback_data=CB_MENU_COS_CFG),
    ]])
    await query.edit_message_text(
        "⚠️ 将删除 COS 配置,远程配置功能将不可用(不影响 COS 上的文件),"
        "确认清除?",
        reply_markup=kb,
    )


async def cb_clear_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    async with crud.session() as s:
        await crud.delete_cos_config(s)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="cos.config.clear",
            result="success",
            detail=None,
        )
        await s.commit()
    await query.edit_message_text("✅ 已清除 COS 配置。")


# ---------- 录入对话 ----------

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始录入腾讯云 COS 配置。任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
        "请输入 bucket 所在区域 id(如新加坡 ap-singapore、"
        "香港 ap-hongkong):",
        reply_markup=cancel_only_kb(),
    )
    return REGION


async def step_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip().lower()
    if not _REGION_PATTERN.match(value):
        await update.message.reply_text(
            "区域格式不合法(示例: ap-singapore),请重新输入:"
        )
        return REGION
    context.user_data[KEY]["region"] = value
    await update.message.reply_text(
        "请输入 bucket 名称(需带 APPID 后缀,如 mycfg-1250000000):"
    )
    return BUCKET


async def step_bucket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    if not _BUCKET_PATTERN.match(value):
        await update.message.reply_text(
            "bucket 名称不合法,需带 APPID 后缀(示例: mycfg-1250000000),"
            "请重新输入:"
        )
        return BUCKET
    context.user_data[KEY]["bucket"] = value
    await update.message.reply_text(
        "请输入 SecretId(建议使用子账号密钥,只授予该 bucket 读写权限)。\n"
        "(收到后 bot 会立即从聊天中删除该消息)"
    )
    return SECRETID


async def step_secretid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    with suppress(BadRequest):
        await update.message.delete()
    if not value:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="SecretId 不能为空,请重新输入:",
        )
        return SECRETID
    context.user_data[KEY]["secret_id"] = value
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "请输入 SecretKey。\n"
            "(收到后 bot 会立即从聊天中删除该消息并加密入库)"
        ),
    )
    return SECRETKEY


async def step_secretkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = (update.message.text or "").strip()
    with suppress(BadRequest):
        await update.message.delete()
    if not value:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="SecretKey 不能为空,请重新输入:",
        )
        return SECRETKEY
    ctx = get_ctx(context)
    context.user_data[KEY]["secret_key"] = ctx.crypto.encrypt(value)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="已收到,聊天中明文已删除。正在测试 COS 访问…",
    )
    return await _verify_and_save(update, context)


def _build_trial_client(ctx: AppContext, data: dict) -> COSClient:
    return COSClient(
        region=data["region"],
        bucket=data["bucket"],
        secret_id=data["secret_id"],
        secret_key=ctx.crypto.decrypt(data["secret_key"]),
    )


async def _verify_and_save(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    ctx = get_ctx(context)
    data = context.user_data[KEY]
    try:
        await _build_trial_client(ctx, data).check_access()
    except COSAPIError as exc:
        # 凭据可能只授予了对象读写而无 GetBucket 权限,允许强行保存
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("仍要保存", callback_data=CB_COS_SAVE),
            InlineKeyboardButton("放弃", callback_data=CB_COS_DROP),
        ]])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚠️ COS 访问测试失败:{exc}\n\n"
                "若确认凭据无误(例如子账号权限未含 GetBucket),"
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
    data = context.user_data[KEY]
    async with crud.session() as s:
        await crud.upsert_cos_config(
            s,
            region=data["region"],
            bucket=data["bucket"],
            secret_id=data["secret_id"],
            secret_key=data["secret_key"],
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="cos.config.set",
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
            f"✅ COS 配置已保存({note})\n"
            f"bucket: {data['bucket']} @ {data['region']}"
        ),
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消 COS 配置录入。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("setcos", cmd_edit),
            CallbackQueryHandler(cmd_edit, pattern=f"^{CB_COS_EDIT}$"),
        ],
        states={
            REGION: [MessageHandler(NON_MENU_TEXT_FILTER, step_region)],
            BUCKET: [MessageHandler(NON_MENU_TEXT_FILTER, step_bucket)],
            SECRETID: [MessageHandler(NON_MENU_TEXT_FILTER, step_secretid)],
            SECRETKEY: [MessageHandler(NON_MENU_TEXT_FILTER, step_secretkey)],
            CONFIRM: [
                CallbackQueryHandler(cb_force_save, pattern=f"^{CB_COS_SAVE}$"),
                CallbackQueryHandler(cb_drop, pattern=f"^{CB_COS_DROP}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="coscfg",
        persistent=False,
    )
    application.add_handler(conv)
    application.add_handler(
        CallbackQueryHandler(show_config, pattern=f"^{CB_MENU_COS_CFG}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_clear, pattern=f"^{CB_COS_CLEAR}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_clear_ok, pattern=f"^{CB_COS_CLEAR_OK}$")
    )
