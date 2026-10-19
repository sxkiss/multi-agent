"""config.json 安全读写回归测试。

重点验证历史事故：配置文件解析失败时，任何写操作都必须拒绝覆盖，
而不是把整份配置回写成只剩当前模块的那一段。
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_store import ConfigError, load_config, save_config, update_config


def test_load_missing_returns_empty(tmp_path):
    assert load_config(str(tmp_path / "config.json")) == {}


def test_load_corrupt_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"api_key": "secret",', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_save_is_atomic_and_preserves_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    os.chmod(path, 0o640)

    save_config(str(path), {"a": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]


def test_update_config_preserves_unrelated_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_key": "secret", "keep": {"x": 1}}), encoding="utf-8")

    def _mutate(cfg):
        cfg.setdefault("added", {})["y"] = 2

    update_config(str(path), _mutate)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["api_key"] == "secret"
    assert data["keep"] == {"x": 1}
    assert data["added"] == {"y": 2}


def test_connector_save_refuses_corrupt_config(tmp_path, monkeypatch):
    from connector_hub import connectors_service as svc

    path = tmp_path / "config.json"
    original = '{"api_key": "secret", "broken":'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(svc, "CONFIG_FILE", str(path))

    with pytest.raises(ConfigError):
        svc._save_instances([{"kind": "demo"}])

    assert path.read_text(encoding="utf-8") == original


def test_sync_mcp_refuses_corrupt_mcp_config(tmp_path, monkeypatch):
    from connector_hub import connectors_service as svc

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    mcp = tmp_path / "mcp.json"
    original = '{"mcpServers": {'
    mcp.write_text(original, encoding="utf-8")
    monkeypatch.setattr(svc, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(svc, "MCP_CONFIG_FILE", str(mcp))

    with pytest.raises(ConfigError):
        svc.sync_to_mcp_config()

    assert mcp.read_text(encoding="utf-8") == original


def test_platform_ext_writes_preserve_unrelated_keys(tmp_path, monkeypatch):
    from platform_ext import gateway as gw
    from platform_ext import storage as st
    from platform_ext import teams as tm

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"api_key": "secret", "platform_ext": {"gateway": {"_auto_start": ["weixin"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "CONFIG_FILE", str(path))
    monkeypatch.setattr(tm, "CONFIG_FILE", str(path))
    monkeypatch.setattr(gw, "_config_path", lambda: str(path))

    st._save_backends([])
    tm._save_teams([])
    gw._set_gateway_cfg({"weixin": {"token": "t"}})

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["api_key"] == "secret"
    assert data["platform_ext"]["storage"]["backends"] == []
    assert data["platform_ext"]["teams"]["teams"] == []
    assert data["platform_ext"]["gateway"] == {"weixin": {"token": "t"}}
