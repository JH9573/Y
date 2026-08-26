"""COS 多存储桶配置页:列表、详情、切换、删除、load_cos 取活动桶。"""
from __future__ import annotations

from types import SimpleNamespace

from sbot.db import crud
from sbot.handlers import cos_config as cc
from sbot.handlers import remote_config as rc
from sbot.handlers.common import (
    CB_COS_ACTIVE,
    CB_COS_DEL,
    CB_COS_DEL_OK,
    CB_COS_DETAIL,
    CB_COS_EDIT,
    CB_MENU_COS_CFG,
)
from tests.conftest import FakeContext, FakeQuery, FakeUpdate


async def _seed(crypto, buckets=("b1", "b2")):
    """录入若干个桶,第一个自动成为当前使用。"""
    async with crud.session() as s:
        rows = [
            await crud.upsert_cos_config(
                s, region="ap-singapore", bucket=b,
                secret_id=f"SID-{b}-000000",
                secret_key=crypto.encrypt(f"sk-{b}"),
            )
            for b in buckets
        ]
        await s.commit()
    return rows


async def test_list_shows_buckets_with_active_mark(db, crypto):
    first, second = await _seed(crypto)
    q = FakeQuery(CB_MENU_COS_CFG)
    await cc.show_config(FakeUpdate(q), FakeContext())
    callbacks = q.callbacks()
    assert f"{CB_COS_DETAIL}{first.id}" in callbacks
    assert f"{CB_COS_DETAIL}{second.id}" in callbacks
    assert CB_COS_EDIT in callbacks
    labels = q.labels()
    assert any(label.startswith("✅ b1") for label in labels)
    assert any("b2" in label and not label.startswith("✅") for label in labels)


async def test_detail_of_backup_offers_activation(db, crypto):
    _first, second = await _seed(crypto)
    q = FakeQuery(f"{CB_COS_DETAIL}{second.id}")
    await cc.cb_detail(FakeUpdate(q), FakeContext())
    assert f"{CB_COS_ACTIVE}{second.id}" in q.callbacks()
    assert f"{CB_COS_DEL}{second.id}" in q.callbacks()
    assert "备用" in q.text
    assert "sk-b2" not in q.text  # SecretKey 不上屏


async def test_detail_of_active_has_no_activation_button(db, crypto):
    first, _second = await _seed(crypto)
    q = FakeQuery(f"{CB_COS_DETAIL}{first.id}")
    await cc.cb_detail(FakeUpdate(q), FakeContext())
    assert q.callbacks(CB_COS_ACTIVE) == []
    assert "当前使用" in q.text


async def test_set_active_switches_and_rerenders_list(db, crypto):
    _first, second = await _seed(crypto)
    q = FakeQuery(f"{CB_COS_ACTIVE}{second.id}")
    await cc.cb_set_active(FakeUpdate(q), FakeContext())
    assert any(label.startswith("✅ b2") for label in q.labels())
    async with crud.session() as s:
        assert (await crud.get_active_cos_config(s)).bucket == "b2"


async def test_delete_flow_removes_bucket_and_promotes_next(db, crypto):
    first, _second = await _seed(crypto)
    q = FakeQuery(f"{CB_COS_DEL}{first.id}")
    await cc.cb_del(FakeUpdate(q), FakeContext())
    assert f"{CB_COS_DEL_OK}{first.id}" in q.callbacks()  # 先二次确认
    assert "自动启用另一个" in q.text

    q2 = FakeQuery(f"{CB_COS_DEL_OK}{first.id}")
    await cc.cb_del_ok(FakeUpdate(q2), FakeContext())
    async with crud.session() as s:
        configs = await crud.list_cos_configs(s)
    assert [c.bucket for c in configs] == ["b2"]
    assert configs[0].is_active
    assert any(label.startswith("✅ b2") for label in q2.labels())


async def test_empty_list_prompts_setup(db):
    q = FakeQuery(CB_MENU_COS_CFG)
    await cc.show_config(FakeUpdate(q), FakeContext())
    assert "尚未配置" in q.text
    assert q.callbacks() == [CB_COS_EDIT]


async def test_load_cos_uses_active_bucket(db, crypto):
    _first, second = await _seed(crypto)
    async with crud.session() as s:
        await crud.set_active_cos_config(s, second.id)
        await s.commit()
    client = await cc.load_cos(SimpleNamespace(crypto=crypto))
    assert client is not None and client.bucket == "b2"


async def test_remote_file_list_shows_active_bucket(db, crypto):
    """远程配置文件列表标出当前使用的桶,多桶时能看清读写目标。"""
    await _seed(crypto, buckets=("cfg-1250000000",))
    upd = FakeUpdate(None)
    await rc.show_file_list(upd, FakeContext())
    assert "cos://cfg-1250000000" in upd.effective_message.replies[0]
