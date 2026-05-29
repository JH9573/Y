"""更新重启:从 git 仓库拉取最新代码并重启进程。

支持两种模式:
1. 更新当前分支(git reset --hard origin/<当前分支>)
2. 切换到其它分支(含 PR 分支)并重启(git checkout -f -B <分支> origin/<分支>)

依赖外部进程管理器(systemd `Restart=always`)在进程退出后自动拉起,从而
加载新代码。本模块只负责:拉取/切换代码 -> (如有需要)安装依赖 -> 触发进程退出。

危险操作,走二次确认 + 白名单鉴权(白名单由 main 的 _wrap_with_auth 统一套上)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import BACKUPS_DIR, ROOT_DIR
from ..db import crud
from .common import (
    CB_UPDATE_BRANCH,
    CB_UPDATE_CANCEL,
    CB_UPDATE_CONFIRM,
    CB_UPDATE_PICK,
    CB_UPDATE_SWITCH,
    MENU_UPDATE,
    truncate,
)


log = logging.getLogger(__name__)

# requirements.txt 位于 sbot 包目录下
REQUIREMENTS = ROOT_DIR / "requirements.txt"
# git 仓库根目录(sbot 包的上一级)
REPO = str(ROOT_DIR.parent)
# 重启交接标记:旧进程退出前写入,新进程启动后据此回复「重启完成」。
# 放在 .gitignore 覆盖的 backups 目录,reset --hard / checkout 不会动它。
RESTART_MARKER = BACKUPS_DIR / ".restart.json"
# 分支列表缓存键 & 列表上限(Telegram 键盘不宜过长)
BRANCHES_KEY = "update_branches"
MAX_BRANCHES = 30


async def _run(*args: str, timeout: int = 120):
    """运行子进程,返回 (returncode, 合并后的输出)。"""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=REPO,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"命令超时({timeout}s): {' '.join(args)}"
    return proc.returncode, out.decode(errors="replace").strip()


async def _git(*args: str, timeout: int = 120):
    return await _run("git", *args, timeout=timeout)


async def _current_branch() -> str:
    rc, branch = await _git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch in ("", "HEAD"):
        return "main"  # detached 或读取失败时回退到主分支
    return branch


# ---------- 入口:展示当前版本 + 操作选项 ----------

async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rc_b, branch = await _git("rev-parse", "--abbrev-ref", "HEAD")
    rc_h, head = await _git("rev-parse", "--short", "HEAD")
    if rc_b != 0 or rc_h != 0:
        await update.effective_message.reply_text(
            "无法读取当前 git 状态,可能不是 git 仓库,已中止。"
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 更新当前分支并重启", callback_data=CB_UPDATE_CONFIRM)],
            [InlineKeyboardButton("🌿 切换到其它分支…", callback_data=CB_UPDATE_PICK)],
            [InlineKeyboardButton("取消", callback_data=CB_UPDATE_CANCEL)],
        ]
    )
    await update.effective_message.reply_text(
        f"当前版本:{branch} @ {head}\n\n请选择操作(执行时 bot 会短暂离线):",
        reply_markup=kb,
    )


async def cb_update_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("已取消。")


# ---------- 更新当前分支 ----------

async def cb_update_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _do_update(update, context, target_branch=None)


# ---------- 切换分支 ----------

async def cb_update_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """拉取远程分支列表并展示供选择。"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ 正在获取远程分支…")

    rc, out = await _git("ls-remote", "--heads", "origin", timeout=30)
    if rc != 0:
        await query.edit_message_text(truncate(f"获取远程分支失败:\n{out}"))
        return

    branches = []
    for line in out.splitlines():
        parts = line.split("\trefs/heads/")
        if len(parts) == 2:
            branches.append(parts[1].strip())
    if not branches:
        await query.edit_message_text("远程没有可用分支。")
        return

    # main 置顶,其余按名称排序;截断以保护键盘长度
    branches = sorted(branches, key=lambda b: (b != "main", b))[:MAX_BRANCHES]
    context.user_data[BRANCHES_KEY] = branches

    cur = await _current_branch()
    rows = [
        [
            InlineKeyboardButton(
                ("✓ " if b == cur else "") + b,
                callback_data=f"{CB_UPDATE_BRANCH}{i}",
            )
        ]
        for i, b in enumerate(branches)
    ]
    rows.append([InlineKeyboardButton("取消", callback_data=CB_UPDATE_CANCEL)])
    await query.edit_message_text(
        "选择要切换并重启的分支:", reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_update_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """选中某分支,弹出二次确认。"""
    query = update.callback_query
    await query.answer()
    branch = _branch_from_index(context, query.data)
    if branch is None:
        await query.edit_message_text("分支列表已过期,请重新点「🔄 更新重启」。")
        return

    idx = query.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认切换并重启", callback_data=f"{CB_UPDATE_SWITCH}{idx}"
                ),
                InlineKeyboardButton("取消", callback_data=CB_UPDATE_CANCEL),
            ]
        ]
    )
    await query.edit_message_text(
        f"⚠️ 将切换到分支 {branch} 并重启 bot,确认?", reply_markup=kb
    )


async def cb_update_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    branch = _branch_from_index(context, query.data)
    if branch is None:
        await query.edit_message_text("分支列表已过期,请重新点「🔄 更新重启」。")
        return
    await _do_update(update, context, target_branch=branch)


def _branch_from_index(context, data: str) -> str | None:
    """从 callback_data 的索引取出缓存的分支名,越界返回 None。"""
    try:
        idx = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    branches = context.user_data.get(BRANCHES_KEY) or []
    if 0 <= idx < len(branches):
        return branches[idx]
    return None


# ---------- 核心:拉取/切换 -> 装依赖 -> 重启 ----------

async def _do_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_branch: str | None,
) -> None:
    query = update.callback_query
    await query.edit_message_text("⏳ 正在拉取最新代码…")

    cur = await _current_branch()
    branch = target_branch or cur
    switching = branch != cur

    _, before = await _git("rev-parse", "HEAD")

    rc, out = await _git("fetch", "origin", branch)
    if rc != 0:
        await _finish_failed(update, f"git fetch 失败:\n{out}")
        return

    req_before = _read_requirements()

    if switching:
        rc, out = await _git("checkout", "-f", "-B", branch, f"origin/{branch}")
        op = "checkout"
    else:
        rc, out = await _git("reset", "--hard", f"origin/{branch}")
        op = "reset"
    if rc != 0:
        await _finish_failed(update, f"git {op} 失败:\n{out}")
        return

    _, after = await _git("rev-parse", "HEAD")
    _, after_short = await _git("rev-parse", "--short", "HEAD")
    _, before_short = await _git("rev-parse", "--short", before)

    # 代码未变化则无需重启(切换到同 commit 的分支也算)
    if before == after:
        note = f"已切换到 {branch}(代码一致)" if switching else f"已是最新 {after_short}"
        await _log_action(update, "bot.update", "success", note)
        await query.edit_message_text(f"✅ {note},无需重启。")
        return

    _, changelog = await _git(
        "log", "--oneline", "--no-decorate", "-n", "30", f"{before}..{after}"
    )

    # 依赖变化则重装
    dep_note = ""
    if _read_requirements() != req_before:
        await query.edit_message_text("📦 依赖有变化,正在安装…")
        rc, out = await _run(
            sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS),
            timeout=300,
        )
        if rc != 0:
            await _finish_failed(
                update,
                f"代码已更新到 {after_short},但依赖安装失败,未重启:\n{out}",
            )
            return
        dep_note = "\n📦 依赖已更新。"

    action = f"切换到 {branch}" if switching else f"更新 {branch}"
    summary = (
        f"✅ {action}\n"
        f"{before_short} → {after_short}\n"
        f"---\n{changelog or '(无提交差异)'}{dep_note}\n\n"
        "♻️ 正在重启 bot,稍候片刻…"
    )
    await query.edit_message_text(truncate(summary))
    await _log_action(
        update, "bot.update", "success", f"{action}: {before_short} -> {after_short}"
    )

    # 写交接标记:重启后由新进程把这条消息改写成「重启完成」
    _write_restart_marker(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id,
        branch=branch,
        commit=after_short,
    )

    log.info("%s,%s -> %s,触发重启", action, before_short, after_short)
    # 触发 run_polling 退出 -> 进程结束 -> systemd 自动以新代码重新拉起
    context.application.stop_running()


async def _finish_failed(update: Update, msg: str) -> None:
    await update.callback_query.edit_message_text(
        truncate(f"❌ 已中止(未重启):\n{msg}")
    )
    await _log_action(update, "bot.update", "failed", truncate(msg, 400))


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


def _read_requirements() -> str:
    try:
        return REQUIREMENTS.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------- 重启交接:写标记 / 启动后回执 ----------

def _write_restart_marker(chat_id: int, message_id: int, branch: str, commit: str) -> None:
    try:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        RESTART_MARKER.write_text(
            json.dumps(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "branch": branch,
                    "commit": commit,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        log.exception("写重启标记失败")


async def notify_restart_done(application) -> None:
    """新进程启动时调用:若存在重启标记,则把原消息改写成「重启完成」。"""
    if not RESTART_MARKER.exists():
        return
    try:
        data = json.loads(RESTART_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("重启标记损坏,忽略")
        _clear_restart_marker()
        return

    text = (
        f"✅ bot 已重启完成。\n当前分支:{data.get('branch')} @ {data.get('commit')}"
    )
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    try:
        if chat_id and message_id:
            await application.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id
            )
        elif chat_id:
            await application.bot.send_message(chat_id, text)
    except TelegramError:
        # 原消息编辑失败(如过旧),退而发新消息
        try:
            if chat_id:
                await application.bot.send_message(chat_id, text)
        except TelegramError:
            log.exception("发送重启完成回执失败")
    finally:
        _clear_restart_marker()


def _clear_restart_marker() -> None:
    try:
        RESTART_MARKER.unlink(missing_ok=True)
    except OSError:
        log.exception("清理重启标记失败")


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("update", cmd_update))
    application.add_handler(MessageHandler(filters.Text([MENU_UPDATE]), cmd_update))
    application.add_handler(
        CallbackQueryHandler(cb_update_confirm, pattern=f"^{CB_UPDATE_CONFIRM}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_update_cancel, pattern=f"^{CB_UPDATE_CANCEL}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_update_pick, pattern=f"^{CB_UPDATE_PICK}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_update_branch, pattern=rf"^{CB_UPDATE_BRANCH}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_update_switch, pattern=rf"^{CB_UPDATE_SWITCH}\d+$")
    )
