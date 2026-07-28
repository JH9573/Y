"""面板节点列表:分页、页码记忆、按钮与回调。"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from sbot.db import crud
from sbot.handlers import panel_node as pn
from sbot.handlers.common import CB_PANEL_NODES
from tests.conftest import FakeContext, FakeQuery, FakeUpdate


class FakeNode:
    def __init__(self, i: int):
        self.node_id = i
        self.name = f"节点{i}"
        self.available_status = i % 3
        self.show = bool(i % 2)
        self.parent_id = None
        self.protocol = "shadowsocks"


class FakePanel:
    id = 1
    name = "测试面板"


@pytest.fixture
def fake_db(monkeypatch):
    """把 panel_node 用到的几个 crud 调用换掉,只测渲染。"""
    from contextlib import asynccontextmanager

    state = {"nodes": []}

    @asynccontextmanager
    async def session():
        yield None

    async def coro(v):
        return v

    monkeypatch.setattr(crud, "session", session)
    monkeypatch.setattr(crud, "get_panel", lambda s, pid: coro(FakePanel()))
    monkeypatch.setattr(crud, "list_panel_nodes", lambda s, pid: coro(state["nodes"]))
    monkeypatch.setattr(crud, "latest_node_sync_at", lambda s, pid: coro(datetime.utcnow()))
    return state


async def render(fake_db, count, page=None, ctx=None):
    fake_db["nodes"] = [FakeNode(i) for i in range(1, count + 1)]
    q = FakeQuery()
    await pn._render_node_list(FakeUpdate(q), ctx or FakeContext(), 1, page=page)
    return q


def node_ids(q: FakeQuery) -> list[int]:
    return [int(d.split(":")[2]) for d in q.callbacks("pnldd:")]


async def test_first_page(fake_db):
    q = await render(fake_db, 137, 1)
    assert node_ids(q) == list(range(1, 21))
    assert "共 137 个" in q.text
    assert "第 1 / 7 页(第 1-20 个)" in q.text
    assert "仅显示前" not in q.text          # 旧的硬截断提示已经没有了


async def test_middle_and_last_page(fake_db):
    q = await render(fake_db, 137, 4)
    assert node_ids(q) == list(range(61, 81))
    assert q.callbacks("pnln:") == ["pnln:1:3", "pnln:1:5"]

    q = await render(fake_db, 137, 7)
    assert node_ids(q) == list(range(121, 138))
    assert q.callbacks("pnln:") == ["pnln:1:6"]


async def test_all_nodes_reachable_by_paging(fake_db):
    """以前第 51 个之后的节点在界面上没有任何入口。"""
    seen: set[int] = set()
    for page in range(1, 8):
        seen |= set(node_ids(await render(fake_db, 137, page)))
    assert seen == set(range(1, 138))


async def test_single_page_has_no_pager(fake_db):
    q = await render(fake_db, 20, 1)
    assert len(node_ids(q)) == 20
    assert q.callbacks("pnln:") == []
    assert "页" not in q.text.split("\n")[2]


async def test_empty_list_offers_sync(fake_db):
    q = await render(fake_db, 0)
    assert "暂无节点" in q.text
    assert q.callbacks("pnlsync:") == ["pnlsync:1"]


async def test_page_is_remembered_across_renders(fake_db):
    ctx = FakeContext()
    await render(fake_db, 137, 5, ctx)
    q = await render(fake_db, 137, None, ctx)      # 详情页返回列表
    assert "第 5 / 7 页" in q.text


async def test_remembered_page_clamped_after_deletion(fake_db):
    ctx = FakeContext()
    await render(fake_db, 137, 7, ctx)
    q = await render(fake_db, 41, None, ctx)       # 节点被删到只剩 3 页
    assert "第 3 / 3 页" in q.text


async def test_empty_list_resets_remembered_page(fake_db):
    ctx = FakeContext()
    await render(fake_db, 137, 6, ctx)
    await render(fake_db, 0, None, ctx)
    assert ctx.user_data[pn.PAGE_MEMO_KEY][1] == 1


async def test_banner_is_shown_above_list(fake_db):
    fake_db["nodes"] = [FakeNode(1)]
    q = FakeQuery()
    await pn._render_node_list(
        FakeUpdate(q), FakeContext(), 1, banner="✅ 已同步 1 个节点"
    )
    assert q.text.startswith("✅ 已同步 1 个节点")


async def test_repeated_identical_render_does_not_raise(fake_db):
    """连点两次「同步」时内容可能一模一样,不能冒 BadRequest 出来。"""
    fake_db["nodes"] = [FakeNode(1)]
    q = FakeQuery()
    upd, ctx = FakeUpdate(q), FakeContext()
    await pn._render_node_list(upd, ctx, 1, banner="✅ 已同步 1 个节点")
    await pn._render_node_list(upd, ctx, 1, banner="✅ 已同步 1 个节点")
    assert q.edits == 1


async def test_keyboard_within_telegram_limits(fake_db):
    q = await render(fake_db, 500, 1)
    rows = q.markup.inline_keyboard
    assert len(rows) <= 100 and all(len(r) <= 8 for r in rows)
    assert all(len(b.callback_data.encode()) <= 64 for b in q.buttons())
    assert len(q.text) <= 4096


@pytest.mark.parametrize("data,ok", [
    ("pnln:1", True), ("pnln:12:3", True),
    ("pnln:", False), ("pnln:1:2:3", False), ("pnln:a", False),
])
def test_list_callback_pattern(data, ok):
    assert bool(re.match(rf"^{CB_PANEL_NODES}\d+(:\d+)?$", data)) is ok


async def test_cb_list_nodes_parses_optional_page(fake_db):
    fake_db["nodes"] = [FakeNode(i) for i in range(1, 60)]
    for data, expect in (("pnln:1", "第 1 / 3 页"), ("pnln:1:2", "第 2 / 3 页")):
        q = FakeQuery(data)
        await pn.cb_list_nodes(FakeUpdate(q), FakeContext())
        assert expect in q.text
        assert q.answered == 1
