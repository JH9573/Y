"""鉴权层。

通过装饰器对 handler 实施 user id 白名单校验。
非白名单用户的消息直接忽略,但记录到日志。
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes


log = logging.getLogger(__name__)


def require_auth(allowed: frozenset[int]) -> Callable:
    """生成一个仅允许 allowed 中的 user id 进入的 handler 装饰器。"""

    def decorator(
        handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable]
    ) -> Callable:
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if user is None or user.id not in allowed:
                log.warning(
                    "拒绝未授权用户访问: user_id=%s, name=%s",
                    user.id if user else None,
                    user.full_name if user else None,
                )
                return None
            return await handler(update, context)

        return wrapper

    return decorator
