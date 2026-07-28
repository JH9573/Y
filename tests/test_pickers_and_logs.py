"""共用选择器(选面板 / 选节点)与操作日志界面。"""
from __future__ import annotations

import pytest
from telegram import InlineKeyboardButton

from sbot.db import crud
from sbot.handlers import logs as logs_handler
from sbot.handlers.pickers import node_label, render_node_picker, render_panel_picker
from tests.conftest import FakeContext, FakeQuery, FakeUpdate


class P:
    def __init__(self, i, api_host="https://p", api_key="k"):
        self.id = i
        self.name = f"面板{i}"
        self.api_host = api_host
        self.api_key = api_key


class N:
    def __init__(self, i, show=True, parent=None):
        self.node_id = i
        self.name = f"节点{i}"
        self.show = show
        self.parent_id = parent


BACK = InlineKeyboardButton("⬅ 返回", callback_data="back")


# ---------- 面板选择器 ----------

async def test_panel_picker_paginates(query):
    await render_panel_picker(
        query, panels=[P(i) for i in range(1, 46)], page=2,
        intro=["选面板:"], pick_cb=lambda p: f"instp:1:{p.id}",
        page_cb_prefix="inst:1:", back=BACK,
        empty_text="没有面板", missing_creds_hint="去同步凭据",
    )
    picked = query.callbacks("instp:")
    assert len(picked) == 20 and picked[0] == "instp:1:21"
    assert query.callbacks("inst:1:") == ["inst:1:1", "inst:1:3"]
    assert "第 2 / 3 页" in query.text


async def test_panel_picker_skips_panels_without_credentials(query):
    panels = [P(1), P(2, api_key=None), P(3, api_host=None)]
    await render_panel_picker(
        query, panels=panels, page=None, intro=["选面板:"],
        pick_cb=lambda p: f"x:{p.id}", page_cb_prefix="y:", back=BACK,
        empty_text="没有面板", missing_creds_hint="去同步凭据",
    )
    assert query.callbacks("x:") == ["x:1"]
    assert "面板2" in query.text and "面板3" in query.text
    assert "去同步凭据" in query.text


async def test_panel_picker_all_skipped(query):
    await render_panel_picker(
        query, panels=[P(1, api_key=None)], page=None, intro=["选面板:"],
        pick_cb=lambda p: f"x:{p.id}", page_cb_prefix="y:", back=BACK,
        empty_text="没有面板", missing_creds_hint="去同步凭据",
    )
    assert query.callbacks("x:") == []
    assert query.callbacks("back") == ["back"]


async def test_panel_picker_empty(query):
    await render_panel_picker(
        query, panels=[], page=None, intro=["选面板:"],
        pick_cb=lambda p: "x", page_cb_prefix="y:", back=BACK,
        empty_text="尚未登记任何面板。", missing_creds_hint="",
    )
    assert query.text == "尚未登记任何面板。"


# ---------- 节点选择器 ----------

async def test_node_picker_paginates_and_labels(query):
    nodes = [N(i, show=bool(i % 2), parent=1 if i == 3 else None) for i in range(1, 61)]
    await render_node_picker(
        query, nodes=nodes, page=3, intro=["选节点:"],
        pick_cb=lambda n: f"instn:1:2:{n.node_id}",
        page_cb_prefix="instp:1:2:", back=BACK, empty_text="没有节点",
    )
    picked = query.callbacks("instn:")
    assert picked[0] == "instn:1:2:41" and len(picked) == 20
    assert query.callbacks("instp:1:2:") == ["instp:1:2:2"]


def test_node_label_marks_state():
    assert node_label(N(7, show=True)) == "✅ #7 节点7"
    assert node_label(N(7, show=False)) == "❌ #7 节点7"
    assert node_label(N(7, show=True, parent=3)) == "✅🔁 #7 节点7"


async def test_node_picker_empty(query):
    await render_node_picker(
        query, nodes=[], page=None, intro=["选节点:"],
        pick_cb=lambda n: "x", page_cb_prefix="y:", back=BACK,
        empty_text="本地未缓存节点",
    )
    assert query.text == "本地未缓存节点"


# ---------- 操作日志 ----------

@pytest.fixture
async def logs_db(db):
    async with crud.session() as s:
        for i in range(45):
            await crud.add_log(s, user_id=1, server_id=None, action=f"act{i}",
                               result="success" if i % 3 else "failed",
                               detail="d" * 200)
        await s.commit()
    return db


async def test_logs_first_page(logs_db):
    q = FakeQuery()
    await logs_handler._render(FakeUpdate(q), FakeContext(), page=1, only_failed=False)
    assert "共 45 条" in q.text and "第 1 / 5 页" in q.text
    assert q.text.count("act") == 10
    # 下一页 / 切到只看失败 / 刷新本页
    assert q.callbacks("logs:") == ["logs:2:0", "logs:1:1", "logs:1:0"]
    assert "❌ 只看失败" in q.labels()
    assert "…" in q.text                                       # detail 被截断


async def test_logs_filter_toggle(logs_db):
    q = FakeQuery()
    await logs_handler._render(FakeUpdate(q), FakeContext(), page=1, only_failed=True)
    assert "失败记录(共 15 条" in q.text
    assert "✅" not in q.text
    labels = q.labels()
    assert "📋 全部" in labels          # 已在筛选态,按钮变成「回到全部」


async def test_logs_page_out_of_range_clamped(logs_db):
    q = FakeQuery()
    await logs_handler._render(FakeUpdate(q), FakeContext(), page=99, only_failed=False)
    assert "第 5 / 5 页" in q.text


async def test_logs_callback_roundtrip(logs_db):
    q = FakeQuery("logs:3:1")
    await logs_handler.cb_logs_page(FakeUpdate(q), FakeContext())
    assert "失败记录" in q.text and "第 2 / 2 页" in q.text   # 15 条失败=2页,夹回
    assert q.answered == 1


async def test_logs_empty(db):
    q = FakeQuery()
    await logs_handler._render(FakeUpdate(q), FakeContext(), page=1, only_failed=False)
    assert "没有符合条件的记录" in q.text
