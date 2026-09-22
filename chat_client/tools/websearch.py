"""
@input: json, logging, os, re, requests, typing
@output: WebSearch 工具 + 可插拔 Provider 抽象
@position: Tools layer — 联网搜索，统一结果结构（参考 rikkahub SearchService 设计）
@auto-doc: Update header and folder INDEX.md when this file changes

设计参考 rikkahub (https://github.com/sxkiss/rikkahub) 的 search 模块：
- 统一 SearchResult{answer, items[title,url,text]} 结构，屏蔽 provider 差异
- Provider 可插拔：新增引擎只需实现 _search() 并在 PROVIDERS 注册
- 配置与实现解耦：api_key 等从 config.json -> search 段读取，不在代码里硬编码

与 rikkahub 的差异（适配本项目）：
- rikkahub 是 Kotlin/Compose 多引擎；这里首批实现 4 个实用 provider
- key 统一走 config.json，由 SettingsDrawer 前端配置
"""
import base64
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
from urllib.parse import urlparse, unquote
from typing import Any

import requests

from . import PROJECT_ROOT, register_tool
from .base import _xml_response, truncate_output

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30          # 单次请求超时（秒）
MAX_RESULT_SIZE = 10          # 默认返回结果条数
MAX_TEXT_LEN = 500            # 单条摘要截断长度，避免塞爆上下文

# 云厂商元数据服务：SSRF 时危害最大（可直接拿到临时凭据），一律拦截
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata"}


# ---------------------------------------------------------------- 配置读取


def _load_search_config() -> dict[str, Any]:
    """从 config.json 的 search 段读取配置。文件缺失/损坏时返回默认结构。"""
    path = os.path.join(PROJECT_ROOT, "config.json")
    default = {"provider": DEFAULT_PROVIDER, "result_size": MAX_RESULT_SIZE,
               "providers": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.warning("config.json 读取失败，搜索回退默认配置", exc_info=True)
        return default

    cfg = data.get("search")
    if not isinstance(cfg, dict):
        return default
    merged = dict(default)
    merged.update(cfg)
    # providers 段的 key 可能已被前端脱敏为 "***"，读取时以真实文件为准（这里就是真实文件）
    if not isinstance(merged.get("providers"), dict):
        merged["providers"] = {}
    return merged


def _provider_options(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    return cfg.get("providers", {}).get(name, {}) or {}


# ---------------------------------------------------------------- 统一结果

class SearchError(Exception):
    pass


def _proxies() -> dict[str, str] | None:
    """读取环境代理（模块级解析一次，进程生命周期内代理配置基本不变）。

    本机无代理时外网直连不通（实测 OSError: Network is unreachable），
    而 curl 通过 http://127.0.0.1:7890 可正常访问，故支持标准环境变量。
    """
    global _PROXIES_CACHE
    if _PROXIES_CACHE is None:
        p = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") \
            or os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") \
            or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
        _PROXIES_CACHE = {"http": p, "https": p} if p else False   # False = 明确无代理
    return _PROXIES_CACHE or None


_PROXIES_CACHE = None


def _req(method: str, url: str, **kw):
    """统一发请求，自动带上环境代理与超时。"""
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    p = _proxies()          # 只解析一次，避免每请求重复读 6 次环境变量
    if p:
        kw["proxies"] = p
    return requests.request(method, url, **kw)


# Python 的 ipaddress 把 2001::/32（Teredo 隧道）整段标为 private，
# 但它并非内网；误拦会让正常外网域名被拒（实测 www.google.com 解析到 2001::1 被拦）。
# 这里只对真正的内网段（ULA fc00::/7 等）保持拦截。
_TEREDO_NET = ipaddress.ip_network("2001::/32")


def _is_ipv6_public_but_flagged(ip_obj) -> bool:
    return ip_obj in _TEREDO_NET


def _block_ssrf(url: str, allow_private: bool = False) -> str | None:
    """返回 None 表示安全，否则返回拒绝原因。

    allow_private: 是否放行内网/本机地址。
      自建服务（如本机 Docker 部署的 SearXNG）本来就跑在 127.0.0.1，
      一刀切拦内网会让这类配置完全不可用。故用户在设置页显式填写的引擎地址
      放行内网；但云元数据地址（169.254.169.254 等）任何情况都不放行——
      它可直接换取云临时凭据，危害远大于普通内网探测。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "URL 格式非法"
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL 缺少主机名"
    # 云元数据：任何情况都拦截
    if host in _METADATA_HOSTS:
        return f"禁止访问云元数据地址 {host}（SSRF 防护）"
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(
                host, None, family=socket.AF_UNSPEC):
            ip = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip)
            # 云元数据 IP：任何情况都拦截
            if str(ip_obj) in _METADATA_HOSTS:
                return f"禁止访问云元数据地址 {host} -> {ip}（SSRF 防护）"
            if ip_obj.version == 6 and _is_ipv6_public_but_flagged(ip_obj):
                # 2001::/32 是 Teredo 隧道而非内网，Python 误标为 private，放行
                continue
            if allow_private:
                continue
            if (ip_obj.is_private or ip_obj.is_loopback
                    or ip_obj.is_link_local or ip_obj.is_reserved
                    or ip_obj.is_unspecified):
                return f"禁止访问内网/本机地址 {host} -> {ip}（SSRF 防护）"
        return None
    except (socket.gaierror, OSError, ValueError):
        # DNS 解析失败按不安全处理，fail closed
        return f"无法解析主机 {host}（SSRF 防护，已拒绝）"


def _post_json(url: str, payload: dict, bearer: str | None = None,
               headers: dict | None = None) -> dict:
    """POST JSON 并统一处理网络/HTTP/JSON 三类错误，返回已解析的 dict。"""
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    if headers:
        h.update(headers)
    try:
        r = _req("post", url, json=payload, headers=h)
    except requests.RequestException as e:
        raise SearchError(f"请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"返回状态码 {r.status_code}（检查 API Key 是否正确）")
    try:
        return r.json()
    except ValueError:
        raise SearchError("返回结果不是有效 JSON")


def _mk_item(title: str, url: str, text: str, **extra) -> dict[str, Any]:
    item = {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "text": (text or "")[:MAX_TEXT_LEN],
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------- Providers

def _bing_real_url(href: str) -> str:
    """还原 Bing 的跳转包装为真实目标地址。

    Bing 结果里的 href 形如 https://www.bing.com/ck/a?!&&p=...&u=a1<base64url>，
    直接返回会让用户点到 Bing 中转页。真实地址 base64url 编码在 u=a1 之后。
    解码失败时退回原 href（宁可给中转页，也不要丢结果）。
    """
    try:
        raw = unquote(href)
        m = re.search(r"[?&]u=a1([^&\"]+)", raw)
        if not m:
            return href
        enc = m.group(1)
        enc += "=" * ((4 - len(enc) % 4) % 4)      # base64 补位
        dec = base64.urlsafe_b64decode(enc).decode("utf-8", "replace")
        # 解码后的内容形如 "https://xxx.com/..."（有时带前缀控制字符）
        if dec.startswith("http://") or dec.startswith("https://"):
            return dec
        idx = dec.find("http")
        return dec[idx:] if idx >= 0 else href
    except Exception:
        return href


def _search_bing(query: str, opts: dict, size: int) -> dict:
    """Bing 网页抓取（对标 rikkahub BingSearchService，无官方 API，解析 HTML）。

    实测本机可用（状态 200，b_algo 块 10 个），且无需代理即可直连。
    无需 api_key，但依赖 Bing 页面结构，若其改版需同步调整解析。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": opts.get("language") or "zh-CN,zh",
        "Referer": "https://www.bing.com/",
    }
    try:
        r = _req("get", "https://www.bing.com/search", params={"q": query}, headers=headers)
    except requests.RequestException as e:
        raise SearchError(f"Bing 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"Bing 返回状态码 {r.status_code}")

    # 结构对齐 rikkahub: li.b_algo -> h2 标题 / h2>a 链接 / .b_caption p 摘要
    blocks = re.split(r'<li[^>]*class="[^"]*b_algo', r.text)
    items = []
    for blk in blocks[1:]:
        if len(items) >= size:
            break
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not m:
            continue
        url = _bing_real_url(html_lib.unescape(m.group(1)))
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        sn = re.search(r'class="[^"]*b_caption[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>', blk, re.S)
        text = ""
        if sn:
            text = html_lib.unescape(re.sub(r"<[^>]+>", "", sn.group(1))).strip()
        if url and title:
            items.append(_mk_item(title, url, text))
    if not items:
        raise SearchError("Bing 未返回结果（页面结构可能已变更）")
    return {"answer": None, "items": items}


def _search_searxng(query: str, opts: dict, size: int) -> dict:
    """SearXNG 开源聚合引擎。需要 url（自建或公共实例），api_key 可选。"""
    base = (opts.get("url") or "").rstrip("/")
    if not base:
        raise SearchError("SearXNG 未配置实例地址（设置 → 搜索 → 实例 URL）")
    if not base.startswith(("http://", "https://")):
        raise SearchError("SearXNG 实例地址必须以 http:// 或 https:// 开头")
    # 实例地址由用户在设置页填写，允许内网/本机（自建 Docker 实例通常就在 127.0.0.1），
    # 但仍拦截云元数据地址
    blocked = _block_ssrf(base, allow_private=True)
    if blocked:
        raise SearchError(blocked)
    params = {"q": query, "format": "json", "safesearch": 0}
    if opts.get("engine"):
        params["engines"] = opts["engine"]
    headers = {}
    if opts.get("api_key"):
        headers["Authorization"] = f"Bearer {opts['api_key']}"
    try:
        r = _req("get", f"{base}/search", params=params, headers=headers)
    except requests.RequestException as e:
        raise SearchError(f"SearXNG 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"SearXNG 返回状态码 {r.status_code}（实例可能未开启 JSON 格式）")
    try:
        d = r.json()
    except ValueError:
        raise SearchError("SearXNG 返回的不是有效 JSON（实例可能未启用 format=json）")

    items = [_mk_item(x.get("title", ""), x.get("url", ""), x.get("content", ""))
             for x in (d.get("results") or [])][:size]
    if not items:
        raise SearchError("SearXNG 未返回结果")
    return {"answer": None, "items": items}


# ---- 以下 API 形状均对照 rikkahub 同名 provider 实现核准（URL/鉴权头/请求体）----

def _search_zhipu(query: str, opts: dict, size: int) -> dict:
    """智谱 bigmodel web_search（对标 rikkahub ZhipuSearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("智谱未配置 API Key（设置 → 搜索 → 智谱）")
    r = _post_json("https://open.bigmodel.cn/api/paas/v4/web_search",
                   {"search_query": query, "search_engine": "search_std", "count": size},
                   bearer=key)
    items = []
    for x in (r.get("search_result") or []):
        items.append(_mk_item(x.get("title", ""), x.get("link", ""), x.get("content", "")))
    items = items[:size]
    if not items:
        raise SearchError("智谱未返回结果")
    return {"answer": None, "items": items}


def _search_doubao(query: str, opts: dict, size: int) -> dict:
    """豆包（对标 rikkahub DoubaoSearchService，经 feedcoopapi 转发）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("豆包未配置 API Key（设置 → 搜索 → 豆包）")
    endpoint = "v1/search" if str(opts.get("mode", "custom")).lower() == "custom" else "v1/ai-search"
    r = _post_json(f"https://open.feedcoopapi.com/search_api/{endpoint}",
                   {"query": query, "count": size}, bearer=key)
    items = []
    for x in (r.get("data") or r.get("results") or []):
        items.append(_mk_item(x.get("title", ""), x.get("url", ""), x.get("content", "")))
    items = items[:size]
    if not items:
        raise SearchError("豆包未返回结果")
    return {"answer": None, "items": items}


def _search_exa(query: str, opts: dict, size: int) -> dict:
    """Exa 语义搜索（对标 rikkahub ExaSearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Exa 未配置 API Key（设置 → 搜索 → Exa）")
    r = _post_json("https://api.exa.ai/search",
                   {"query": query, "numResults": size,
                    "contents": {"text": {"maxCharacters": MAX_TEXT_LEN}}},
                   bearer=key)
    items = []
    for x in (r.get("results") or []):
        items.append(_mk_item(x.get("title", ""), x.get("url", ""), x.get("text", "")))
    items = items[:size]
    if not items:
        raise SearchError("Exa 未返回结果")
    return {"answer": None, "items": items}


def _search_brave(query: str, opts: dict, size: int) -> dict:
    """Brave Search（对标 rikkahub BraveSearchService，用 X-Subscription-Token）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Brave 未配置 API Key（设置 → 搜索 → Brave）")
    try:
        r = _req("get", "https://api.search.brave.com/res/v1/web/search",
                 params={"q": query, "count": size},
                 headers={"Accept": "application/json", "X-Subscription-Token": key})
    except requests.RequestException as e:
        raise SearchError(f"Brave 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"Brave 返回状态码 {r.status_code}（检查 API Key）")
    d = r.json()
    items = [_mk_item(x.get("title", ""), x.get("url", ""), x.get("description", ""))
             for x in ((d.get("web") or {}).get("results") or [])][:size]
    if not items:
        raise SearchError("Brave 未返回结果")
    return {"answer": None, "items": items}


def _search_metaso(query: str, opts: dict, size: int) -> dict:
    """秘塔 Metaso（对标 rikkahub MetasoSearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("秘塔未配置 API Key（设置 → 搜索 → 秘塔）")
    r = _post_json("https://metaso.cn/api/v1/search",
                   {"q": query, "scope": "webpage", "size": size, "includeSummary": False},
                   bearer=key)
    items = []
    for x in ((r.get("webpages") or {}).get("webpage") or []):
        items.append(_mk_item(x.get("title", ""), x.get("link", ""), x.get("snippet", "")))
    items = items[:size]
    if not items:
        raise SearchError("秘塔未返回结果")
    return {"answer": None, "items": items}


def _search_serper(query: str, opts: dict, size: int) -> dict:
    """Serper / Google（对标 rikkahub SerperSearchService，X-API-KEY 头）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Serper 未配置 API Key（设置 → 搜索 → Serper）")
    try:
        r = _req("post", "https://google.serper.dev/search",
                 json={"q": query, "num": size},
                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise SearchError(f"Serper 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"Serper 返回状态码 {r.status_code}（检查 API Key）")
    d = r.json()
    answer = None
    ab = d.get("answerBox")
    if isinstance(ab, dict):
        answer = ab.get("answer") or ab.get("snippet")
    kg = d.get("knowledgeGraph")
    if not answer and isinstance(kg, dict):
        answer = kg.get("description")
    items = [_mk_item(x.get("title", ""), x.get("link", ""), x.get("snippet", ""))
             for x in (d.get("organic") or [])][:size]
    if not items:
        raise SearchError("Serper 未返回结果")
    return {"answer": answer, "items": items}


def _search_jina(query: str, opts: dict, size: int) -> dict:
    """Jina Reader 搜索（对标 rikkahub JinaSearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Jina 未配置 API Key（设置 → 搜索 → Jina）")
    try:
        r = _req("get", "https://s.jina.ai/", params={"q": query},
                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                          "X-Respond-With": "no-content"})
    except requests.RequestException as e:
        raise SearchError(f"Jina 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"Jina 返回状态码 {r.status_code}（检查 API Key）")
    try:
        d = r.json()
    except ValueError:
        raise SearchError("Jina 返回的不是有效 JSON")
    items = [_mk_item(x.get("title", ""), x.get("url", ""), x.get("content", "") or x.get("description", ""))
             for x in (d.get("data") or [])][:size]
    if not items:
        raise SearchError("Jina 未返回结果")
    return {"answer": None, "items": items}


def _search_linkup(query: str, opts: dict, size: int) -> dict:
    """LinkUp（对标 rikkahub LinkUpService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("LinkUp 未配置 API Key（设置 → 搜索 → LinkUp）")
    d = _post_json("https://api.linkup.so/v1/search",
                   {"q": query, "depth": opts.get("depth", "standard"),
                    "outputType": "searchResults"}, bearer=key)
    items = [_mk_item(x.get("name", "") or x.get("title", ""), x.get("url", ""), x.get("content", ""))
             for x in (d.get("results") or [])][:size]
    if not items:
        raise SearchError("LinkUp 未返回结果")
    return {"answer": None, "items": items}


def _search_perplexity(query: str, opts: dict, size: int) -> dict:
    """Perplexity Search API（对标 rikkahub PerplexitySearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Perplexity 未配置 API Key（设置 → 搜索 → Perplexity）")
    d = _post_json("https://api.perplexity.ai/search",
                   {"query": query, "max_results": size}, bearer=key)
    items = [_mk_item(x.get("title", ""), x.get("url", ""), x.get("snippet", ""))
             for x in (d.get("results") or [])][:size]
    if not items:
        raise SearchError("Perplexity 未返回结果")
    return {"answer": None, "items": items}


def _search_firecrawl(query: str, opts: dict, size: int) -> dict:
    """Firecrawl v2 search（对标 rikkahub FirecrawlSearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Firecrawl 未配置 API Key（设置 → 搜索 → Firecrawl）")
    d = _post_json("https://api.firecrawl.dev/v2/search",
                   {"query": query, "limit": size}, bearer=key)
    items = [_mk_item(x.get("title", ""), x.get("url", ""),
                      x.get("markdown", "") or x.get("description", ""))
             for x in (d.get("data") or [])][:size]
    if not items:
        raise SearchError("Firecrawl 未返回结果")
    return {"answer": None, "items": items}


def _search_ollama(query: str, opts: dict, size: int) -> dict:
    """Ollama 官方 web_search（对标 rikkahub OllamaSearchService）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Ollama 未配置 API Key（设置 → 搜索 → Ollama）")
    d = _post_json("https://ollama.com/api/web_search",
                   {"query": query, "max_results": max(5, min(size, 10))}, bearer=key)
    items = [_mk_item(x.get("title", ""), x.get("url", ""), x.get("content", ""))
             for x in (d.get("results") or [])][:size]
    if not items:
        raise SearchError("Ollama 未返回结果")
    return {"answer": None, "items": items}


def _search_tinyfish(query: str, opts: dict, size: int) -> dict:
    """Tinyfish（对标 rikkahub TinyfishSearchService，X-API-Key 头）。需 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Tinyfish 未配置 API Key（设置 → 搜索 → Tinyfish）")
    try:
        r = _req("post", "https://api.fetch.tinyfish.ai",
                 json={"query": query, "limit": size},
                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise SearchError(f"Tinyfish 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"Tinyfish 返回状态码 {r.status_code}（检查 API Key）")
    d = r.json()
    items = [_mk_item(x.get("title", ""), x.get("url", ""), x.get("content", "") or x.get("snippet", ""))
             for x in (d.get("results") or [])][:size]
    if not items:
        raise SearchError("Tinyfish 未返回结果")
    return {"answer": None, "items": items}


def _search_grok(query: str, opts: dict, size: int) -> dict:
    """Grok/xAI 兼容 chat 接口的搜索（对标 rikkahub GrokSearchService）。

    Grok 走的是 OpenAI 兼容 /chat/completions，custom_url 可指向自建网关。
    """
    key = opts.get("api_key")
    if not key:
        raise SearchError("Grok 未配置 API Key（设置 → 搜索 → Grok）")
    base = (opts.get("url") or "https://api.x.ai/v1").rstrip("/")
    # url 由用户在设置页填写（可指向自建网关），允许内网/本机，但仍拦云元数据
    blocked = _block_ssrf(base, allow_private=True)
    if blocked:
        raise SearchError(blocked)
    payload = {
        "model": opts.get("model", "grok-3"),
        "messages": [
            {"role": "system", "content": "You are a search assistant. Return concise results."},
            {"role": "user", "content": query},
        ],
        "stream": False,
    }
    if str(opts.get("enable_search", "true")).lower() == "true":
        payload["search_parameters"] = {"mode": "auto"}
    d = _post_json(f"{base}/chat/completions", payload, bearer=key)
    try:
        content = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SearchError("Grok 返回结构异常（缺少 choices[0].message.content）")
    # Grok 非结构化返回：整段作为 answer，同时给出一条占位 item 便于上层统一处理
    return {"answer": content, "items": [_mk_item("Grok 回答", "", content)]}


def _search_tavily(query: str, opts: dict, size: int) -> dict:
    """Tavily：面向 AI 优化的搜索，answer 字段质量好。需要 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("Tavily 未配置 API Key（设置 → 搜索 → Tavily）")
    try:
        r = _req("post", "https://api.tavily.com/search",
                 json={"api_key": key, "query": query, "max_results": size,
                       "search_depth": opts.get("depth", "basic")})
    except requests.RequestException as e:
        raise SearchError(f"Tavily 请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"Tavily 返回状态码 {r.status_code}（检查 API Key 是否正确）")
    d = r.json()
    items = [_mk_item(x.get("title", ""), x.get("url", ""),
                      x.get("content", "") or x.get("snippet", ""))
             for x in (d.get("results") or [])][:size]
    if not items:
        raise SearchError("Tavily 未返回结果")
    return {"answer": d.get("answer"), "items": items}


def _search_bocha(query: str, opts: dict, size: int) -> dict:
    """博查 Bocha：国内可用，中文结果较好。需要 api_key。"""
    key = opts.get("api_key")
    if not key:
        raise SearchError("博查未配置 API Key（设置 → 搜索 → 博查）")
    try:
        r = _req("post", "https://api.bochaai.com/v1/web-search",
                 headers={"Authorization": f"Bearer {key}",
                          "Content-Type": "application/json"},
                 json={"query": query, "count": size, "summary": True})
    except requests.RequestException as e:
        raise SearchError(f"博查请求失败: {e!s}")
    if not r.ok:
        raise SearchError(f"博查返回状态码 {r.status_code}（检查 API Key 是否正确）")
    d = r.json()
    data = d.get("data") or {}
    pages = ((data.get("webPages") or {}).get("value") or []) if isinstance(data, dict) else []
    items = [_mk_item(x.get("name", ""), x.get("url", ""), x.get("summary", ""))
             for x in pages][:size]
    if not items:
        raise SearchError("博查未返回结果")
    return {"answer": None, "items": items}


# provider 注册表：新增引擎只需实现 _search_xxx 并在此加一行。
# 字段：label=显示名, need_key=是否必须配 API Key, fn=实现
PROVIDERS = {
    # 免 Key
    "bing": {"label": "Bing", "need_key": False, "fn": _search_bing},
    "searxng": {"label": "SearXNG", "need_key": False, "fn": _search_searxng},
    # 需 Key —— 国内可用
    "zhipu": {"label": "智谱", "need_key": True, "fn": _search_zhipu},
    "doubao": {"label": "豆包", "need_key": True, "fn": _search_doubao},
    "bocha": {"label": "博查", "need_key": True, "fn": _search_bocha},
    "metaso": {"label": "秘塔", "need_key": True, "fn": _search_metaso},
    # 需 Key —— 海外
    "tavily": {"label": "Tavily", "need_key": True, "fn": _search_tavily},
    "exa": {"label": "Exa", "need_key": True, "fn": _search_exa},
    "brave": {"label": "Brave", "need_key": True, "fn": _search_brave},
    "serper": {"label": "Serper", "need_key": True, "fn": _search_serper},
    "linkup": {"label": "LinkUp", "need_key": True, "fn": _search_linkup},
    "perplexity": {"label": "Perplexity", "need_key": True, "fn": _search_perplexity},
    "firecrawl": {"label": "Firecrawl", "need_key": True, "fn": _search_firecrawl},
    "jina": {"label": "Jina", "need_key": True, "fn": _search_jina},
    "ollama": {"label": "Ollama", "need_key": True, "fn": _search_ollama},
    "grok": {"label": "Grok", "need_key": True, "fn": _search_grok},
    "tinyfish": {"label": "Tinyfish", "need_key": True, "fn": _search_tinyfish},
}

DEFAULT_PROVIDER = "bing"


# ---------------------------------------------------------------- 工具入口

@register_tool(category="网络", name_cn="联网搜索", risk_level="low")
def WebSearch(query: str, provider: str | None = None, result_size: int | None = None) -> str:
    """
    - 联网搜索，返回结构化结果（标题/链接/摘要），可选引擎见下方说明
    - 与模型无关：任何模型都可调用，不依赖 qwen 的 enable_search 扩展参数
    - 使用前需在「设置 → 搜索」配置对应引擎的 API Key（Bing / SearXNG 免 Key）

    Args:
        query: 搜索关键词。
        provider: 引擎名，留空用全局默认（bing）。可选：
            免 Key — bing(默认), searxng
            需 Key — zhipu(智谱), doubao(豆包), bocha(博查), metaso(秘塔),
                     tavily, exa, brave, serper, linkup, perplexity,
                     firecrawl, jina, ollama, grok, tinyfish
        result_size: 返回结果条数，默认取全局配置（10），上限 20。
    """
    q = (query or "").strip()
    if not q:
        return _xml_response("error", "缺少参数 query")

    cfg = _load_search_config()
    pname = (provider or cfg.get("provider") or DEFAULT_PROVIDER).lower()
    if pname not in PROVIDERS:
        supported = ", ".join(PROVIDERS)
        return _xml_response("error", f"不支持的引擎 '{pname}'，可选：{supported}")

    try:
        size = int(result_size or cfg.get("result_size") or MAX_RESULT_SIZE)
    except (TypeError, ValueError):
        size = MAX_RESULT_SIZE
    size = max(1, min(size, 20))

    opts = _provider_options(cfg, pname)
    meta = PROVIDERS[pname]

    # 需要 key 但没配：给出明确指引，而不是让下游报难以理解的 401
    if meta["need_key"] and not opts.get("api_key"):
        fallback = "provider=bing（免 Key，实测可用）" if pname != "bing" else "其它需 Key 的引擎"
        return _xml_response(
            "error",
            f"引擎 {meta['label']} 未配置 API Key。请在「设置 → 搜索 → {meta['label']}」填入后重试，"
            f"或改用 {fallback}。",
        )

    try:
        res = meta["fn"](q, opts, size)
    except SearchError as e:
        return _xml_response("error", str(e))
    except Exception as e:
        logger.warning("搜索异常 provider=%s", pname, exc_info=True)
        return _xml_response("error", f"搜索失败（{meta['label']}）: {e!s}")

    lines = []
    if res.get("answer"):
        lines.append(f"## 摘要\n{res['answer']}\n")
    items = res.get("items") or []
    for i, it in enumerate(items, 1):
        lines.append(f"### {i}. {it['title']}\n{it['url']}\n{it['text']}\n")
    if not lines:
        return _xml_response("error", f"{meta['label']} 未返回可用结果")

    out = f"<!-- engine: {pname} / {len(items)} 条 -->\n" + "\n".join(lines)
    return _xml_response("done", truncate_output(out))
