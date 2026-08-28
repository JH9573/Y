"""腾讯云 COS 配置:存储桶列表、交互录入、切换与删除。

主菜单 → 🛠 远程配置 → ⚙️ COS 配置

可录入多个存储桶,✅ 标记的那个是远程配置读写实际使用的(「当前使用」);
其余为备用,在详情页可一键切换。(region, bucket) 相同视为同一配置,
重复录入即更新凭据。
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
    CB_COS_ACTIVE,
    CB_COS_DEL,
    CB_COS_DEL_OK,
    CB_COS_DETAIL,
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
    """从数据库构建 COS 客户端(多桶时用当前使用的那个);未配置返回 None。"""
    async with crud.session() as s:
        row = await crud.get_active_cos_config(s)
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


# ---------- 存储桶列表 / 详情 / 切换 / 删除 ----------

async def _render_bucket_list(query) -> None:
    async with crud.session() as s:
        configs = await crud.list_cos_configs(s)

    add_row = [InlineKeyboardButton("➕ 添加存储桶", callback_data=CB_COS_EDIT)]
    if configs:
        rows = [
            [InlineKeyboardButton(
                f"{'✅ ' if c.is_active else ''}{c.bucket} @ {c.region}",
                callback_data=f"{CB_COS_DETAIL}{c.id}",
            )]
            for c in configs
        ]
        rows.append(add_row)
        text = (
            f"⚙️ COS 配置(共 {len(configs)} 个存储桶)\n"
            "✅ 为远程配置当前使用的存储桶;点击查看详情、切换或删除。"
        )
    else:
        rows = [add_row]
        text = "⚙️ COS 尚未配置,远程配置功能不可用。点下方按钮添加存储桶。"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_bucket_list(query)


def _back_to_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("« 返回列表", callback_data=CB_MENU_COS_CFG),
    ]])


def _config_id_from(data: str) -> int:
    return int(data.split(":", 1)[1])


async def cb_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.get_cos_config(s, config_id)
    if config is None:
        await query.edit_message_text(
            "该存储桶配置不存在,可能已被删除。", reply_markup=_back_to_list_kb(),
        )
        return
    rows = []
    if not config.is_active:
        rows.append([InlineKeyboardButton(
            "⭐ 设为当前使用", callback_data=f"{CB_COS_ACTIVE}{config.id}",
        )])
    rows.append([
        InlineKeyboardButton("🗑 删除", callback_data=f"{CB_COS_DEL}{config.id}"),
        InlineKeyboardButton("« 返回列表", callback_data=CB_MENU_COS_CFG),
    ])
    await query.edit_message_text(
        f"📦 存储桶: {config.bucket}\n"
        f"状态: {'✅ 当前使用(远程配置读写走这个桶)' if config.is_active else '备用'}\n"
        f"区域: {config.region}\n"
        f"SecretId: {mask_secret(config.secret_id)}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_set_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.set_active_cos_config(s, config_id)
        if config is None:
            await query.edit_message_text(
                "该存储桶配置不存在,可能已被删除。",
                reply_markup=_back_to_list_kb(),
            )
            return
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="cos.config.activate",
            result="success",
            detail=f"region={config.region}, bucket={config.bucket}",
        )
        await s.commit()
    await _render_bucket_list(query)


async def cb_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.get_cos_config(s, config_id)
        others = (
            len(await crud.list_cos_configs(s)) - 1 if config is not None else 0
        )
    if config is None:
        await query.edit_message_text(
            "该存储桶配置不存在,可能已被删除。", reply_markup=_back_to_list_kb(),
        )
        return
    if config.is_active and others:
        note = "它是当前使用的存储桶,删除后将自动启用另一个。"
    elif others:
        note = ""
    else:
        note = "删除后远程配置功能不可用。"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "确认删除", callback_data=f"{CB_COS_DEL_OK}{config.id}",
        ),
        InlineKeyboardButton(
            "取消", callback_data=f"{CB_COS_DETAIL}{config.id}",
        ),
    ]])
    await query.edit_message_text(
        f"⚠️ 将删除存储桶「{config.bucket} @ {config.region}」的配置"
        f"(不影响 COS 上的文件)。{note}确认删除?",
        reply_markup=kb,
    )


async def cb_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.delete_cos_config(s, config_id)
        if config is None:
            await query.edit_message_text(
                "该存储桶配置不存在,可能已被删除。",
                reply_markup=_back_to_list_kb(),
            )
            return
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="cos.config.del",
            result="success",
            detail=f"region={config.region}, bucket={config.bucket}",
        )
        await s.commit()
    await _render_bucket_list(query)


# ---------- 录入对话 ----------

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加腾讯云 COS 存储桶(区域与 bucket 名都相同时视为更新)。"
        "任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
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
        config = await crud.upsert_cos_config(
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
    active_note = (
        "当前远程配置使用的存储桶。" if config.is_active
        else "备用存储桶,远程配置仍走 ✅ 标记的那个,可在「COS 配置」列表中切换。"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ COS 配置已保存({note})\n"
            f"bucket: {data['bucket']} @ {data['region']}\n"
            f"{active_note}"
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
        CallbackQueryHandler(cb_detail, pattern=rf"^{CB_COS_DETAIL}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_set_active, pattern=rf"^{CB_COS_ACTIVE}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_del, pattern=rf"^{CB_COS_DEL}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_del_ok, pattern=rf"^{CB_COS_DEL_OK}\d+$")
    )
