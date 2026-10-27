"""从 WorkBuddy CDN 下载最新连接器 ZIP 并更新 connector_hub/catalog.json。

数据源：https://static.workbuddy.cn/connectors-config-v2/connectors-config.zip
缓存位置：connector_hub/../market_cache/connectors-config-v2.zip（~33MB）

用法：
    from connector_hub.sync_workbuddy import sync_catalog
    res = await sync_catalog()  # 返回 {"ok": True, "added": N, "total": M}
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from typing import Any

logger = logging.getLogger("connector-hub.sync")

_ZIP_URL = "https://static.workbuddy.cn/connectors-config-v2/connectors-config.zip"
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_ZIP = os.path.join(_HERE, "..", "market_cache", "connectors-config-v2.zip")


def sync_catalog() -> dict[str, Any]:
    """从 WorkBuddy CDN 下载最新 ZIP 并更新 catalog.json。

    流程：
      1. 检查缓存 ZIP（< 30MB 则重下），保存到 market_cache/
      2. 解压到临时目录
      3. 复用 import_workbuddy 的映射逻辑，幂等合并到现有 catalog
      4. 写入 catalog.json.new 再 rename 替换
      5. 清理临时目录

    返回：{"ok": True, "added": N, "total": M}
    """
    import connector_hub.import_workbuddy as iwb

    # 1. 下载 ZIP（有缓存且 >=30MB 则跳过）
    zip_path = _CACHE_ZIP
    need_download = False
    try:
        need_download = os.path.getsize(zip_path) < 30_000_000
    except Exception:  # noqa: BLE001
        need_download = True

    if need_download:
        logger.info("从 CDN 下载: %s", _ZIP_URL)
        import httpx
        with httpx.Client(timeout=120) as client:
            r = client.get(_ZIP_URL)
            r.raise_for_status()
            os.makedirs(os.path.dirname(zip_path), exist_ok=True)
            tmp_zip = zip_path + ".tmp"
            with open(tmp_zip, "wb") as f:
                f.write(r.content)
            os.replace(tmp_zip, zip_path)
        logger.info("ZIP 已缓存: %s (%d bytes)", zip_path, os.path.getsize(zip_path))

    # 2. 解压到临时目录
    tmp_dir = tempfile.mkdtemp(prefix="wb_conn_sync_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp_dir)
        extracted = os.path.join(tmp_dir, "connectors-config")
        if not os.path.isdir(extracted):
            extracted = tmp_dir
        logger.info("解压到 %s", extracted)

        # 3. 复用 import_workbuddy：patch _SRC_ROOT 后调用 build_new_catalog
        orig_src_root = iwb._SRC_ROOT
        orig_src_catalog = iwb._SRC_CATALOG
        orig_src_connectors_dir = iwb._SRC_CONNECTORS_DIR
        iwb._SRC_ROOT = extracted
        iwb._SRC_CATALOG = os.path.join(extracted, ".codebuddy-connector", "connectors.json")
        iwb._SRC_CONNECTORS_DIR = os.path.join(extracted, "connectors")

        existing = iwb.load_existing_catalog()
        wb = iwb._load_wb_catalog()
        wb_entries = [iwb._to_catalog_entry(c) for c in wb]
        merged = iwb.merge_catalogs(existing, wb_entries)
        added = sum(1 for e in merged if e["kind"] not in {x["kind"] for x in existing})
        new_catalog = {"version": 1, "connectors": merged}

        # 4. 幂等写入：写 .new 再 rename
        new_path = os.path.join(_HERE, "catalog.json.new")
        with open(new_path, "w", encoding="utf-8") as fh:
            json.dump(new_catalog, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(new_path, os.path.join(_HERE, "catalog.json"))
        logger.info("catalog.json 已更新：共 %d 条，新增 %d 条", len(merged), added)

        return {"ok": True, "added": added, "total": len(merged)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("同步 catalog 失败")
        raise
    finally:
        iwb._SRC_ROOT = orig_src_root
        iwb._SRC_CATALOG = orig_src_catalog
        iwb._SRC_CONNECTORS_DIR = orig_src_connectors_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    res = sync_catalog()
    print(json.dumps(res, ensure_ascii=False, indent=2))
