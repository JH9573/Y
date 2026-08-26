"""OSS 分发配置:存储桶列表、交互录入、切换与删除。

主菜单 → 📦 安装包分发 → ⚙️ OSS 配置

可录入多个存储桶,✅ 标记的那个是发布时实际使用的(「当前使用」);
其余为备用,在详情页可一键切换。(region, bucket) 相同视为同一配置,
重复录入即更新凭据。
配置优先级: 数据库(bot 内录入)> .env 的 OSS_*(仅当库里一个桶都没有)。
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
    CB_OSS_ACTIVE,
    CB_OSS_DEL,
    CB_OSS_DEL_OK,
    CB_OSS_DETAIL,
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
    """按优先级构建 OSS 客户端(多桶时用当前使用的那个),
    返回 (client, 路径前缀);未配置返回 None。"""
    async with crud.session() as s:
        row = await crud.get_active_oss_config(s)
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


# ---------- 存储桶列表 / 详情 / 切换 / 删除 ----------

async def _render_bucket_list(query, ctx: AppContext) -> None:
    async with crud.session() as s:
        configs = await crud.list_oss_configs(s)

    add_row = [InlineKeyboardButton("➕ 添加存储桶", callback_data=CB_OSS_EDIT)]
    if configs:
        rows = [
            [InlineKeyboardButton(
                f"{'✅ ' if c.is_active else ''}{c.bucket} @ {c.region}",
                callback_data=f"{CB_OSS_DETAIL}{c.id}",
            )]
            for c in configs
        ]
        rows.append(add_row)
        text = (
            f"⚙️ OSS 配置(共 {len(configs)} 个存储桶)\n"
            "✅ 为当前发布使用的存储桶;点击查看详情、切换或删除。"
        )
    elif ctx.config.oss_configured:
        cfg = ctx.config
        rows = [add_row]
        text = (
            "⚙️ OSS 配置\n"
            "来源: .env 环境变量\n"
            f"区域: {cfg.oss_region}\n"
            f"Bucket: {cfg.oss_bucket}\n"
            f"AccessKey ID: {mask_secret(cfg.oss_access_key_id)}\n"
            f"下载域名: {cfg.oss_public_base_url or '(默认 bucket 域名)'}\n"
            f"路径前缀: {cfg.oss_prefix}/\n\n"
            "在 bot 内添加存储桶后将以 bot 配置为准。"
        )
    else:
        rows = [add_row]
        text = "⚙️ OSS 尚未配置,发布功能不可用。点下方按钮添加存储桶。"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_bucket_list(query, get_ctx(context))


def _back_to_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("« 返回列表", callback_data=CB_MENU_OSS_CFG),
    ]])


def _config_id_from(data: str) -> int:
    return int(data.split(":", 1)[1])


async def cb_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.get_oss_config(s, config_id)
    if config is None:
        await query.edit_message_text(
            "该存储桶配置不存在,可能已被删除。", reply_markup=_back_to_list_kb(),
        )
        return
    rows = []
    if not config.is_active:
        rows.append([InlineKeyboardButton(
            "⭐ 设为当前使用", callback_data=f"{CB_OSS_ACTIVE}{config.id}",
        )])
    rows.append([
        InlineKeyboardButton("🗑 删除", callback_data=f"{CB_OSS_DEL}{config.id}"),
        InlineKeyboardButton("« 返回列表", callback_data=CB_MENU_OSS_CFG),
    ])
    await query.edit_message_text(
        f"📦 存储桶: {config.bucket}\n"
        f"状态: {'✅ 当前使用(发布走这个桶)' if config.is_active else '备用'}\n"
        f"区域: {config.region}\n"
        f"AccessKey ID: {mask_secret(config.access_key_id)}\n"
        f"下载域名: {config.public_base_url or '(默认 bucket 域名)'}\n"
        f"路径前缀: {config.prefix}/",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_set_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.set_active_oss_config(s, config_id)
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
            action="oss.config.activate",
            result="success",
            detail=f"region={config.region}, bucket={config.bucket}",
        )
        await s.commit()
    await _render_bucket_list(query, get_ctx(context))


async def cb_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.get_oss_config(s, config_id)
        others = (
            len(await crud.list_oss_configs(s)) - 1 if config is not None else 0
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
        ctx = get_ctx(context)
        note = (
            "删除后回退到 .env 配置。" if ctx.config.oss_configured
            else "删除后无可用 OSS 配置,发布功能不可用。"
        )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "确认删除", callback_data=f"{CB_OSS_DEL_OK}{config.id}",
        ),
        InlineKeyboardButton(
            "取消", callback_data=f"{CB_OSS_DETAIL}{config.id}",
        ),
    ]])
    await query.edit_message_text(
        f"⚠️ 将删除存储桶「{config.bucket} @ {config.region}」的配置"
        f"(不影响 OSS 上已有文件)。{note}确认删除?",
        reply_markup=kb,
    )


async def cb_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config_id = _config_id_from(query.data)
    async with crud.session() as s:
        config = await crud.delete_oss_config(s, config_id)
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
            action="oss.config.del",
            result="success",
            detail=f"region={config.region}, bucket={config.bucket}",
        )
        await s.commit()
    await _render_bucket_list(query, get_ctx(context))


# ---------- 录入对话 ----------

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加 OSS 存储桶(区域与 bucket 名都相同时视为更新)。"
        "任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
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
        config = await crud.upsert_oss_config(
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
    active_note = (
        "当前发布使用的存储桶。" if config.is_active
        else "备用存储桶,发布仍走 ✅ 标记的那个,可在「OSS 配置」列表中切换。"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ OSS 配置已保存({note})\n"
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
        CallbackQueryHandler(cb_detail, pattern=rf"^{CB_OSS_DETAIL}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_set_active, pattern=rf"^{CB_OSS_ACTIVE}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_del, pattern=rf"^{CB_OSS_DEL}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_del_ok, pattern=rf"^{CB_OSS_DEL_OK}\d+$")
    )
