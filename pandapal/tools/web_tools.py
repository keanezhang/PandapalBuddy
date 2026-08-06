"""pandapal/tools/web.py — Web 工具集（应用层）

提供两个 Web 能力工具：
- web_fetch   : 获取 URL 内容，HTML 自动转为纯文本（对标 Claude Code WebFetchTool）
- web_search  : 网络搜索，四后端自动优先级瀑布链

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
web_search 后端优先级（自动按顺序尝试）：

  1. Tavily      — AI 搜索，全球覆盖，结果质量最高
                   需要：TAVILY_API_KEY
                   注册：https://tavily.com

  2. DuckDuckGo  — 免费，无需 Key，始终可用（保底）
                   无需任何配置

  3. Bocha       — 博查 AI 搜索，国内直连，中文内容更优
                   需要：BOCHA_API_KEY
                   注册：https://open.bochaai.com

  4. SerpAPI     — Google 搜索结果，全球覆盖
                   需要：SERPAPI_KEY
                   注册：https://serpapi.com

规则：
  - 有 Key 且调用成功 → 使用该后端，不再尝试后续
  - 无 Key 或调用失败 → 跳过，尝试下一个
  - DuckDuckGo 无需 Key，是永远的保底后端
  - 四个全部失败才返回错误

配置示例（.env 文件）：
  TAVILY_API_KEY=tvly-xxx
  BOCHA_API_KEY=sk-xxx
  SERPAPI_KEY=xxx
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

依赖：
    httpx >= 0.25.0（已在 pyproject.toml 中声明）

可选依赖（安装后自动启用更好的 HTML 解析）：
    pip install beautifulsoup4
"""

import re
import json
import os
import html
import logging
from urllib.parse import urlencode, urlparse

import httpx

from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.decorator import tool

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  内部辅助：HTML 转纯文本
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _html_to_text(html_content: str) -> str:
    """将 HTML 内容转换为可读纯文本。

    优先使用 BeautifulSoup（若已安装），降级为正则表达式方案。
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html_content, "html.parser")

        for tag in soup(["script", "style", "noscript", "nav", "footer"]):
            tag.decompose()

        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            level = int(h.name[1])
            h.insert_before("\n" + "#" * level + " ")
            h.append("\n")

        for a in soup.find_all("a", href=True):
            a.replace_with(f"{a.get_text()} [{a['href']}]")

        text = soup.get_text(separator="\n")
    except ImportError:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)

    lines = [line.strip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            cleaned.append(line)
            blank_count = 0
        else:
            blank_count += 1
            if blank_count <= 1:
                cleaned.append("")

    return "\n".join(cleaned).strip()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. web_fetch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@tool.function(
    tier=ToolTier.ALWAYS,
    name="web_fetch",
    description="获取指定 URL 的网页内容，自动将 HTML 转为可读纯文本",
    when_to_use="当需要读取网页内容、API 响应或在线文档时调用；不适用于需要登录认证的页面",
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        max_output_bytes=100_000,
        max_calls_per_turn=10,
    ),
    progress_label='获取网页「{url}」',
)
def web_fetch(
    ctx: ToolContext,
    url: str,
    timeout: int = 30,
) -> str:
    """获取 URL 内容并转为可读纯文本。

    Args:
        ctx: 工具上下文。
        url: 要访问的 URL（HTTP/HTTPS），HTTP 会自动升级为 HTTPS。
        timeout: 请求超时时间（秒），默认 30 秒。

    Returns:
        页面内容文本（HTML 自动转换为纯文本）；请求失败时返回错误信息。
    """
    if url.startswith("http://"):
        url = "https://" + url[7:]

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return f"错误：无效的 URL：{url}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; PandarenAgent/1.0; "
            "+https://github.com/pandaren-agent)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(url)

        redirect_note = f"（重定向到：{response.url}）\n\n" if str(response.url) != url else ""

        if response.status_code >= 400:
            return f"请求失败：HTTP {response.status_code}（{url}）"

        content_type = response.headers.get("content-type", "")

        if "application/json" in content_type:
            try:
                text = json.dumps(response.json(), ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, ValueError):
                text = response.text
        elif "text/plain" in content_type:
            text = response.text
        else:
            text = _html_to_text(response.text)

        if not text.strip():
            return f"页面内容为空：{url}"

        header = f"# {url}\n状态：{response.status_code} | 内容类型：{content_type.split(';')[0]}\n{redirect_note}\n"
        return header + text

    except httpx.TimeoutException:
        return f"请求超时（{timeout}秒）：{url}"
    except httpx.TooManyRedirects:
        return f"重定向次数过多：{url}"
    except httpx.ConnectError:
        return f"连接失败（网络不可达或域名无法解析）：{url}"
    except Exception as e:
        return f"获取 URL 失败：{e}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. web_search — 四后端优先级瀑布链
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── 后端 1：Tavily（需要 TAVILY_API_KEY）────────

def _search_tavily(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str,
) -> list[dict]:
    """Tavily AI 搜索（直接 HTTP，无需安装 tavily-python）。"""
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return []

    # freshness → Tavily 不支持时间过滤参数，用 topic 区分新闻/通用
    topic = "news" if freshness in ("oneDay", "oneWeek") else "general"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": min(max_results, 20),
                    "search_depth": "basic",
                    "topic": topic,
                    "include_answer": True,
                },
            )
        if response.status_code != 200:
            logger.warning("[web_search] Tavily HTTP %d", response.status_code)
            return []

        data = response.json()
        results = []
        if data.get("answer"):
            results.append({
                "title": "Tavily AI 答案",
                "url": "",
                "snippet": data["answer"],
            })
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })
        return results

    except Exception as e:
        logger.warning("[web_search] Tavily 失败：%s", e)
        return []


# ── 后端 2：DuckDuckGo（免费，无需 Key）──────────

def _search_duckduckgo(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str,  # noqa: ARG001  # DuckDuckGo 不支持时间过滤
) -> list[dict]:
    """DuckDuckGo Instant Answer API（免费，无需 Key，永远可用）。"""
    params = urlencode({"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"})
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(f"https://api.duckduckgo.com/?{params}")
        data = response.json()
    except Exception as e:
        logger.warning("[web_search] DuckDuckGo 失败：%s", e)
        return []

    results = []
    if data.get("AbstractText"):
        results.append({
            "title": data.get("Heading", "摘要"),
            "url": data.get("AbstractURL", ""),
            "snippet": data["AbstractText"],
        })
    for topic in data.get("RelatedTopics", []):
        if isinstance(topic, dict) and "Text" in topic:
            results.append({
                "title": topic.get("Text", "")[:80],
                "url": topic.get("FirstURL", ""),
                "snippet": topic.get("Text", ""),
            })
        if len(results) >= max_results:
            break

    return results


# ── 后端 3：Bocha 博查（需要 BOCHA_API_KEY）──────

def _search_bocha(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str,
) -> list[dict]:
    """博查 AI 搜索，国内直连，中文内容更优（需要 BOCHA_API_KEY）。"""
    api_key = os.environ.get("BOCHA_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.bochaai.com/v1/web-search",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "count": min(max_results, 20),
                    "freshness": freshness,
                    "summary": True,
                },
            )
        if response.status_code != 200:
            logger.warning("[web_search] Bocha HTTP %d", response.status_code)
            return []

        data = response.json()
        results = []
        for item in data.get("data", {}).get("webPages", {}).get("value", [])[:max_results]:
            results.append({
                "title": item.get("name", ""),
                "url": item.get("url", ""),
                "snippet": item.get("summary") or item.get("snippet", ""),
            })
        return results

    except Exception as e:
        logger.warning("[web_search] Bocha 失败：%s", e)
        return []


# ── 后端 4：SerpAPI（需要 SERPAPI_KEY）──────────

def _search_serpapi(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str,  # noqa: ARG001  # SerpAPI 不直接支持 freshness
) -> list[dict]:
    """SerpAPI Google 搜索（需要 SERPAPI_KEY）。"""
    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not api_key:
        return []

    params = urlencode({"q": query, "api_key": api_key, "num": max_results, "hl": "zh-cn"})
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(f"https://serpapi.com/search.json?{params}")
        if response.status_code != 200:
            logger.warning("[web_search] SerpAPI HTTP %d", response.status_code)
            return []

        data = response.json()
        results = []
        for item in data.get("organic_results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return results

    except Exception as e:
        logger.warning("[web_search] SerpAPI 失败：%s", e)
        return []


# ── 优先级瀑布链 ─────────────────────────────────

_PROVIDER_CHAIN: list[tuple[str, object]] = [
    ("Tavily",     _search_tavily),
    ("DuckDuckGo", _search_duckduckgo),
    ("Bocha",      _search_bocha),
    ("SerpAPI",    _search_serpapi),
]


def _run_search_chain(
    query: str,
    max_results: int,
    timeout: int,
    freshness: str,
) -> tuple[list[dict], str]:
    """按优先级依次尝试各后端，返回 (results, 使用的后端名)。

    - 有 Key 的后端：Key 缺失则跳过；调用失败则继续尝试下一个。
    - DuckDuckGo：无需 Key，永远参与尝试。
    - 返回第一个拿到非空结果的后端的数据。
    """
    for name, fn in _PROVIDER_CHAIN:
        results = fn(query, max_results, timeout, freshness)  # type: ignore[call-arg]
        if results:
            logger.info("[web_search] 使用后端：%s，查询：%r，结果数：%d", name, query, len(results))
            return results, name

    return [], "无可用后端"


# ── 工具定义 ─────────────────────────────────────

_SNIPPET_MAX_LENGTH = 5000  # 摘要最大字符数（前端展开上限5000字）

@tool.function(
    tier=ToolTier.ALWAYS,
    name="web_search",
    description=(
        "在网络上搜索最新信息，返回结果列表。"
        "自动按优先级尝试：Tavily → DuckDuckGo → Bocha → SerpAPI，"
        "有 Key 的后端优先，DuckDuckGo 作为免费保底"
    ),
    when_to_use=(
        "当需要获取最新信息、新闻、文档或回答时事问题时调用；"
        "知识截止日期之后的信息或需要实时数据时必须使用此工具"
    ),
    policy=ToolPolicy(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        max_calls_per_turn=10,
        max_output_bytes=50_000,
    ),
    progress_label='搜索「{query}」',
)
def web_search(
    ctx: ToolContext,
    query: str,
    max_results: int = 5,
    freshness: str = "noLimit",
    timeout: int = 15,
) -> str:
    """在网络上搜索并返回结果摘要，自动选择最优可用后端。

    Args:
        ctx: 工具上下文。
        query: 搜索查询词或问题。
        max_results: 最多返回的结果数量，默认 5 条（上限 20）。
        freshness: 时间范围过滤，可选值：
                   noLimit（不限，默认）、oneDay（一天内）、
                   oneWeek（一周内）、oneMonth（一月内）、oneYear（一年内）。
                   注：仅 Bocha 和 Tavily 支持此参数，其他后端忽略。
        timeout: 单个后端的请求超时时间（秒），默认 15 秒。

    Returns:
        格式化的搜索结果列表（含标题、URL 和摘要）；
        所有后端均失败时返回错误信息和配置建议。
    """
    max_results = max(1, min(max_results, 20))
    valid_freshness = {"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}
    if freshness not in valid_freshness:
        freshness = "noLimit"

    results, backend = _run_search_chain(query, max_results, timeout, freshness)

    if not results:
        return (
            f"未找到 '{query}' 的搜索结果（已尝试所有可用后端）。\n\n"
            "配置建议（在 .env 文件中设置，可提升搜索质量）：\n"
            "  TAVILY_API_KEY=tvly-xxx   # https://tavily.com（推荐，AI 搜索）\n"
            "  BOCHA_API_KEY=sk-xxx      # https://open.bochaai.com（中文内容更优）\n"
            "  SERPAPI_KEY=xxx           # https://serpapi.com（Google 结果）\n"
            "  DuckDuckGo 无需 Key，如连接失败请检查网络。"
        )

    lines = [f"# 搜索结果：{query}（后端：{backend}，共 {len(results)} 条）\n"]
    for i, r in enumerate(results, start=1):
        lines.append(f"## {i}. {r['title']}")
        if r.get("url"):
            lines.append(f"URL: {r['url']}")
        if r.get("snippet"):
            snippet = r["snippet"]
            if len(snippet) > _SNIPPET_MAX_LENGTH:
                snippet = snippet[:_SNIPPET_MAX_LENGTH] + "..."
            lines.append(snippet)
        lines.append("")

    return "\n".join(lines).strip()


# ═══════════════════════════════════════════════════════════════════════════════
# 工具注册（供 tools/__init__.py 自动发现）
# ═══════════════════════════════════════════════════════════════════════════════


def get_web_tools() -> list:
    """返回 Web 工具列表（供自动发现）。"""
    return [
        web_fetch,   # type: ignore[list-item]
        web_search,  # type: ignore[list-item]
    ]
