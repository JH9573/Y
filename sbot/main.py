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
    add_node,
    add_panel,
    add_server,
    edit_panel,
    edit_panel_node,
    install,
    logs,
    node,
    ops,
    panel,
    panel_node,
    server,
    uninstall,
)
from .handlers.common import AppContext, CTX_KEY
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
    log.info("bot 已就绪,授权用户 %s", sorted(cfg.allowed_user_ids))


def build_application() -> Application:
    cfg = load_config()
    _setup_logging(cfg.log_level)

    crypto = Crypto(cfg.cred_encryption_key)
    ssh_client = SSHClient(crypto, timeout=cfg.ssh_timeout)
    v2board_client = V2BoardClient(crypto, timeout=cfg.ssh_timeout)
    ctx = AppContext(config=cfg, crypto=crypto, ssh=ssh_client, v2board=v2board_client)

    application = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_init(_post_init)
        .build()
    )
    application.bot_data[CTX_KEY] = ctx

    # 注册 handler。顺序无所谓,但 ConversationHandler 应先于其它 CallbackQueryHandler
    # 以确保它能优先消费进入对话的回调。
    add_server.register(application, ctx)
    add_node.register(application, ctx)
    add_panel.register(application, ctx)
    install.register(application, ctx)
    uninstall.register(application, ctx)
    server.register(application, ctx)
    ops.register(application, ctx)
    node.register(application, ctx)
    panel.register(application, ctx)
    panel_node.register(application, ctx)
    edit_panel.register(application, ctx)
    edit_panel_node.register(application, ctx)
    logs.register(application, ctx)

    _wrap_with_auth(application, cfg.allowed_user_ids)
    application.add_error_handler(_error_handler)
    return application


def main() -> None:
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
