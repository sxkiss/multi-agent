"""连接器字段对齐回归测试（前端 catalog ↔ 后端 normalize_credentials）。

背景
----
`connector_hub/catalog.json` 曾出现两类字段缺失：

1. 5 个 oauth2 连接器（qcc / tencent-ardot / notion / openalex / dida365）
   完全没有 `credential_fields` 键，导致前端 `openModal()` 在「手动粘贴 Token」
   入口渲染不出输入框；
2. `tencent-ima` 只声明了 `api_key`，但 `normalize_credentials()` 强制要求
   `client_id`，用户在前端根本无从填写，该连接器实际不可创建。

修复方式：catalog 补齐字段；前端改为以 `credential_fields` 为准而非硬编码 kind。
本测试把这些契约锁死，防止回归。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CATALOG_PATH = _ROOT / "connector_hub" / "catalog.json"
_SERVICE_PATH = _ROOT / "connector_hub" / "connectors_service.py"
_HTML_PATH = _ROOT / "static" / "connectors.html"


@pytest.fixture(scope="module")
def catalog() -> dict[str, Any]:
    with _CATALOG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def service_source() -> str:
    return _SERVICE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html_source() -> str:
    return _HTML_PATH.read_text(encoding="utf-8")


def _connector(catalog: dict, kind: str) -> dict[str, Any]:
    entry = next((c for c in catalog.get("connectors", []) if c["kind"] == kind), None)
    assert entry is not None, f"catalog 中找不到连接器 {kind}"
    return entry


def _field_keys(entry: dict) -> set[str]:
    return {
        f.get("key")
        for f in (entry.get("credential_fields") or [])
        if isinstance(f, dict)
    }


# ── 1. catalog 完整性 ─────────────────────────────────────────────


class TestCatalogCompleteness:
    def test_no_connector_missing_credential_fields(self, catalog: dict) -> None:
        """所有连接器都必须声明 credential_fields 键（即使是空数组）。

        键缺失会让 `to_api()` 输出 None，前端 openModal 走 fallback 分支，
        本该显示字段的连接器就渲染不出输入框。
        """
        missing = [
            c["kind"]
            for c in catalog.get("connectors", [])
            if "credential_fields" not in c
        ]
        assert not missing, (
            "下列连接器未声明 credential_fields 键，请在 catalog.json 补齐：" f"{missing}"
        )

    def test_agently_cli_has_empty_list_not_missing(self, catalog: dict) -> None:
        """agently-cli 走 CLI 设备码授权、无表单字段：应显式声明 [] 而非省略。"""
        entry = _connector(catalog, "agently-cli")
        assert "credential_fields" in entry, "agently-cli 必须显式声明 credential_fields"
        assert entry["credential_fields"] == [], (
            "agently-cli 的 credential_fields 应为空数组 []"
        )


# ── 2. oauth2 连接器必须有 manual_token ──────────────────────────


_OAUTH2_KINDS = ["qcc", "tencent-ardot", "notion", "openalex", "dida365"]


class TestOAuth2ManualToken:
    @pytest.mark.parametrize("kind", _OAUTH2_KINDS)
    def test_oauth2_has_manual_token_field(self, catalog: dict, kind: str) -> None:
        """oauth2 连接器需在 credential_fields 里声明 manual_token，
        否则前端「手动粘贴 Token」入口渲染不出输入框。
        """
        entry = _connector(catalog, kind)
        assert entry["auth_kind"] == "oauth2", f"{kind} 的 auth_kind 不是 oauth2"
        assert "manual_token" in _field_keys(entry), (
            f"oauth2 连接器 {kind} 缺少 manual_token 字段，"
            f"前端手动粘贴入口无法渲染。当前字段: {sorted(_field_keys(entry))}"
        )

    def test_frontend_does_not_hardcode_manual_kinds(self, html_source: str) -> None:
        """前端不得硬编码「哪些 oauth2 支持手动粘贴」，应由 credential_fields 决定。"""
        hardcoded = re.search(
            r"e\.kind\s*===\s*['\"]openalex['\"][^;]{0,80}?"
            r"e\.kind\s*===\s*['\"]qcc['\"]",
            html_source,
        )
        assert hardcoded is None, (
            "static/connectors.html 仍硬编码 openalex/qcc 手动粘贴判断，"
            "应改为检查 credential_fields 是否含 manual_token"
        )

    def test_frontend_renders_manual_field_from_catalog(self, html_source: str) -> None:
        """前端「手动粘贴」分支应读取 manual_token 字段而不是写死标签。"""
        assert "manual_token" in html_source, (
            "static/connectors.html 未引用 manual_token 字段"
        )
        assert "credential_fields" in html_source, (
            "static/connectors.html 未基于 credential_fields 渲染"
        )


# ── 3. credential_fields 结构 & 与后端要求对齐 ───────────────────


class TestFieldAlignment:
    def test_every_connector_fields_is_structurally_valid(self, catalog: dict) -> None:
        """credential_fields 每一项必须是 dict 且含 key / label。"""
        bad: list[str] = []
        for c in catalog.get("connectors", []):
            for idx, f in enumerate(c.get("credential_fields") or []):
                if not isinstance(f, dict):
                    bad.append(f"{c['kind']}[{idx}] 不是 dict")
                    continue
                if not f.get("key"):
                    bad.append(f"{c['kind']}[{idx}] 缺 key")
                if not f.get("label"):
                    bad.append(f"{c['kind']}[{idx}] 缺 label")
        assert not bad, "credential_fields 结构异常:\n" + "\n".join(bad)

    @pytest.mark.parametrize(
        "kind,auth_kind,required_keys",
        [
            # personal_token
            ("tencent-docs", "personal_token", ["token"]),
            ("tencent-meeting", "personal_token", ["token"]),
            ("tencent-weiyun", "personal_token", ["token"]),
            ("youdao-note", "personal_token", ["token"]),
            # api_key（IMA 必须同时有 client_id，否则后端校验必失败）
            ("tencent-ima", "api_key", ["api_key", "client_id"]),
            ("tencent-lexiang", "api_key", ["api_key", "company_from"]),
            ("didi", "api_key", ["api_key"]),
            # api_key + CLI 专用字段（不声明通用 api_key）
            ("feishu-cli", "api_key", ["app_id", "app_secret"]),
            ("wecom-cli", "api_key", ["bot_id", "bot_secret"]),
            # imap
            ("qq-mail", "imap_app_password", ["email", "password"]),
            # oauth2 —— 均须含 manual_token
            ("qcc", "oauth2", ["manual_token"]),
            ("tencent-ardot", "oauth2", ["manual_token"]),
            ("notion", "oauth2", ["manual_token"]),
            ("openalex", "oauth2", ["manual_token"]),
            ("dida365", "oauth2", ["manual_token"]),
            # custom_fields
            ("agently-cli", "custom_fields", []),
            ("obsidian-cli", "custom_fields", ["vault"]),
        ],
    )
    def test_connector_has_required_field_keys(
        self,
        catalog: dict,
        kind: str,
        auth_kind: str,
        required_keys: list[str],
    ) -> None:
        """已知连接器必须声明后端实际读取的字段 key，防止前后端脱节。"""
        entry = _connector(catalog, kind)
        assert entry["auth_kind"] == auth_kind, (
            f"{kind} 的 auth_kind 预期 {auth_kind}，实际 {entry['auth_kind']}"
        )
        actual = _field_keys(entry)
        for k in required_keys:
            assert k in actual, (
                f"{kind} (auth_kind={auth_kind}) 的 credential_fields 应含 key={k}，"
                f"当前只有 {sorted(actual)}"
            )


# ── 4. 后端强校验字段必须在前端有对应输入项 ─────────────────────


class TestBackendRequiredFieldsExposed:
    """解析 normalize_credentials 里的硬性 raise 分支，确认其依赖字段已在 catalog 声明。"""

    # kind -> 该连接器在 normalize_credentials 中被强校验的字段
    _HARD_REQUIRED: dict[str, list[str]] = {
        "tencent-ima": ["client_id"],
        "tencent-lexiang": ["company_from"],
        "feishu-cli": ["app_id", "app_secret"],
        "wecom-cli": ["bot_id", "bot_secret"],
    }

    @pytest.mark.parametrize("kind", sorted(_HARD_REQUIRED))
    def test_hard_required_field_declared(
        self, catalog: dict, service_source: str, kind: str
    ) -> None:
        """后端 raise ValueError 强校验的字段，前端 catalog 必须有对应输入项，
        否则用户永远无法通过校验（历史 bug：tencent-ima 缺 client_id）。
        """
        entry = _connector(catalog, kind)
        actual = _field_keys(entry)
        for key in self._HARD_REQUIRED[kind]:
            # 后端源码中应确实读取该字段
            assert re.search(rf'creds\.get\("{re.escape(key)}"\)', service_source), (
                f"预期 normalize_credentials 读取 {key}，但源码未找到"
            )
            assert key in actual, (
                f"{kind} 后端强校验字段 {key} 未在 catalog.credential_fields 声明，"
                f"前端无输入框 → 该连接器无法创建。当前字段: {sorted(actual)}"
            )
