"""bot 入口。

装配应用上下文(配置、加密、SSH 客户端)、初始化数据库,然后注册各 handler。
所有 handler 在内部通过 require_auth 装饰器实施白名单校验。
"""
from __future__ import annotations

import asyncio
import logging
from functools import wraps

from telegram.ext import (
    Application,
    ApplicationBuilder,
    BaseHandler,
    ContextTypes,
    ConversationHandler,
)

from .config import load_config
from .core.auth import require_auth
from .core.crypto import Crypto
from .core.ssh import SSHClient
from .db import crud
from .handlers import (
    add_dns_account,
    add_node,
    add_panel,
    add_release_source,
    add_remote_file,
    add_server,
    cos_config,
    dns,
    dns_record,
    edit_remote_file,
    edit_dns_account,
    edit_panel,
    edit_panel_node,
    edit_server,
    firewall,
    install,
    logs,
    menu,
    node,
    ops,
    oss_config,
    panel,
    panel_node,
    release,
    remote_config,
    server,
    uninstall,
    update_bot,
)
from .handlers.common import AppContext, CTX_KEY, is_not_modified
from .services.cloudflare_api import CloudflareClient
from .services.github_release import GitHubReleaseClient
from .services.v2board_api import V2BoardClient


log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # asyncssh 的连接日志默认很吵,提升到 WARNING
    logging.getLogger("asyncssh").setLevel(logging.WARNING)


def _wrap_handler(handler: BaseHandler, deco) -> None:
    """递归地为 handler 的 callback 套上鉴权装饰器。

    ConversationHandler 没有自己的 callback,但包含 entry_points / states / fallbacks
    三组子 handler,需要分别处理。
    """
    if isinstance(handler, ConversationHandler):
        for h in handler.entry_points:
            _wrap_handler(h, deco)
        for state_handlers in handler.states.values():
            for h in state_handlers:
                _wrap_handler(h, deco)
        for h in handler.fallbacks:
            _wrap_handler(h, deco)
        return
    cb = getattr(handler, "callback", None)
    if cb is None:
        return
    handler.callback = deco(cb)


def _wrap_with_auth(application: Application, allowed: frozenset[int]) -> None:
    """对所有已注册 handler 的 callback 套上白名单装饰器。

    比给每个 handler 函数挨个加装饰器更稳妥,且新增 handler 时不会漏。
    """
    deco = require_auth(allowed)
    for group in application.handlers.values():
        for handler in group:
            _wrap_handler(handler, deco)


async def _error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 「编辑成和原内容一样」不是错误,只是没变化。以前它会一路冒到这里,
    # 给用户回一条误导的「操作出错」。渲染路径应尽量用 common.safe_edit,
    # 这里是兜底,保证任何一处漏用都不会打扰用户。
    if is_not_modified(context.error):
        log.debug("忽略 message-is-not-modified: %s", context.error)
        return
    log.exception("未捕获异常", exc_info=context.error)
    if update and hasattr(update, "effective_message") and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "操作出错,详情请查看 bot 日志。"
            )
        except Exception:  # noqa: BLE001
            pass


async def _post_init(application: Application) -> None:
    cfg = application.bot_data[CTX_KEY].config
    await crud.init_db(cfg.db_url)
    # 操作日志只增不减,启动时清理一次过期记录
    async with crud.session() as s:
        pruned = await crud.prune_logs(s)
        await s.commit()
    if pruned:
        log.info(
            "已清理 %d 条超过 %d 天的操作日志", pruned, crud.LOG_RETENTION_DAYS
        )
    log.info("bot 已就绪,授权用户 %s", sorted(cfg.allowed_user_ids))
    # 若上次是通过「更新重启」退出的,回执一条「重启完成」
    await update_bot.notify_restart_done(application)


def build_application() -> Application:
    cfg = load_config()
    _setup_logging(cfg.log_level)

    crypto = Crypto(cfg.cred_encryption_key)
    ssh_client = SSHClient(crypto, timeout=cfg.ssh_timeout)
    v2board_client = V2BoardClient(crypto, timeout=cfg.ssh_timeout)
    cloudflare_client = CloudflareClient(crypto, timeout=cfg.ssh_timeout)
    github_client = GitHubReleaseClient(crypto, timeout=cfg.ssh_timeout)
    ctx = AppContext(
        config=cfg,
        crypto=crypto,
        ssh=ssh_client,
        v2board=v2board_client,
        cloudflare=cloudflare_client,
        github=github_client,
    )

    application = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        # PTB 默认 max_concurrent_updates=1,即所有 update 严格排队。本 bot 有
        # 分钟级的长任务(装 v2node 要 apt-get + 下载;发布要下载几百 MB 再传
        # OSS),排队意味着这期间任何人点任何按钮都没反应,连「取消」都点不动。
        # 放开到 4:长任务占一个槽,其余交互照常。DB 侧靠 SQLite WAL 承接并发
        # (见 crud._apply_sqlite_pragmas)。
        .concurrent_updates(cfg.max_concurrent_updates)
        .post_init(_post_init)
        .build()
    )
    application.bot_data[CTX_KEY] = ctx

    # 注册 handler。顺序无所谓,但 ConversationHandler 应先于其它 CallbackQueryHandler
    # 以确保它能优先消费进入对话的回调。
    add_server.register(application, ctx)
    edit_server.register(application, ctx)
    add_node.register(application, ctx)
    add_panel.register(application, ctx)
    install.register(application, ctx)
    uninstall.register(application, ctx)
    firewall.register(application, ctx)
    server.register(application, ctx)
    ops.register(application, ctx)
    node.register(application, ctx)
    panel.register(application, ctx)
    panel_node.register(application, ctx)
    edit_panel.register(application, ctx)
    edit_panel_node.register(application, ctx)
    # DNS 管理(顺序:add/edit conversation 先注册,普通 callback 后)
    add_dns_account.register(application, ctx)
    edit_dns_account.register(application, ctx)
    dns_record.register(application, ctx)
    dns.register(application, ctx)
    # 安装包分发(add/oss_config conversation 先注册,普通 callback 后)
    add_release_source.register(application, ctx)
    oss_config.register(application, ctx)
    release.register(application, ctx)
    # 远程配置(conversation 先注册,普通 callback 后)
    cos_config.register(application, ctx)
    add_remote_file.register(application, ctx)
    edit_remote_file.register(application, ctx)
    remote_config.register(application, ctx)
    logs.register(application, ctx)
    update_bot.register(application, ctx)
    # menu 必须放在所有 ConversationHandler 之后,确保对话 entry 先匹配
    menu.register(application, ctx)

    _wrap_with_auth(application, cfg.allowed_user_ids)
    application.add_error_handler(_error_handler)
    return application


def main() -> None:
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
