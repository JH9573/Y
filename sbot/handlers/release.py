"""安装包分发:从 GitHub Release 下载构建产物并上传到阿里云 OSS。

主菜单 → 📦 安装包分发 → 仓库列表 → 选仓库 → 选版本 → 确认后执行:
逐个 asset 下载到本地临时目录 → 算 sha256 → PutObject 到
<prefix>/<tag>/<文件名>(public-read)→ 服务端复制刷新 <prefix>/latest/ →
回复分发链接。

发布结果(版本号、各平台链接、sha256、体积)会存进 user_data,供
release_sync.py 一键同步到远程配置 JSON。

OSS 凭据来自 .env(OSS_REGION / OSS_BUCKET / OSS_ACCESS_KEY_ID /
OSS_ACCESS_KEY_SECRET),未配置时提示功能未启用。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ..db import crud
from ..services import release_manifest
from ..services.github_release import GitHubAPIError
from ..services.oss_api import OSSAPIError
from ..services.release_manifest import ManifestAsset
from .common import (
    CB_BACK_REL_LIST,
    CB_DEL_REL_SRC,
    CB_DEL_REL_SRC_OK,
    CB_MENU_REL_ADD,
    CB_REL_ALL,
    CB_REL_GO,
    CB_REL_NONE,
    CB_REL_PICK,
    CB_REL_SRC,
    CB_REL_SYNC,
    CB_REL_TOGGLE,
    CB_REL_VER,
    get_ctx,
    human_size,
    humanize_age,
    truncate,
)
from .oss_config import load_oss


log = logging.getLogger(__name__)

# Release 列表缓存键(callback_data 放不下 tag,存 user_data 用索引引用)
RELEASES_KEY = "rel_releases"
MAX_RELEASES = 10

# 最近一次发布结果,release_sync.py 读它来同步远程配置。
# 每次发布递增 serial 并写进按钮的 callback_data:旧消息上的按钮点了会
# 被认出是过期的,不会把新版本的信息错当成它自己的。
PUBLISH_KEY = "rel_publish"


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
    try:
        oss_pair = await load_oss(ctx)
    except OSSAPIError as exc:
        oss_pair = None
        oss_note = f"⚠️ 配置异常: {exc}"
    else:
        oss_note = (
            f"oss://{oss_pair[0].bucket}/{oss_pair[1]}/"
            if oss_pair is not None
            else "⚠️ 未配置(分发菜单 → OSS 配置)"
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
    try:
        oss_pair = await load_oss(ctx)
    except OSSAPIError as exc:
        await query.edit_message_text(f"⚠️ OSS 配置异常,无法发布:{exc}")
        return
    if oss_pair is None:
        await query.edit_message_text(
            "⚠️ OSS 未配置,无法发布。请在「安装包分发 → ⚙️ OSS 配置」中录入。"
        )
        return

    oss, prefix = oss_pair
    idx = int(query.data.split(":", 1)[1])
    # 进入勾选页:默认全选,状态存 user_data
    cache["sel_version"] = idx
    cache["selected"] = set(range(len(rel["assets"])))
    cache["oss_bucket"] = oss.bucket
    cache["oss_prefix"] = prefix
    await _render_selection(query, context)


def _selection_state(context) -> tuple[dict, dict, set[int]] | None:
    """取当前勾选上下文,返回 (缓存, release, 已选索引集合)。"""
    cache = context.user_data.get(RELEASES_KEY) or {}
    items = cache.get("items") or []
    ver = cache.get("sel_version")
    if ver is None or not (0 <= ver < len(items)):
        return None
    selected = cache.get("selected")
    if not isinstance(selected, set):
        return None
    return cache, items[ver], selected


async def _render_selection(query, context) -> None:
    """渲染文件勾选页(每个文件一个可切换按钮 + 全选/清空/确认)。"""
    state = _selection_state(context)
    if state is None:
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    cache, rel, selected = state
    assets = rel["assets"]
    ver = cache["sel_version"]

    rows = []
    for i, a in enumerate(assets):
        mark = "✅" if i in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {a['name']} ({human_size(a['size'])})",
            callback_data=f"{CB_REL_TOGGLE}{i}",
        )])
    rows.append([
        InlineKeyboardButton("全选", callback_data=CB_REL_ALL),
        InlineKeyboardButton("清空", callback_data=CB_REL_NONE),
    ])
    rows.append([
        InlineKeyboardButton(
            f"✅ 确认发布({len(selected)})", callback_data=f"{CB_REL_GO}{ver}",
        ),
        InlineKeyboardButton(
            "« 返回", callback_data=f"{CB_REL_PICK}{cache['source_id']}",
        ),
    ])
    sel_total = sum(assets[i]["size"] for i in selected)
    await query.edit_message_text(
        f"📦 {rel['tag']} — {cache['repo']}\n"
        f"勾选要发布的文件(已选 {len(selected)}/{len(assets)},"
        f"合计 {human_size(sel_total)}):\n\n"
        f"上传到: oss://{cache['oss_bucket']}/{cache['oss_prefix']}/{rel['tag']}/\n"
        f"并刷新固定目录: {cache['oss_prefix']}/latest/\n"
        f"文件将设为公共读(public-read)。",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = _selection_state(context)
    if state is None:
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    _cache, rel, selected = state
    try:
        idx = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if not (0 <= idx < len(rel["assets"])):
        return
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await _render_selection(query, context)


async def cb_select_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = _selection_state(context)
    if state is None:
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    cache, rel, _selected = state
    cache["selected"] = set(range(len(rel["assets"])))
    await _render_selection(query, context)


async def cb_select_none(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = _selection_state(context)
    if state is None:
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    cache, _rel, selected = state
    selected.clear()
    await _render_selection(query, context)


# ---------- 执行发布 ----------

async def cb_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    state = _selection_state(context)
    if state is None:
        await query.answer()
        await query.edit_message_text("版本列表已过期,请重新进入仓库选择。")
        return
    cache, rel, selected = state
    if not selected:
        await query.answer("请至少勾选一个文件。", show_alert=True)
        return
    await query.answer()

    ctx = get_ctx(context)
    try:
        oss_pair = await load_oss(ctx)
    except OSSAPIError as exc:
        await query.edit_message_text(f"⚠️ OSS 配置异常,无法发布:{exc}")
        return
    if oss_pair is None:
        await query.edit_message_text("⚠️ OSS 未配置,无法发布。")
        return
    async with crud.session() as s:
        source = await crud.get_release_source(s, cache["source_id"])
    if source is None:
        await query.edit_message_text("仓库不存在,可能已被删除。")
        return

    oss, prefix = oss_pair
    tag = rel["tag"]
    # 只处理勾选的文件,保持原始顺序
    assets = [rel["assets"][i] for i in sorted(selected)]
    n = len(assets)
    uploaded: list[tuple[str, str]] = []  # (文件名, 版本 key)
    manifest: list[ManifestAsset] = []  # 能识别平台/架构的,供同步远程配置
    unknown: list[str] = []  # 认不出平台/架构的文件名
    tmpdir = tempfile.mkdtemp(prefix="sbot-release-")
    try:
        for i, asset in enumerate(assets):
            name = os.path.basename(asset["name"])
            label = f"({i + 1}/{n}) {name}({human_size(asset['size'])})"
            path = os.path.join(tmpdir, name)
            await query.edit_message_text(f"⏳ {label} 从 GitHub 下载中…")
            size = await ctx.github.download_asset(source, asset["id"], path)
            # 校验值按实际落盘的文件算,不用 GitHub 报的 size
            digest = await release_manifest.sha256_file(path)
            await query.edit_message_text(f"⏳ {label} 上传 OSS 中…")
            key = f"{prefix}/{tag}/{name}"
            await oss.put_object_file(key, path)
            os.remove(path)  # 及时释放磁盘,大包场景重要
            uploaded.append((name, key))
            slot = release_manifest.classify(name)
            if slot is None:
                unknown.append(name)
            else:
                manifest.append(ManifestAsset(
                    name=name,
                    platform=slot[0],
                    arch=slot[1],
                    url=oss.public_url(key),
                    sha256=digest,
                    size=size,
                ))

        # 刷新 latest/:仅把本次上传的文件复制过去(覆盖同名)。
        # 因为是按需选择上传,不删除 latest/ 里其它已存在的文件。
        await query.edit_message_text(f"⏳ 正在刷新 {prefix}/latest/ …")
        latest_prefix = f"{prefix}/latest/"
        copy_errors: list[str] = []
        for name, key in uploaded:
            try:
                await oss.copy_object(key, latest_prefix + name)
            except OSSAPIError as exc:
                # 服务端复制单文件上限 1GB,超限等场景仅记录不中断
                copy_errors.append(f"{name}: {exc}")
    except (GitHubAPIError, OSSAPIError) as exc:
        detail = f"repo={source.repo}, tag={tag}: {exc}"
        log.warning("发布失败: %s", detail)
        await query.edit_message_text(truncate(f"❌ 发布中止:{exc}"))
        await _log_action(update, "release.publish", "failed", truncate(detail, 400))
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    previous = context.user_data.get(PUBLISH_KEY) or {}
    serial = int(previous.get("serial") or 0) + 1
    context.user_data[PUBLISH_KEY] = {
        "serial": serial,
        "repo": source.repo,
        "tag": tag,
        "dir_url": oss.public_url(f"{prefix}/{tag}/"),
        "assets": manifest,
    }

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

    missing = release_manifest.missing_slots(manifest)
    kb = None
    if missing:
        lines.append(
            "\n⚠️ 不能同步远程配置,缺少:"
            + "、".join(release_manifest.slot_label(s) for s in missing)
            + "(四个包齐全才允许同步)"
        )
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🔗 同步远程配置", callback_data=f"{CB_REL_SYNC}{serial}",
        )]])
    if unknown:
        lines.append("(未识别平台/架构,不参与同步:" + "、".join(unknown) + ")")

    await query.edit_message_text(
        truncate("\n".join(lines)),
        reply_markup=kb,
        disable_web_page_preview=True,
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
        CallbackQueryHandler(cb_toggle, pattern=rf"^{CB_REL_TOGGLE}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_select_all, pattern=f"^{CB_REL_ALL}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_select_none, pattern=f"^{CB_REL_NONE}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_publish, pattern=rf"^{CB_REL_GO}\d+$")
    )
