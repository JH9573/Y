"""services 层:v2board 字段映射与校验、远程 config.json 的增删与回滚。"""
from __future__ import annotations

import json

import pytest

from sbot.core.ssh import SSHError
from sbot.services.v2board_api import (
    V2BoardAPIError,
    v2node_to_db_row,
    validate_base_url,
    validate_email,
    validate_secure_path,
)
from sbot.services.v2node_config import (
    CONFIG_PATH,
    NodeEntry,
    V2NodeConfigError,
    add_node_to_config,
    read_remote_nodes,
    remove_node_from_config,
    serialize_config,
)

# ---------- v2board 校验 ----------

def test_validate_base_url_normalizes():
    assert validate_base_url("  https://p.example.com/  ") == "https://p.example.com"


@pytest.mark.parametrize("bad", ["p.example.com", "ftp://x", "", "http s://x"])
def test_validate_base_url_rejects(bad):
    with pytest.raises(V2BoardAPIError):
        validate_base_url(bad)


def test_validate_secure_path():
    assert validate_secure_path("/admin/") == "admin"
    for bad in ("", "a/b", "a b"):
        with pytest.raises(V2BoardAPIError):
            validate_secure_path(bad)


def test_validate_email():
    assert validate_email(" a@b.co ") == "a@b.co"
    for bad in ("a@b", "ab.co", "a b@c.co"):
        with pytest.raises(V2BoardAPIError):
            validate_email(bad)


# ---------- 节点映射 ----------

def test_v2node_to_db_row_full():
    row = v2node_to_db_row({
        "id": "12", "name": "香港01", "protocol": "vless", "host": "h.example.com",
        "port": "443", "server_port": "8443", "network": "ws", "tls": "2",
        "rate": 1.5, "sort": "3", "show": 1, "parent_id": 4,
        "available_status": 2, "tls_settings": {"server_name": "x"},
    })
    assert row["node_id"] == 12 and row["port"] == 443 and row["server_port"] == 8443
    assert row["tls"] == 2 and row["sort"] == 3 and row["parent_id"] == 4
    assert row["rate"] == "1.5" and row["show"] is True
    assert json.loads(row["raw_json"])["tls_settings"] == {"server_name": "x"}


def test_v2node_to_db_row_defaults():
    row = v2node_to_db_row({"id": 1})
    assert row["name"] == "" and row["protocol"] == "" and row["host"] == ""
    assert row["port"] == 0 and row["server_port"] == 0
    assert row["network"] is None and row["tls"] is None and row["rate"] is None
    assert row["show"] is False and row["parent_id"] is None


@pytest.mark.parametrize("raw", [0, "0", "", None])
def test_v2node_parent_zero_means_no_parent(raw):
    assert v2node_to_db_row({"id": 1, "parent_id": raw})["parent_id"] is None


def test_v2node_raw_json_survives_non_serializable():
    row = v2node_to_db_row({"id": 1, "weird": {1, 2}})
    assert "weird" in json.loads(row["raw_json"])


# ---------- 远程 config.json ----------

class FakeSSH:
    """够用的 SSH 替身:内存里存文件,记录跑过的命令。"""

    def __init__(self, files: dict[str, str] | None = None, healthy=True):
        self.files = dict(files or {})
        self.commands: list[str] = []
        self.healthy = healthy

    async def run(self, server, command, *, timeout=None, check=False):
        from sbot.core.ssh import CommandResult

        self.commands.append(command)
        if command.startswith("cp -f "):
            _, _, src, dst = command.split()
            self.files[dst] = self.files.get(src, "")
        if "is-active" in command:
            return CommandResult(0, "active" if self.healthy else "failed", "")
        return CommandResult(0, "", "")

    async def read_file(self, server, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, server, path, content):
        self.files[path] = content


def _cfg(*nodes) -> str:
    return serialize_config({"Log": {"Level": "warning"}, "Nodes": list(nodes)})


ENTRY = NodeEntry(api_host="https://p", node_id=7, api_key="k")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("sbot.services.v2node_config.asyncio.sleep", instant)


async def test_add_node_writes_backup_then_config():
    ssh = FakeSSH({CONFIG_PATH: _cfg()})
    ok, msg = await add_node_to_config(ssh, object(), ENTRY)
    assert ok, msg
    assert json.loads(ssh.files[CONFIG_PATH])["Nodes"] == [ENTRY.to_dict()]
    assert any(c.startswith("cp -f") for c in ssh.commands)      # 先备份
    assert any("restart" in c for c in ssh.commands)
    # 备份内容是改动前的
    assert json.loads(ssh.files["/etc/v2node/config.json.bak"])["Nodes"] == []


async def test_add_node_rejects_duplicate():
    ssh = FakeSSH({CONFIG_PATH: _cfg(ENTRY.to_dict())})
    with pytest.raises(V2NodeConfigError, match="已存在"):
        await add_node_to_config(ssh, object(), ENTRY)


async def test_add_node_rolls_back_when_service_unhealthy():
    before = _cfg()
    ssh = FakeSSH({CONFIG_PATH: before}, healthy=False)
    ok, msg = await add_node_to_config(ssh, object(), ENTRY)
    assert not ok and "回滚" in msg
    # 回滚 = 用 .bak 覆盖回去
    assert json.loads(ssh.files[CONFIG_PATH])["Nodes"] == []


async def test_remove_node():
    other = NodeEntry(api_host="https://p", node_id=9, api_key="k2").to_dict()
    ssh = FakeSSH({CONFIG_PATH: _cfg(ENTRY.to_dict(), other)})
    ok, _ = await remove_node_from_config(ssh, object(), "https://p", 7)
    assert ok
    assert json.loads(ssh.files[CONFIG_PATH])["Nodes"] == [other]


async def test_remove_missing_node_reports():
    ssh = FakeSSH({CONFIG_PATH: _cfg()})
    with pytest.raises(V2NodeConfigError, match="不存在"):
        await remove_node_from_config(ssh, object(), "https://p", 7)


async def test_config_must_be_valid_json():
    ssh = FakeSSH({CONFIG_PATH: "{not json"})
    with pytest.raises(V2NodeConfigError, match="解析失败"):
        await add_node_to_config(ssh, object(), ENTRY)


async def test_nodes_field_must_be_array():
    ssh = FakeSSH({CONFIG_PATH: json.dumps({"Nodes": "oops"})})
    with pytest.raises(V2NodeConfigError, match="不是数组"):
        await add_node_to_config(ssh, object(), ENTRY)


async def test_read_remote_nodes_tolerates_junk():
    ssh = FakeSSH({CONFIG_PATH: json.dumps({"Nodes": [
        ENTRY.to_dict(), "不是对象", {"ApiHost": "x"},          # 缺字段
    ]})})
    got = await read_remote_nodes(ssh, object())
    assert [n.node_id for n in got] == [7]


async def test_read_remote_nodes_missing_file_is_empty():
    assert await read_remote_nodes(FakeSSH(), object()) == []


def test_serialize_keeps_chinese_readable():
    out = serialize_config({"Name": "香港"})
    assert "香港" in out and out.startswith("{\n    ")


def test_node_entry_roundtrip():
    d = ENTRY.to_dict()
    assert NodeEntry.from_dict(d) == ENTRY
    with pytest.raises(V2NodeConfigError):
        NodeEntry.from_dict({"ApiHost": "x"})


async def test_write_failure_triggers_rollback(monkeypatch):
    ssh = FakeSSH({CONFIG_PATH: _cfg()})

    async def boom(server, path, content):
        raise SSHError("磁盘满了")

    ssh.write_file = boom
    with pytest.raises(SSHError):
        await add_node_to_config(ssh, object(), ENTRY)
    assert any(c.startswith("cp -f /etc/v2node/config.json.bak") for c in ssh.commands)
