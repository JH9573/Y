"""添加 / 编辑面板节点的对话流:拆包后的结构、字段流程、payload 组装、父节点分页。"""
from __future__ import annotations

import pytest

from sbot.db import crud
from sbot.handlers.edit_panel_node import conversation, options, state, steps_basic
from tests.conftest import FakeContext, FakeQuery, FakeUpdate


class FakeApp:
    def __init__(self):
        self.handlers = []

    def add_handler(self, h):
        self.handlers.append(h)


@pytest.fixture(scope="module")
def conv():
    app = FakeApp()
    conversation.register(app, None)
    return app.handlers[0]


# ---------- 拆包后的结构 ----------

def test_all_states_have_handlers(conv):
    assert len(conv.states) == 28
    assert all(handlers for handlers in conv.states.values())


def test_entry_points_and_fallbacks(conv):
    assert len(conv.entry_points) == 2
    assert {h.callback.__name__ for h in conv.entry_points} == {
        "cb_add_entry", "cb_edit_entry"
    }
    assert {h.callback.__name__ for h in conv.fallbacks} == {"cmd_cancel"}


def test_every_flow_field_has_a_prompt():
    """_advance 按字段名查表跳下一步,表里缺一项就会 KeyError。"""
    fields = {f for flow in options._FLOW_BY_PROTOCOL.values() for f in flow}
    assert fields <= set(state.PROMPT_BY_FIELD), fields - set(state.PROMPT_BY_FIELD)


def test_prompt_registry_is_complete():
    assert len(state.PROMPT_BY_FIELD) == 18
    assert state.PROMPT_BY_FIELD["confirm"].__name__ == "_prompt_confirm"


def test_module_layering_has_no_cycles():
    """options <- state <- steps_* <- conversation,单向依赖。"""
    import ast
    import pathlib

    pkg = pathlib.Path("sbot/handlers/edit_panel_node")
    allowed = {
        "options": set(),
        "state": {"options"},
        "steps_basic": {"options", "state"},
        "steps_proto": {"options", "state"},
        "steps_tls": {"options", "state"},
        "confirm": {"options", "state"},
        "conversation": {"options", "state", "steps_basic", "steps_proto",
                         "steps_tls", "confirm"},
    }
    for mod, ok in allowed.items():
        tree = ast.parse((pkg / f"{mod}.py").read_text(encoding="utf-8"))
        siblings = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
        }
        assert siblings <= ok, f"{mod} 多出了依赖: {siblings - ok}"


@pytest.mark.parametrize("protocol,expect", [
    ("shadowsocks", "cipher"),
    ("vless", "flow"),
    ("hysteria2", "up_mbps"),
])
def test_protocol_specific_steps_in_flow(protocol, expect):
    assert expect in options._FLOW_BY_PROTOCOL[protocol]


def test_anytls_and_hysteria2_skip_tls_question():
    # 服务端会强制 tls=1,不该再问用户
    assert "tls" not in options._FLOW_BY_PROTOCOL["anytls"]
    assert "tls" not in options._FLOW_BY_PROTOCOL["hysteria2"]


def test_flow_for_falls_back_to_shadowsocks():
    data = {"values": {"protocol": "不认识"}, "initial": {}}
    assert state._flow_for(data) == options._FLOW_BY_PROTOCOL["shadowsocks"]


# ---------- payload 组装 ----------

def _data(protocol, **values):
    base = {
        "name": "香港01", "host": "h.example.com", "port": 443,
        "server_port": 8443, "rate": 1.0, "group_id": [1],
        "network": "tcp", "tls": 1, "cipher": "aes-128-gcm",
    }
    base.update(values)
    base["protocol"] = protocol
    return {"mode": "add", "values": base, "initial": {}}


def test_payload_shadowsocks():
    from sbot.handlers.edit_panel_node.confirm import _compose_payload

    p = _compose_payload(_data("shadowsocks"))
    assert p["cipher"] == "aes-128-gcm" and p["protocol"] == "shadowsocks"
    assert p["disable_sni"] == 0 and p["show"] == 1


def test_payload_hysteria2_forces_tls_and_network():
    from sbot.handlers.edit_panel_node.confirm import _compose_payload

    p = _compose_payload(_data("hysteria2", tls=0, up_mbps=100, down_mbps=200))
    assert p["tls"] == 1                    # 服务端强制,bot 先对齐
    assert p["network"] == "tcp"            # v2board 校验 network 必填
    assert p["up_mbps"] == 100 and p["down_mbps"] == 200


def test_payload_vless_drops_empty_flow():
    from sbot.handlers.edit_panel_node.confirm import _compose_payload

    assert "flow" not in _compose_payload(_data("vless", flow=""))
    assert _compose_payload(_data("vless", flow="xtls-rprx-vision"))["flow"] == \
        "xtls-rprx-vision"


def test_payload_edit_mode_keeps_whitelisted_initial_fields():
    from sbot.handlers.edit_panel_node.confirm import _compose_payload

    data = _data("shadowsocks")
    data["mode"] = "edit"
    data["initial"] = {"tags": ["a"], "route_id": [2], "不在白名单": "x"}
    p = _compose_payload(data)
    assert p["tags"] == ["a"] and p["route_id"] == [2]
    assert "不在白名单" not in p


def test_payload_advanced_overrides_everything():
    from sbot.handlers.edit_panel_node.confirm import _compose_payload

    p = _compose_payload(_data("shadowsocks", advanced={"rate": 9.9, "tags": ["x"]}))
    assert p["rate"] == 9.9 and p["tags"] == ["x"]


def test_all_payload_keys_are_accepted_by_v2board():
    from sbot.handlers.edit_panel_node.confirm import _compose_payload

    for protocol in options._FLOW_BY_PROTOCOL:
        p = _compose_payload(_data(protocol, up_mbps=1, down_mbps=2))
        unknown = set(p) - options.SAVE_FIELDS
        assert not unknown, f"{protocol} 多出字段: {unknown}"


# ---------- 父节点选择:分页取代 30 条截断 ----------

class FakeNode:
    def __init__(self, i, parent=None):
        self.node_id = i
        self.name = f"节点{i}"
        self.show = True
        self.parent_id = parent


@pytest.fixture
def parent_candidates(monkeypatch):
    from contextlib import asynccontextmanager

    nodes = [FakeNode(i) for i in range(1, 76)]
    nodes.append(FakeNode(999, parent=1))     # 已是子节点,不能当父节点

    @asynccontextmanager
    async def session():
        yield None

    async def coro(v):
        return v

    monkeypatch.setattr(crud, "session", session)
    monkeypatch.setattr(crud, "list_panel_nodes", lambda s, pid: coro(nodes))
    return nodes


def _ctx():
    ctx = FakeContext()
    ctx.user_data[options.KEY] = {
        "panel_id": 1, "mode": "add", "values": {}, "initial": {},
    }
    return ctx


async def test_parent_picker_paginates(parent_candidates):
    q, ctx = FakeQuery(), _ctx()
    assert await steps_basic._prompt_parent(FakeUpdate(q), ctx) == options.PARENT
    picks = [c for c in q.callbacks("pnlsave:p:") if c != "pnlsave:p:none"]
    assert len(picks) == 20                       # 以前是固定前 30 且没有下文
    assert "第 1 / 4 页" in q.text
    assert q.callbacks("pnlsave:pp:") == ["pnlsave:pp:2"]


async def test_parent_picker_excludes_relay_nodes(parent_candidates):
    q, ctx = FakeQuery(), _ctx()
    seen = []
    for page in range(1, 5):
        ctx.user_data[options.KEY]["parent_page"] = page
        q = FakeQuery()
        await steps_basic._prompt_parent(FakeUpdate(q), ctx)
        seen += [c for c in q.callbacks("pnlsave:p:") if c != "pnlsave:p:none"]
    assert "pnlsave:p:999" not in seen            # 已有父节点的不作候选
    assert len(seen) == 75


async def test_parent_page_callback_redraws_without_selecting(parent_candidates):
    ctx = _ctx()
    q = FakeQuery("pnlsave:pp:3")
    assert await steps_basic.step_parent(FakeUpdate(q), ctx) == options.PARENT
    assert "第 3 / 4 页" in q.text
    assert "parent_id" not in ctx.user_data[options.KEY]["values"]


@pytest.mark.parametrize("data,expected", [
    ("pnlsave:p:none", None),
    ("pnlsave:p:12", 12),
])
async def test_parent_selection_sets_value(parent_candidates, monkeypatch,
                                           data, expected):
    # 选定后会 _advance 到下一步(要真的 AppContext),这里只验证取值
    advanced = []

    async def fake_advance(field, update, context):
        advanced.append(field)
        return "next"

    monkeypatch.setattr(steps_basic, "_advance", fake_advance)
    ctx = _ctx()
    q = FakeQuery(data)
    assert await steps_basic.step_parent(FakeUpdate(q), ctx) == "next"
    assert ctx.user_data[options.KEY]["values"]["parent_id"] == expected
    assert advanced == ["parent"]


def test_parent_state_pattern_accepts_paging(conv):
    import re

    pattern = conv.states[options.PARENT][0].pattern
    for good in ("pnlsave:p:5", "pnlsave:p:none", "pnlsave:pp:2", "pnlsave:keep"):
        assert re.match(pattern, good), good
    assert not re.match(pattern, "pnlsave:pp:x")
