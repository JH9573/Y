"""安装包分发:从 GitHub Release 下载构建产物并上传到阿里云 OSS。

主菜单 → 📦 安装包分发 → 仓库列表 → 选仓库 → 选版本 → 确认后执行:
逐个 asset 下载到本地临时目录 → PutObject 到 <prefix>/<tag>/<文件名>
(public-read)→ 服务端复制刷新 <prefix>/latest/ → 回复分发链接。

OSS 凭据来自 .env(OSS_REGION / OSS_BUCKET / OSS_ACCESS_KEY_ID /
OSS_ACCESS_KEY_SECRET),未配置时提示功能未启用。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import suppress

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ..db import crud
from ..services.github_release import GitHubAPIError
from ..services.oss_api import OSSAPIError
from .common import (
    CB_BACK_REL_LIST,
    CB_DEL_REL_SRC,
    CB_DEL_REL_SRC_OK,
    CB_MENU_REL_ADD,
    CB_REL_GO,
    CB_REL_PICK,
    CB_REL_SRC,
    CB_REL_VER,
    get_ctx,
    human_size,
    humanize_age,
    truncate,
)


log = logging.getLogger(__name__)

# Release 列表缓存键(callback_data 放不下 tag,存 user_data 用索引引用)
RELEASES_KEY = "rel_releases"
MAX_RELEASES = 10


async def _reply_or_edit(update: Update, text: str, reply_markup=None) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup,
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=reply_markup,
        )


# ---------- 仓库列表 / 详情 / 删除 ----------

async def show_source_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    async with crud.session() as s:
        sources = await crud.list_release_sources(s)
    if not sources:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "➕ 添加仓库", callback_data=CB_MENU_REL_ADD,
        )]])
        await _reply_or_edit(
            update, "还没有登记分发仓库。", reply_markup=kb,
        )
        return
    rows = [
        [InlineKeyboardButton(
            src.repo, callback_data=f"{CB_REL_SRC}{src.id}",
        )]
        for src in sources
    ]
    rows.append([InlineKeyboardButton(
        "➕ 添加仓库", callback_data=CB_MENU_REL_ADD,
    )])
    await _reply_or_edit(
        update, "分发仓库列表:", reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_back_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await show_source_list(update, context)


async def cb_source_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    source_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        source = await crud.get_release_source(s, source_id)
    if source is None:
        await query.edit_message_text("仓库不存在,可能已被删除。")
        return
    ctx = get_ctx(context)
    oss_note = (
        f"oss://{ctx.oss.bucket}/{ctx.config.oss_prefix}/"
        if ctx.oss is not None
        else "⚠️ 未配置(.env 补 OSS_* 后重启)"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🚀 选择版本发布", callback_data=f"{CB_REL_PICK}{source.id}",
        )],
        [
            InlineKeyboardButton(
                "🗑 删除仓库", callback_data=f"{CB_DEL_REL_SRC}{source.id}",
            ),
            InlineKeyboardButton("« 返回列表", callback_data=CB_BACK_REL_LIST),
        ],
    ])
    await query.edit_message_text(
        f"📦 {source.repo}\n"
        f"登记于: {humanize_age(source.created_at)}\n"
        f"上传目标: {oss_note}",
        reply_markup=kb,
    )


async def cb_source_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    source_id = int(query.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "确认删除", callback_data=f"{CB_DEL_REL_SRC_OK}{source_id}",
        ),
        InlineKeyboardButton("取消", callback_data=f"{CB_REL_SRC}{source_id}"),
    ]])
    await query.edit_message_text(
        "⚠️ 删除后需重新录入 Token 才能再次发布,确认删除该仓库?"
        "(不影响 OSS 上已发布的文件)",
        reply_markup=kb,
    )


async def cb_source_del_ok(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    source_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        source = await crud.get_release_source(s, source_id)
        repo = source.repo if source else f"id={source_id}"
        await crud.delete_release_source(s, source_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="release.source.del",
            result="success",
            detail=f"repo={repo}",
        )
        await s.commit()
    await query.edit_message_text(f"✅ 已删除分发仓库「{repo}」。")


# ---------- 选版本 ----------

async def cb_pick_release(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    source_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        source = await crud.get_release_source(s, source_id)
    if source is None:
        await query.edit_message_text("仓库不存在,可能已被删除。")
        return

    await query.edit_message_text(f"⏳ 正在获取 {source.repo} 的 Release 列表…")
    ctx = get_ctx(context)
    try:
        releases = await ctx.github.list_releases(source, limit=MAX_RELEASES)
    except GitHubAPIError as exc:
        await query.edit_message_text(truncate(f"❌ 获取 Release 失败:{exc}"))
        return
    releases = [r for r in releases if r["assets"]]
    if not releases:
        await query.edit_message_text(
            "该仓库没有带构建产物的 Release。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "« 返回", callback_data=f"{CB_REL_SRC}{source_id}",
            )]]),
        )
        return

    context.user_data[RELEASES_KEY] = {
        "source_id": source_id,
        "repo": source.repo,
        "items": releases,
    }
    rows = []
    for i, rel in enumerate(releases):
        total = sum(a["size"] for a in rel["assets"])
        label = (
            f"{rel['tag']}({len(rel['assets'])} 个文件, {human_size(total)})"
        )
        if rel["prerelease"]:
            label = "🧪 " + label
        rows.append([InlineKeyboardButton(
            label, callback_data=f"{CB_REL_VER}{i}",
        )])
    rows.append([InlineKeyboardButton(
        "« 返回", callback_data=f"{CB_REL_SRC}{source_id}",
    )])
    await query.edit_message_text(
        f"选择要发布到 OSS 的版本({source.repo}):",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _release_from_index(context, data: str) -> tuple[dict, dict] | None:
    """从 callback_data 的索引取出缓存的 release,返回 (缓存, release)。"""
    try:
        idx = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    cache = context.user_data.get(RELEASES_KEY) or {}
    items = cache.get("items") or []
    if 0 <= idx < len(items):
        return cache, items[idx]
    return None


async def cb_pick_version(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    found = _release_from_index(context, query.data)
    if found is None:
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    cache, rel = found
    ctx = get_ctx(context)
    if ctx.oss is None:
        await query.edit_message_text(
            "⚠️ OSS 未配置,无法发布。请在 .env 中补齐 OSS_REGION / "
            "OSS_BUCKET / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 后重启 bot。"
        )
        return

    prefix = ctx.config.oss_prefix
    total = sum(a["size"] for a in rel["assets"])
    lines = [f"· {a['name']} ({human_size(a['size'])})" for a in rel["assets"]]
    idx = query.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 确认发布", callback_data=f"{CB_REL_GO}{idx}"),
        InlineKeyboardButton(
            "« 返回", callback_data=f"{CB_REL_PICK}{cache['source_id']}",
        ),
    ]])
    await query.edit_message_text(
        truncate(
            f"📦 {rel['tag']} — {cache['repo']}\n"
            f"共 {len(rel['assets'])} 个文件,合计 {human_size(total)}:\n"
            + "\n".join(lines)
            + f"\n\n上传到: oss://{ctx.oss.bucket}/{prefix}/{rel['tag']}/\n"
            f"并刷新固定目录: {prefix}/latest/\n"
            f"文件将设为公共读(public-read),确认发布?"
        ),
        reply_markup=kb,
    )


# ---------- 执行发布 ----------

async def cb_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    found = _release_from_index(context, query.data)
    if found is None:
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    cache, rel = found
    ctx = get_ctx(context)
    if ctx.oss is None:
        await query.edit_message_text("⚠️ OSS 未配置,无法发布。")
        return
    async with crud.session() as s:
        source = await crud.get_release_source(s, cache["source_id"])
    if source is None:
        await query.edit_message_text("仓库不存在,可能已被删除。")
        return

    oss = ctx.oss
    prefix = ctx.config.oss_prefix
    tag = rel["tag"]
    assets = rel["assets"]
    n = len(assets)
    uploaded: list[tuple[str, str]] = []  # (文件名, 版本 key)
    tmpdir = tempfile.mkdtemp(prefix="sbot-release-")
    try:
        for i, asset in enumerate(assets):
            name = os.path.basename(asset["name"])
            label = f"({i + 1}/{n}) {name}({human_size(asset['size'])})"
            path = os.path.join(tmpdir, name)
            await query.edit_message_text(f"⏳ {label} 从 GitHub 下载中…")
            await ctx.github.download_asset(source, asset["id"], path)
            await query.edit_message_text(f"⏳ {label} 上传 OSS 中…")
            key = f"{prefix}/{tag}/{name}"
            await oss.put_object_file(key, path)
            os.remove(path)  # 及时释放磁盘,大包场景重要
            uploaded.append((name, key))

        # 刷新 latest/:先复制新文件(覆盖同名),再清掉上个版本残留
        await query.edit_message_text(f"⏳ 正在刷新 {prefix}/latest/ …")
        latest_prefix = f"{prefix}/latest/"
        old_keys = set(await oss.list_keys(latest_prefix))
        copy_errors: list[str] = []
        new_keys: set[str] = set()
        for name, key in uploaded:
            latest_key = latest_prefix + name
            try:
                await oss.copy_object(key, latest_key)
                new_keys.add(latest_key)
            except OSSAPIError as exc:
                # 服务端复制单文件上限 1GB,超限等场景仅记录不中断
                copy_errors.append(f"{name}: {exc}")
        for stale in old_keys - new_keys:
            with suppress(OSSAPIError):
                await oss.delete_object(stale)
    except (GitHubAPIError, OSSAPIError) as exc:
        detail = f"repo={source.repo}, tag={tag}: {exc}"
        log.warning("发布失败: %s", detail)
        await query.edit_message_text(truncate(f"❌ 发布中止:{exc}"))
        await _log_action(update, "release.publish", "failed", truncate(detail, 400))
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    lines = [f"✅ {tag} 发布完成,共 {n} 个文件。\n"]
    lines.append("固定链接(始终指向最新版本):")
    lines += [
        oss.public_url(latest_prefix + name)
        for name, _ in uploaded
    ]
    lines.append("\n本版本链接:")
    lines += [oss.public_url(key) for _, key in uploaded]
    if copy_errors:
        lines.append("\n⚠️ latest 刷新部分失败:")
        lines += copy_errors
    await query.edit_message_text(
        truncate("\n".join(lines)), disable_web_page_preview=True,
    )
    await _log_action(
        update,
        "release.publish",
        "success",
        f"repo={source.repo}, tag={tag}, files={n}",
    )


async def _log_action(update: Update, action: str, result: str, detail: str) -> None:
    try:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action=action,
                result=result,
                detail=detail,
            )
            await s.commit()
    except Exception:  # noqa: BLE001
        log.exception("写操作日志失败")


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("repos", show_source_list))
    application.add_handler(
        CallbackQueryHandler(cb_back_list, pattern=f"^{CB_BACK_REL_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_source_detail, pattern=rf"^{CB_REL_SRC}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_source_del, pattern=rf"^{CB_DEL_REL_SRC}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_source_del_ok, pattern=rf"^{CB_DEL_REL_SRC_OK}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_pick_release, pattern=rf"^{CB_REL_PICK}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_pick_version, pattern=rf"^{CB_REL_VER}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_publish, pattern=rf"^{CB_REL_GO}\d+$")
    )
