"""更新重启:从 git 仓库拉取最新代码并重启进程。

依赖外部进程管理器(systemd `Restart=always`)在进程退出后自动拉起,从而
加载新代码。本模块只负责:拉取最新代码 -> (如有需要)安装依赖 -> 触发进程退出。

危险操作,走二次确认 + 白名单鉴权(白名单由 main 的 _wrap_with_auth 统一套上)。
"""
from __future__ import annotations

import asyncio
import logging
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import ROOT_DIR
from ..db import crud
from .common import (
    CB_UPDATE_CANCEL,
    CB_UPDATE_CONFIRM,
    MENU_UPDATE,
    truncate,
)


log = logging.getLogger(__name__)

# requirements.txt 位于 sbot 包目录下
REQUIREMENTS = ROOT_DIR / "requirements.txt"


async def _run(*args: str, cwd: str | None = None, timeout: int = 120):
    """运行子进程,返回 (returncode, 合并后的输出)。"""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"命令超时({timeout}s): {' '.join(args)}"
    return proc.returncode, out.decode(errors="replace").strip()


async def _git(*args: str, cwd: str, timeout: int = 120):
    return await _run("git", *args, cwd=cwd, timeout=timeout)


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """展示当前版本并请求二次确认。"""
    repo = str(ROOT_DIR.parent)
    rc_branch, branch = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    rc_head, head = await _git("rev-parse", "--short", "HEAD", cwd=repo)
    if rc_branch != 0 or rc_head != 0:
        await update.effective_message.reply_text(
            "无法读取当前 git 状态,可能不是 git 仓库,已中止。"
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("确认更新并重启", callback_data=CB_UPDATE_CONFIRM),
                InlineKeyboardButton("取消", callback_data=CB_UPDATE_CANCEL),
            ]
        ]
    )
    await update.effective_message.reply_text(
        f"⚠️ 将从 origin/{branch} 拉取最新代码并重启 bot。\n"
        f"当前版本:{branch} @ {head}\n\n"
        "更新过程中 bot 会短暂离线,确认继续?",
        reply_markup=kb,
    )


async def cb_update_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("已取消更新。")


async def cb_update_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    repo = str(ROOT_DIR.parent)

    await query.edit_message_text("⏳ 正在拉取最新代码…")

    # 当前分支与更新前的 commit
    rc, branch = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if rc != 0 or branch in ("", "HEAD"):
        branch = "main"  # detached 或读取失败时回退到主分支
    _, before = await _git("rev-parse", "HEAD", cwd=repo)

    # 拉取
    rc, out = await _git("fetch", "origin", branch, cwd=repo)
    if rc != 0:
        await _finish_failed(update, query, branch, f"git fetch 失败:\n{out}")
        return

    # 记录依赖文件内容,用于判断是否需要重装依赖
    req_before = _read_requirements()

    rc, out = await _git("reset", "--hard", f"origin/{branch}", cwd=repo)
    if rc != 0:
        await _finish_failed(update, query, branch, f"git reset 失败:\n{out}")
        return

    _, after = await _git("rev-parse", "HEAD", cwd=repo)
    _, after_short = await _git("rev-parse", "--short", "HEAD", cwd=repo)
    _, before_short = await _git("rev-parse", "--short", before, cwd=repo)

    if before == after:
        await _log_action(update, "bot.update", "success", f"已是最新 {after_short}")
        await query.edit_message_text(
            f"✅ 已是最新版本({branch} @ {after_short}),无需重启。"
        )
        return

    # 更新了哪些提交
    _, changelog = await _git(
        "log", "--oneline", "--no-decorate", f"{before}..{after}", cwd=repo
    )

    # 依赖变化则重装
    dep_note = ""
    if _read_requirements() != req_before:
        await query.edit_message_text("📦 依赖有变化,正在安装…")
        rc, out = await _run(
            sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS),
            cwd=repo, timeout=300,
        )
        if rc != 0:
            await _finish_failed(
                update, query, branch,
                f"代码已更新到 {after_short},但依赖安装失败,未重启:\n{out}",
            )
            return
        dep_note = "\n📦 依赖已更新。"

    summary = (
        f"✅ 更新完成:{branch}\n"
        f"{before_short} → {after_short}\n"
        f"---\n{changelog or '(无提交差异)'}{dep_note}\n\n"
        "♻️ 正在重启 bot,稍候片刻…"
    )
    await query.edit_message_text(truncate(summary))
    await _log_action(
        update, "bot.update", "success", f"{before_short} -> {after_short}"
    )

    log.info("更新完成 %s -> %s,触发重启", before_short, after_short)
    # 触发 run_polling 退出 -> 进程结束 -> systemd 自动以新代码重新拉起
    context.application.stop_running()


async def _finish_failed(update, query, branch, msg: str) -> None:
    await query.edit_message_text(truncate(f"❌ 更新失败,已中止(未重启):\n{msg}"))
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


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("update", cmd_update))
    application.add_handler(
        MessageHandler(filters.Text([MENU_UPDATE]), cmd_update)
    )
    application.add_handler(
        CallbackQueryHandler(cb_update_confirm, pattern=f"^{CB_UPDATE_CONFIRM}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_update_cancel, pattern=f"^{CB_UPDATE_CANCEL}$")
    )
