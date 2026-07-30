"""把分发结果同步进远程配置 JSON。

安装包分发完成后,发布结果消息上会多一个「🔗 同步远程配置」按钮:
选目标文件 → 预览将写入的字段 → 确认后写回 COS。

写入的字段(其余字段如 api 一概不动):
- tag: Release 的 tag
- download_urls: 每个平台一个链接(macOS 给 arm64 的 dmg)
- download_assets: 各平台各架构的 url / sha256 / size

四个包(安卓通用、Windows amd64、macOS arm64、macOS amd64)必须齐全,
否则分发环节就不会给出同步按钮,避免半套配置上线。
"""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..db import crud
from ..services import release_manifest
from ..services.cos_api import COSAPIError
from .common import (
    CB_MENU_COS_CFG,
    CB_MENU_RCFG_ADD,
    CB_REL_SYNC,
    CB_RSYNC_FILE,
    CB_RSYNC_GO,
    CB_RSYNC_NO,
    get_ctx,
    human_size,
    truncate,
)
from .cos_config import load_cos
from .release import PUBLISH_KEY
from .remote_config import build_detail_text, detail_keyboard


log = logging.getLogger(__name__)


EXPIRED = "发布结果已失效(bot 可能重启过),请重新发布后再同步。"


def _args(data: str) -> list[int]:
    """rsyg:<serial>:<file_id> → [serial, file_id]。"""
    return [int(piece) for piece in data.split(":")[1:]]


def _publish(context, serial: int) -> tuple[dict | None, str]:
    """校验按钮所属的发布结果仍然有效,返回 (结果, 失败原因)。"""
    data = context.user_data.get(PUBLISH_KEY)
    if not isinstance(data, dict) or not data.get("assets"):
        return None, EXPIRED
    if data.get("serial") != serial:
        return None, (
            f"这条发布结果已被更新的发布({data['tag']})取代,"
            "请用最新那条发布消息上的按钮同步。"
        )
    missing = release_manifest.missing_slots(data["assets"])
    if missing:
        return None, (
            "本次发布缺少:"
            + "、".join(release_manifest.slot_label(s) for s in missing)
            + ",四个包齐全才允许同步。"
        )
    return data, ""


def _short(url: str, dir_url: str) -> str:
    """预览里省掉公共的目录前缀,只留文件名。"""
    return url[len(dir_url):] if url.startswith(dir_url) else url


def _preview_text(path: str, before: dict, patch: dict, dir_url: str) -> str:
    """按 patch 里最终的字段顺序渲染,预览即所写。

    dir_url 只用来把长链接缩成文件名显示,本身不写进配置。
    """
    old_tag = before.get("tag")
    lines = [
        f"🔗 即将写入「{path}」",
        "",
        f"tag: {old_tag or '(无)'} → {patch['tag']}",
        f"本版本目录: {dir_url}",
        "",
        "download_urls(相对上面的目录):",
    ]
    lines += [
        f"  {platform}: {_short(url, dir_url)}"
        for platform, url in patch["download_urls"].items()
    ]
    lines += ["", "download_assets:"]
    for platform, arches in patch["download_assets"].items():
        for arch, info in arches.items():
            lines.append(f"  {platform} {arch}: {_short(info['url'], dir_url)}")
            lines.append(
                f"    {human_size(info['size'])} ({info['size']}) "
                f"sha256 {info['sha256'][:12]}…"
            )

    kept = [
        key for key in (before.get("download_assets") or {})
        if key not in patch["download_assets"]
    ]
    lines.append("")
    if kept:
        lines.append("本次未上传、条目原样保留的平台:" + "、".join(kept))
    lines.append("其余字段(api、download_url 等)保持不动。")
    return truncate("\n".join(lines))


async def _fetch_json(cos, path: str) -> dict:
    """拉取并解析目标文件,顶层必须是 JSON 对象。"""
    content = await cos.get_object_text(path)
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"{path} 不是合法 JSON({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 的顶层不是 JSON 对象,无法按字段同步")
    return data


async def _load_file(query, file_id: int):
    async with crud.session() as s:
        file = await crud.get_remote_file(s, file_id)
    if file is None:
        await query.edit_message_text("文件不存在,可能已被移除。")
    return file


# ---------- 选文件 ----------

async def cb_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    (serial,) = _args(query.data)
    data, why = _publish(context, serial)
    if data is None:
        await query.edit_message_text(why)
        return

    ctx = get_ctx(context)
    try:
        cos = await load_cos(ctx)
    except COSAPIError as exc:
        await query.edit_message_text(f"⚠️ COS 配置异常,无法同步:{exc}")
        return
    if cos is None:
        await query.edit_message_text(
            "⚠️ COS 未配置,无法同步。请在「远程配置 → ⚙️ COS 配置」中录入。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "⚙️ 去配置 COS", callback_data=CB_MENU_COS_CFG,
            )]]),
        )
        return

    async with crud.session() as s:
        files = await crud.list_remote_files(s)
    if not files:
        await query.edit_message_text(
            "还没有登记远程配置文件,先添加一个再同步。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "➕ 添加文件", callback_data=CB_MENU_RCFG_ADD,
            )]]),
        )
        return
    if len(files) == 1:
        await _show_preview(update, context, serial, files[0].id)
        return
    rows = [
        [InlineKeyboardButton(
            f.path, callback_data=f"{CB_RSYNC_FILE}{serial}:{f.id}",
        )]
        for f in files
    ]
    await query.edit_message_text(
        f"把 {data['tag']} 的版本信息同步到哪个配置文件?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------- 预览 ----------

async def cb_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    serial, file_id = _args(query.data)
    await _show_preview(update, context, serial, file_id)


async def _show_preview(
    update: Update, context: ContextTypes.DEFAULT_TYPE, serial: int, file_id: int
) -> None:
    query = update.callback_query
    data, why = _publish(context, serial)
    if data is None:
        await query.edit_message_text(why)
        return
    file = await _load_file(query, file_id)
    if file is None:
        return

    ctx = get_ctx(context)
    try:
        cos = await load_cos(ctx)
        if cos is None:
            await query.edit_message_text("COS 尚未配置,请先完成配置。")
            return
        before = await _fetch_json(cos, file.path)
    except COSAPIError as exc:
        await query.edit_message_text(truncate(f"❌ 拉取 {file.path} 失败:{exc}"))
        return
    except ValueError as exc:
        await query.edit_message_text(
            truncate(f"❌ {exc}\n请回到「远程配置」用「替换全文」修好再同步。")
        )
        return

    patch = release_manifest.build_patch(
        tag=data["tag"], assets=data["assets"],
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ 确认写入", callback_data=f"{CB_RSYNC_GO}{serial}:{file_id}",
        ),
        InlineKeyboardButton("取消", callback_data=CB_RSYNC_NO),
    ]])
    await query.edit_message_text(
        _preview_text(file.path, before, patch, data["dir_url"]),
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def cb_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "已取消同步,远程配置未改动。分发到 OSS 的文件不受影响。"
    )


# ---------- 写入 ----------

async def cb_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    serial, file_id = _args(query.data)
    data, why = _publish(context, serial)
    if data is None:
        await query.edit_message_text(why)
        return
    file = await _load_file(query, file_id)
    if file is None:
        return

    ctx = get_ctx(context)
    patch = release_manifest.build_patch(
        tag=data["tag"], assets=data["assets"],
    )
    try:
        cos = await load_cos(ctx)
        if cos is None:
            await query.edit_message_text("COS 尚未配置,请先完成配置。")
            return
        # 预览到确认之间文件可能被改过,以此刻的内容为基础合并
        before = await _fetch_json(cos, file.path)
        new_text = json.dumps(
            release_manifest.apply_patch(before, patch),
            ensure_ascii=False,
            indent=2,
        )
        await cos.put_object_text(file.path, new_text)
    except COSAPIError as exc:
        await query.edit_message_text(truncate(f"❌ 同步失败:{exc}"))
        return
    except ValueError as exc:
        await query.edit_message_text(truncate(f"❌ {exc}"))
        return

    await _log_sync(update, file.path, data)
    await query.edit_message_text(
        f"✅ 已把 {data['tag']} 同步到「{file.path}」。"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_detail_text(file.path, new_text),
        reply_markup=detail_keyboard(file_id),
    )


async def _log_sync(update: Update, path: str, data: dict) -> None:
    try:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="release.config.sync",
                result="success",
                detail=(
                    f"path={path}, repo={data['repo']}, tag={data['tag']}, "
                    f"assets={len(data['assets'])}"
                ),
            )
            await s.commit()
    except Exception:  # noqa: BLE001
        log.exception("写操作日志失败")


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(cb_start, pattern=rf"^{CB_REL_SYNC}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_preview, pattern=rf"^{CB_RSYNC_FILE}\d+:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_go, pattern=rf"^{CB_RSYNC_GO}\d+:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_drop, pattern=f"^{CB_RSYNC_NO}$")
    )
