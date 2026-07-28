"""共用 fixture。

这些测试不碰 Telegram 网络,也不碰真服务器:
- DB 用临时文件里的真 SQLite(WAL / 迁移 / 级联都要真库才测得出)
- Telegram 侧用 FakeQuery / FakeUpdate 假对象
- SSH 侧在本机起一个真的 asyncssh 服务端(见 test_ssh.py)
"""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from sbot.core.crypto import Crypto
from sbot.db import crud


@pytest_asyncio.fixture
async def db(tmp_path):
    """每个用例一个全新的 SQLite 库。"""
    path = tmp_path / "test.db"
    await crud.init_db(f"sqlite+aiosqlite:///{path}")
    yield path
    if crud._engine is not None:
        await crud._engine.dispose()
    crud._engine = None
    crud._session_factory = None


@pytest.fixture
def crypto():
    from cryptography.fernet import Fernet

    return Crypto(Fernet.generate_key().decode())


class FakeQuery:
    """够用的 CallbackQuery 替身。

    edit_message_text 在内容与上次完全相同时抛 BadRequest,和 Telegram 行为一致,
    这样 safe_edit 的取舍才测得出来。
    """

    def __init__(self, data: str | None = None):
        self.data = data
        self.text: str | None = None
        self.markup: Any = None
        self.edits = 0
        self.answered = 0

    async def answer(self, *a, **kw):
        self.answered += 1

    async def edit_message_text(self, text, reply_markup=None, **kw):
        from telegram.error import BadRequest

        if text == self.text and repr(reply_markup) == repr(self.markup):
            raise BadRequest("Message is not modified: specified new message content")
        self.text, self.markup = text, reply_markup
        self.edits += 1

    # ----- 断言辅助 -----

    def buttons(self) -> list[Any]:
        if self.markup is None:
            return []
        return [b for row in self.markup.inline_keyboard for b in row]

    def callbacks(self, prefix: str = "") -> list[str]:
        return [
            b.callback_data for b in self.buttons()
            if b.callback_data and b.callback_data.startswith(prefix)
        ]

    def labels(self) -> list[str]:
        return [b.text for b in self.buttons()]


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.text: str | None = None

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kw):
        from telegram.error import BadRequest

        if text == self.text:
            raise BadRequest("Message is not modified")
        self.text = text


class FakeUpdate:
    def __init__(self, query: FakeQuery | None = None, user_id: int = 1):
        self.callback_query = query
        self.effective_message = FakeMessage()
        self.effective_user = type("U", (), {"id": user_id, "full_name": "t"})()
        self.effective_chat = type("C", (), {"id": 42})()


class FakeContext:
    def __init__(self, **bot_data):
        self.user_data: dict = {}
        self.application = type("A", (), {"bot_data": bot_data})()


@pytest.fixture
def query():
    return FakeQuery()


@pytest.fixture
def update(query):
    return FakeUpdate(query)


@pytest.fixture
def context():
    return FakeContext()
