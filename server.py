"""一体化 MCP 工具:搜索(百度/必应/360/搜狗) + Crawl4AI 整页解析 + LM Studio 图片描述。

完全本地、零外部 API、零密钥:
- 搜索   = 本地直接抓取搜索引擎结果页(engines.py)
- 抓取   = 本地 Crawl4AI + 本地 Chromium
- 图片描述 = 本地 LM Studio 视觉模型(vision.py)

MCP 传输层:仅用 Python 标准库手写的 MCP stdio(JSON-RPC over stdio,newline-delimited),
不依赖 mcp / pydantic,避免额外安装负担。通过 DSH 的 @deepseek-ai/dsh-mcp-client 接入。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from urllib.parse import urljoin

import httpx

import cache as _cache
from config import CONFIG
from engines import HEADERS, _soup, resolve_url, search, search_multi
from vision import DEFAULT_PROMPT, chat, fetch_and_describe

SERVER_NAME = "web-search-mcp"
SERVER_VERSION = "1.0.0"

# crawl4ai 数据目录:默认项目内自包含(见 config.py 的 crawl4ai_base_dir)
os.environ.setdefault(
    "CRAWL4_AI_BASE_DIRECTORY",
    CONFIG["crawl4ai_base_dir"] or os.path.dirname(os.path.abspath(__file__)),
)

# ---- 所有可变配置集中在 config.py,这里读取为便捷别名 ----
VISION_BASE_URL = CONFIG["vision_base_url"]
VISION_API_KEY = CONFIG["vision_api_key"]
VISION_MODEL = CONFIG["vision_model"]
LLM_BASE_URL = CONFIG["llm_base_url"]
LLM_API_KEY = CONFIG["llm_api_key"]
SMALL_MODEL = CONFIG["small_model"]
LARGE_MODEL = CONFIG["large_model"]
SMALL_MODEL_BASE_URL = CONFIG["small_model_base_url"]
LARGE_MODEL_BASE_URL = CONFIG["large_model_base_url"]
MODEL_SWITCHING = CONFIG["model_switching"]


def _small_url() -> str:
    """切换模式下小模型走同一实例;否则用独立的 small_model_base_url。"""
    return LLM_BASE_URL if MODEL_SWITCHING else SMALL_MODEL_BASE_URL


def _large_url() -> str:
    """切换模式下大模型走同一实例;否则用独立的 large_model_base_url。"""
    return LLM_BASE_URL if MODEL_SWITCHING else LARGE_MODEL_BASE_URL


def _lm_host(base_url: str) -> str:
    """从 /v1 端点推导 LM Studio 服务根(用于 /api/v1 模型管理)。"""
    return base_url.rstrip("/").rsplit("/v1", 1)[0]


async def _load_model(client, api_key, base_url, key: str) -> dict:
    """加载模型(LM Studio v1 API: POST /api/v1/models/load)。"""
    r = await client.post(
        _lm_host(base_url) + "/api/v1/models/load",
        json={"model": key},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


async def _unload_model(client, api_key, base_url, key: str) -> dict:
    """卸载模型(POST /api/v1/models/unload)。"""
    r = await client.post(
        _lm_host(base_url) + "/api/v1/models/unload",
        json={"instance_id": key},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


async def _switch_to(client, api_key, base_url, target: str, unload_other: str) -> None:
    """切到 target 模型:先卸载 unload_other,再加载 target。失败不致命(后续 chat 会报错)。"""
    if unload_other and unload_other != target:
        try:
            await _unload_model(client, api_key, base_url, unload_other)
        except Exception:
            pass
    try:
        await _load_model(client, api_key, base_url, target)
    except Exception:
        pass


def _new_client() -> httpx.AsyncClient:
    # trust_env=False:本工具全部走直连(大陆搜索 + 本地 LM Studio),
    # 避免系统代理(如 Clash)拦截 localhost 导致 502。
    return httpx.AsyncClient(
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=30,
        follow_redirects=True,
        trust_env=False,
    )


# 明显的页脚/版权/广告噪声行关键词(只删这些无歧义的)
_NOISE_MARKERS = (
    "备案号", "copyright", "©", "版权所有", "关注微信", "二维码", "回到顶部",
    "意见反馈", "免责声明", "关于我们", "加入我们", "友情链接", "站内搜索",
    "advertisement", "赞助", "投放广告",
)

# 纯链接行: `* [text](url)` / `1. [text](url)` / `# [text](url)` 等导航条特征
# url 里可能含括号(如 javascript:void(0)),用 \(.*\) 贪婪匹配到行尾最后一个 )
_LINK_LINE = re.compile(r'^\s*(?:[-*+]|\d+[.)]|#{1,6})\s+\[[^\]]*\]\(.*\)\s*$')


def _strip_leading_nav(lines: list[str]) -> list[str]:
    """剥离开头的连续纯链接导航块(>=5 个连续纯链接行才判为导航条)。"""
    i = 0
    while i < len(lines) and _LINK_LINE.match(lines[i].strip()):
        i += 1
    return lines[i:] if i >= 5 else lines


def _clean_markdown(md: str) -> str:
    """轻量去噪:剥离顶部导航条 + 删除页脚/版权/广告噪声行 + 压缩连续空行。"""
    if not md:
        return ""
    lines = [l.rstrip() for l in md.splitlines()]
    lines = _strip_leading_nav(lines)
    kept = []
    for line in lines:
        low = line.strip().lower()
        if any(m in low for m in _NOISE_MARKERS):
            continue
        kept.append(line)
    out, prev_blank = [], False
    for line in kept:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    return "\n".join(out).strip()


# ============================================================
# 工具定义(JSON Schema)
# ============================================================

TOOLS = [
    {
        "name": "search_web",
        "description": (
            "搜索网页(直接抓取搜索引擎结果页,零 API、零密钥)。"
            "engine 可选: baidu(默认,百度) / bing(必应国内版,最稳定) / 360 / sogou。"
            "返回标题、URL、摘要片段。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "engine": {
                    "type": "string",
                    "description": "搜索引擎: baidu / bing / 360 / sogou",
                    "enum": ["baidu", "bing", "360", "sogou"],
                    "default": CONFIG["default_engine"],
                },
                "max_results": {
                    "type": "number",
                    "description": "返回结果条数(1-30)",
                    "default": CONFIG["search_max_results"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_multi",
        "description": (
            "多搜索引擎综合搜索:并发查询百度/必应/360/搜狗,按 URL 去重合并,"
            "覆盖更全、结果更综合。每条结果带 engine 字段标明来源;单引擎失败不影响整体。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "engines": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["baidu", "bing", "360", "sogou"]},
                    "description": "参与综合的搜索引擎列表,默认全部(baidu/bing/360/sogou)",
                },
                "max_results": {
                    "type": "number",
                    "description": "每个引擎返回的结果条数(1-20)",
                    "default": CONFIG["multi_max_results"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "scrape_url",
        "description": (
            "用 Crawl4AI 解析整个网页(结构 + 文本 + 图片),"
            "并可用本地 LM Studio 视觉模型把每张图片转成中文文字描述。"
            "返回 markdown(净化全文)、links(链接)、images(图片URL+alt)、image_descriptions(图片描述)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要解析的网页 URL"},
                "include_markdown": {"type": "boolean", "default": True},
                "full_markdown": {
                    "type": "boolean",
                    "description": "true=完整 markdown(含导航/广告);false=过滤后正文(默认,推荐)",
                    "default": False,
                },
                "max_chars": {
                    "type": "number",
                    "description": "markdown 最大字符数(超出截断,控制上下文占用)",
                    "default": CONFIG["scrape_max_chars"],
                },
                "include_links": {"type": "boolean", "default": True},
                "include_images": {"type": "boolean", "default": True},
                "describe_images": {
                    "type": "boolean",
                    "description": "是否用本地视觉模型描述图片(需设置 VISION_MODEL,默认关,较慢)",
                    "default": False,
                },
                "max_images": {"type": "number", "description": "最多描述多少张图片", "default": CONFIG["scrape_max_images"]},
                "image_prompt": {"type": "string", "description": "图片描述提示词(默认中文简洁描述)", "default": ""},
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_and_extract",
        "description": (
            "搜索并自动抓取、解析前 N 条结果,一步到位。"
            "use_llm_extract=true 时走三阶段 LLM 提取,并汇总成一份综合总结(需加载 SMALL/LARGE_MODEL)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "engine": {
                    "type": "string",
                    "enum": ["baidu", "bing", "360", "sogou"],
                    "default": CONFIG["default_engine"],
                },
                "max_results": {"type": "number", "description": "抓取前几条结果", "default": 3},
                "use_llm_extract": {
                    "type": "boolean",
                    "description": "true=三阶段 LLM 提取并出综合总结;false=返回原始 markdown",
                    "default": CONFIG["search_extract_use_llm"],
                },
                "describe_images": {"type": "boolean", "default": False},
                "max_images": {"type": "number", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "llm_extract",
        "description": (
            "三阶段智能提取:①规则过滤抓取的网页正文 → ②小模型逐块快速提取有用信息 → "
            "③大模型汇总成连贯总结。彻底去除广告/导航噪声,输出高价值摘要。"
            "需在 LM Studio 加载 SMALL_MODEL(快速提取)与 LARGE_MODEL(汇总)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要提取的网页 URL"},
                "query": {"type": "string", "description": "提取目标/问题(可选,用于引导)", "default": ""},
                "instruction": {"type": "string", "description": "具体提取指令(可选,更精确)", "default": ""},
                "max_chars": {"type": "number", "description": "阶段1规则过滤后正文上限", "default": CONFIG["extract_max_chars"]},
                "chunk_chars": {"type": "number", "description": "阶段2分块大小(喂给小模型每块字符数)", "default": CONFIG["extract_chunk_chars"]},
            },
            "required": ["url"],
        },
    },
]


# ============================================================
# 核心实现
# ============================================================

def _extract_images(result, page_url: str) -> list[dict]:
    imgs: list[dict] = []
    media = getattr(result, "media", None)
    if isinstance(media, dict):
        for it in media.get("images", []) or []:
            if isinstance(it, dict):
                imgs.append({"src": it.get("src") or it.get("url") or "", "alt": it.get("alt") or ""})
            elif isinstance(it, str):
                imgs.append({"src": it, "alt": ""})
    if not imgs:
        html = getattr(result, "cleaned_html", None) or getattr(result, "html", "") or ""
        if html:
            soup = _soup(html)
            for tag in soup.find_all("img"):
                src = tag.get("src") or tag.get("data-src") or tag.get("data-original") or ""
                if src and not src.startswith("data:"):
                    imgs.append({"src": src, "alt": tag.get("alt") or ""})
    seen, out = set(), []
    for im in imgs:
        src = (im.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        full = urljoin(page_url, src)
        if full in seen:
            continue
        seen.add(full)
        out.append({"src": full, "alt": (im.get("alt") or "").strip()})
    return out


async def _describe_images(
    client: httpx.AsyncClient,
    images: list[dict],
    max_images: int,
    prompt: str,
    page_url: str,
) -> dict:
    if not VISION_MODEL:
        return {"status": "skipped", "reason": "未设置 VISION_MODEL 环境变量"}
    prompt = prompt or DEFAULT_PROMPT
    out, failed = [], 0
    for im in images[: max(1, max_images)]:
        entry = {"src": im["src"], "alt": im.get("alt", ""), "description": ""}
        try:
            entry["description"] = await fetch_and_describe(
                client, VISION_BASE_URL, VISION_API_KEY, VISION_MODEL,
                im["src"], page_url, prompt,
            )
        except Exception as e:  # 视觉失败不影响整页抓取
            entry["description"] = f"[描述失败] {type(e).__name__}: {e}"
            failed += 1
        out.append(entry)
    return {
        "status": "ok" if failed == 0 else "partial",
        "model": VISION_MODEL,
        "described": len(out) - failed,
        "failed": failed,
        "images": out,
    }


async def _do_scrape(
    client: httpx.AsyncClient,
    url: str,
    query: str = "",
    include_markdown: bool = True,
    full_markdown: bool = False,
    max_chars: int = 20000,
    include_links: bool = True,
    include_images: bool = True,
    describe_images: bool = False,
    max_images: int = 5,
    image_prompt: str = "",
) -> dict:
    # 路线 A · F3:磁盘缓存——同一 URL + 同一参数直接命中,
    # 省去重复的 Chromium 渲染(命中时无需 crawl4ai,未安装也能返回缓存)。
    ck = {
        "url": url,
        "query": query,
        "include_markdown": include_markdown,
        "full_markdown": full_markdown,
        "max_chars": max_chars,
        "include_links": include_links,
        "include_images": include_images,
        "describe_images": describe_images,
        "max_images": max_images,
        "image_prompt": image_prompt,
    }
    hit = _cache.get("scrape", **ck)
    if hit is not None:
        hit = dict(hit)
        hit["_from_cache"] = True
        return hit

    try:
        from crawl4ai import (
            AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig,
            DefaultMarkdownGenerator, PruningContentFilter, BM25ContentFilter,
        )
    except ImportError:
        raise RuntimeError(
            "未安装 crawl4ai。请先执行: pip install crawl4ai && playwright install chromium"
        )

    # 有查询词时用 BM25 做「查询相关性」过滤(只留与查询相关的正文);
    # 否则用 PruningContentFilter 做通用「链接密度」去噪(剪导航/广告/页脚)。
    if query:
        content_filter = BM25ContentFilter(
            user_query=query, language="chinese", use_stemming=False,
        )
    else:
        content_filter = PruningContentFilter()
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        markdown_generator=DefaultMarkdownGenerator(content_filter=content_filter),
    )
    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
        result = await crawler.arun(url=url, config=config)

    if not getattr(result, "success", True):
        raise RuntimeError(f"抓取失败: {getattr(result, 'error_message', '未知错误')}")

    data = {
        "url": str(getattr(result, "url", url)),
        "title": (getattr(result, "metadata", None) or {}).get("title", ""),
        "metadata": getattr(result, "metadata", None) or {},
    }

    if include_markdown:
        raw = getattr(result, "markdown", "") or ""
        fit = getattr(result, "fit_markdown", None) or ""
        if full_markdown:
            md, mode = raw, "full"
        elif fit and len(fit) >= 200:
            # 过滤版足够长才采用;过短(过度剪枝)则回退原始
            md, mode = fit, "filtered"
        else:
            md, mode = raw, "full-fallback"
        md = _clean_markdown(md)
        data["markdown"] = md[:max_chars]
        data["markdown_mode"] = mode
        data["markdown_chars"] = len(md)
        data["markdown_truncated"] = len(md) > max_chars

    if include_links:
        links = getattr(result, "links", None)
        data["links"] = links if isinstance(links, dict) else {"external": links or []}

    images = _extract_images(result, url)
    if include_images:
        data["images"] = images
    if describe_images and images:
        data["image_descriptions"] = await _describe_images(client, images, max_images, image_prompt, url)

    _cache.put("scrape", data, **ck)  # 路线 A · F3:写缓存
    return data


async def _search_and_extract(
    client,
    query,
    engine,
    max_results,
    describe_images,
    max_images,
    use_llm_extract=False,
    extract_max_chars=20000,
    extract_chunk_chars=4000,
) -> dict:
    """搜索 → 还原跳转 → 抓取 →(可选)三阶段 LLM 提取。

    路线 A 优化:
    - F4:URL 还原只读响应头,不下载正文(engines.resolve_url);
    - F2:并行还原 URL + 并行抓取(Semaphore 限流,Chromium 实例数受内存约束);
    - F1:LLM 模式下「切换」从每页 2 次降到每次调用 2 次——
         阶段1 并行抓完所有正文 → 阶段2 切到小模型一次,逐页逐块提取 →
         阶段3 切回大模型一次,逐页汇总 + 跨页综合总结。
    """
    s = await search(query, engine=engine, max_results=max_results)
    results = s["results"][: max(1, max_results)]
    out = {"query": query, "engine": engine, "search_results": s["results"], "pages": []}

    # 并行还原跳转链接(F4:只读头部,不下载正文)
    async def _resolve(r):
        raw = (r.get("url") or "").strip()
        if not raw:
            return None
        try:
            return await resolve_url(raw, client)
        except Exception:
            return raw

    reals = await asyncio.gather(*[_resolve(r) for r in results])

    entries = []
    for r, real in zip(results, reals):
        if not real:
            continue
        entries.append({"title": r.get("title", ""), "search_snippet": r.get("snippet", ""), "url": real})
    out["pages"] = entries

    sem = asyncio.Semaphore(max(1, int(CONFIG["scrape_concurrency"])))
    small_model = SMALL_MODEL or LARGE_MODEL
    llm_mode = use_llm_extract and bool(small_model)

    # ---- 普通模式:并行抓取整页(F2) ----
    if not llm_mode:
        async def _one(entry):
            try:
                async with sem:
                    entry["page"] = await _do_scrape(
                        client, entry["url"],
                        query=query,
                        include_markdown=True,
                        include_links=False,
                        include_images=True,
                        describe_images=describe_images,
                        max_images=max_images,
                    )
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"

        await asyncio.gather(*[_one(e) for e in entries])
        if use_llm_extract and not small_model:
            out["llm_note"] = "未设置 SMALL_MODEL/LARGE_MODEL,已降级为普通抓取"
        return out

    # ---- LLM 三阶段流水线(F1:切换只发生在阶段边界,共 2 次) ----

    # 阶段1:并行抓取各页正文(规则过滤 + 缓存;不带图片,提取用不到)
    async def _scrape_md(entry):
        try:
            async with sem:
                return await _do_scrape(
                    client, entry["url"],
                    query=query,
                    include_markdown=True,
                    include_links=False,
                    include_images=False,
                    describe_images=False,
                    max_chars=extract_max_chars,
                )
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    scraped = await asyncio.gather(*[_scrape_md(e) for e in entries])

    # 阶段2:切到小模型一次,逐页逐块快速提取
    if MODEL_SWITCHING and small_model != LARGE_MODEL:
        await _switch_to(client, LLM_API_KEY, LLM_BASE_URL, small_model, LARGE_MODEL)
    for entry, sc in zip(entries, scraped):
        sc = sc or {}
        md = sc.get("markdown", "")
        if not md:
            entry["extract"] = {"error": sc.get("error") or "网页抓取为空,无法提取"}
            continue
        extractions = await _extract_chunks(
            client, md, entry.get("search_snippet", ""), extract_chunk_chars, small_model,
        )
        entry["extract"] = {
            "url": sc.get("url", entry["url"]),
            "title": entry["title"],
            "stage1": {
                "markdown_chars": len(md),
                "chunks": len(extractions),
                "mode": sc.get("markdown_mode"),
            },
            "stage2": {"model": small_model, "extractions": extractions},
        }

    # 阶段3:切回大模型一次,逐页汇总 + 跨页综合总结
    if LARGE_MODEL:
        if MODEL_SWITCHING and small_model != LARGE_MODEL:
            await _switch_to(client, LLM_API_KEY, LLM_BASE_URL, LARGE_MODEL, small_model)
        for entry in entries:
            ex = entry.get("extract") or {}
            if ex.get("error") or "stage2" not in ex:
                continue
            try:
                summ = await _summarize_extractions(
                    client, ex["stage2"]["extractions"], query, entry.get("search_snippet", ""),
                )
                ex["stage3"] = {"model": LARGE_MODEL, "summary": summ}
            except Exception as e:
                ex["stage3"] = {"model": LARGE_MODEL, "summary": f"[汇总失败] {type(e).__name__}: {e}"}

        parts = []
        for p in entries:
            summ = ((p.get("extract") or {}).get("stage3") or {}).get("summary") or ""
            if summ and not summ.startswith("[汇总失败]"):
                parts.append(f"【{p.get('title', '')}】\n{summ}")
        if parts:
            try:
                out["final_summary"] = await chat(
                    client, _large_url(), LLM_API_KEY, LARGE_MODEL,
                    [{"role": "user", "content": _final_summary_prompt("\n\n".join(parts), query)}],
                    max_tokens=2000, temperature=0.3,
                )
            except Exception as e:
                out["final_summary"] = f"[综合总结失败] {type(e).__name__}: {e}"
    else:
        for entry in entries:
            ex = entry.get("extract") or {}
            if not ex.get("error"):
                ex.setdefault("stage3", {"model": None, "summary": None})

    return out


# ============================================================
# llm_extract:规则过滤 → 小模型逐块提取 → 大模型汇总
# ============================================================

def _chunk_text(text: str, chunk_chars: int = 4000) -> list[str]:
    """按段落边界把长文本切成约 chunk_chars 的小块(尽量不切断段落)。"""
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    cur = ""
    for p in text.split("\n\n"):
        p = p.strip()
        if not p:
            continue
        if cur and len(cur) + len(p) + 2 > chunk_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _extract_prompt(chunk: str, instruction: str) -> str:
    head = (
        "请阅读下面的网页片段,提取其中有价值的信息,忽略导航/广告/页脚等无关内容。"
        "用简洁的中文要点输出,每条一行、以\"- \"开头,保留具体数据、数字、名称、结论。"
        "直接输出要点,不要输出思考过程。"
    )
    if instruction:
        head += f"\n\n提取重点: {instruction}"
    return f"{head}\n\n网页片段:\n{chunk}\n\n提取要点:"


def _agg_prompt(extractions: str, query: str, instruction: str) -> str:
    head = (
        "下面是从一个网页中分块提取的要点。请把它们汇总成一份结构清晰、连贯的中文总结:"
        "去重合并相关内容,突出核心信息与关键数据,忽略琐碎内容。"
    )
    if query or instruction:
        head += f"\n\n任务: {query or instruction}"
    return f"{head}\n\n提取要点:\n{extractions}\n\n汇总:"


def _final_summary_prompt(summaries: str, query: str) -> str:
    """跨网页综合总结提示词(search_and_extract + use_llm_extract 用)。"""
    return (
        f"以下是从多个网页分别提取的总结。请围绕「{query}」综合成一份最终的中文总结报告:\n"
        "1. 合并相同观点、去重,突出共识与关键数据\n"
        "2. 结构清晰(分段或分点),可指出不同来源的差异\n"
        "3. 客观准确,不编造\n\n"
        f"各网页总结:\n{summaries}\n\n最终综合总结:"
    )


async def _chat_any(client, base_url, api_key, models, messages, **kw):
    """依次尝试多个模型名,返回首个成功的文本;全部失败抛最后一个异常。

    用途:LM Studio 对小模型的注册名可能有/无发布者前缀两种形式,
    这里自动兜底,避免因 id 规则差异导致提取失败。
    """
    last = None
    for m in models:
        try:
            return await chat(client, base_url, api_key, m, messages, **kw)
        except Exception as e:
            last = e
    raise last


async def _extract_chunks(
    client, md: str, instruction: str, chunk_chars: int, small_model: str,
) -> list[dict]:
    """阶段2:小模型逐块快速提取。

    不处理模型切换——切换由调用方在循环外统一完成(路线 A · F1)。
    """
    small_url = _small_url()
    # 候选名:配置的小模型名 + 去掉发布者前缀的备用名
    small_names = list(dict.fromkeys([small_model, small_model.split("/")[-1]]))
    chunks = _chunk_text(md, max(500, chunk_chars))
    extractions: list[dict] = []
    for i, ch in enumerate(chunks, 1):
        try:
            out = await _chat_any(
                client, small_url, LLM_API_KEY, small_names,
                [{"role": "user", "content": _extract_prompt(ch, instruction)}],
                max_tokens=1500, temperature=0.1, reasoning_effort="none",
            )
        except Exception as e:
            out = f"[块{i}提取失败] {type(e).__name__}: {e}"
        extractions.append({"chunk": i, "chars": len(ch), "extraction": out})
    return extractions


async def _summarize_extractions(
    client, extractions: list[dict], query: str, instruction: str,
) -> str:
    """阶段3:大模型把分块要点汇总成连贯总结。

    不处理模型切换——切换由调用方在循环外统一完成(路线 A · F1)。
    """
    combined = "\n".join(x["extraction"] for x in extractions)
    return await chat(
        client, _large_url(), LLM_API_KEY, LARGE_MODEL,
        [{"role": "user", "content": _agg_prompt(combined, query, instruction)}],
        max_tokens=1500, temperature=0.3,
    )


async def _llm_extract(
    client: httpx.AsyncClient,
    url: str,
    query: str,
    instruction: str,
    max_chars: int,
    chunk_chars: int,
) -> dict:
    """单页三阶段提取(llm_extract 工具)。切换只在阶段边界发生,共 2 次。"""
    # 阶段1:规则初步过滤(抓取 + PruningContentFilter + 去噪 + 截断)
    scraped = await _do_scrape(
        client, url, query=query,
        include_markdown=True, include_links=False, include_images=False,
        describe_images=False, max_chars=max_chars,
    )
    md = scraped.get("markdown", "")
    if not md:
        return {"error": "网页抓取为空,无法提取", "stage1": scraped}

    small_model = SMALL_MODEL or LARGE_MODEL
    if not small_model:
        return {
            "error": "未设置 SMALL_MODEL 或 LARGE_MODEL 环境变量",
            "stage1": {"url": scraped.get("url"), "title": scraped.get("title"),
                        "markdown_chars": len(md)},
        }

    # 切换模式:先切到小模型(卸载大模型),避免两个模型同时占显存
    if MODEL_SWITCHING and small_model != LARGE_MODEL:
        await _switch_to(client, LLM_API_KEY, LLM_BASE_URL, small_model, LARGE_MODEL)

    # 阶段2:小模型逐块快速提取
    extractions = await _extract_chunks(client, md, instruction, chunk_chars, small_model)

    # 阶段3:大模型汇总(未配 LARGE_MODEL 则跳过)
    # 切换模式:切回大模型(卸载小模型)
    final = None
    if LARGE_MODEL:
        if MODEL_SWITCHING and small_model != LARGE_MODEL:
            await _switch_to(client, LLM_API_KEY, LLM_BASE_URL, LARGE_MODEL, small_model)
        try:
            final = await _summarize_extractions(client, extractions, query, instruction)
        except Exception as e:
            final = f"[汇总失败] {type(e).__name__}: {e}"

    return {
        "url": scraped.get("url"),
        "title": scraped.get("title"),
        "stage1": {"markdown_chars": len(md), "chunks": len(extractions), "mode": scraped.get("markdown_mode")},
        "stage2": {"model": small_model, "extractions": extractions},
        "stage3": {"model": LARGE_MODEL or None, "summary": final},
    }


async def call_tool(name: str, args: dict) -> dict:
    async with _new_client() as client:
        if name == "search_web":
            return await search(
                args.get("query", ""),
                engine=args.get("engine", CONFIG["default_engine"]),
                max_results=int(args.get("max_results", CONFIG["search_max_results"])),
            )
        if name == "search_multi":
            return await search_multi(
                args.get("query", ""),
                engines=args.get("engines") or CONFIG["multi_engines"],
                max_results=int(args.get("max_results", CONFIG["multi_max_results"])),
            )
        if name == "scrape_url":
            return await _do_scrape(
                client,
                args["url"],
                include_markdown=args.get("include_markdown", True),
                full_markdown=args.get("full_markdown", False),
                max_chars=int(args.get("max_chars", CONFIG["scrape_max_chars"])),
                include_links=args.get("include_links", True),
                include_images=args.get("include_images", True),
                describe_images=args.get("describe_images", CONFIG["scrape_describe_images"]),
                max_images=int(args.get("max_images", CONFIG["scrape_max_images"])),
                image_prompt=args.get("image_prompt", ""),
            )
        if name == "search_and_extract":
            return await _search_and_extract(
                client,
                args.get("query", ""),
                args.get("engine", CONFIG["default_engine"]),
                int(args.get("max_results", 3)),
                args.get("describe_images", CONFIG["scrape_describe_images"]),
                int(args.get("max_images", 3)),
                args.get("use_llm_extract", CONFIG["search_extract_use_llm"]),
                int(args.get("extract_max_chars", CONFIG["extract_max_chars"])),
                int(args.get("extract_chunk_chars", CONFIG["extract_chunk_chars"])),
            )
        if name == "llm_extract":
            return await _llm_extract(
                client,
                args["url"],
                args.get("query", ""),
                args.get("instruction", ""),
                int(args.get("max_chars", CONFIG["extract_max_chars"])),
                int(args.get("chunk_chars", CONFIG["extract_chunk_chars"])),
            )
    raise ValueError(f"未知工具: {name}")


# ============================================================
# MCP stdio 协议(JSON-RPC 2.0,newline-delimited)
# ============================================================

def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _sanitize(obj):
    """递归清除字符串里的孤立代理项(lone surrogate),避免 UTF-8 编码失败。"""
    if isinstance(obj, str):
        return "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c for c in obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(x) for x in obj]
    return obj


def handle_message(msg: dict):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "notifications/initialized":
        return None  # 通知,无需响应

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = asyncio.run(call_tool(name, arguments))
            text = json.dumps(_sanitize(result), ensure_ascii=False, indent=2)
            return _ok(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            return _ok(msg_id, {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            })

    if msg_id is not None:
        return _err(msg_id, -32601, f"Method not found: {method}")
    return None


def main() -> None:
    # 强制 stdin/stdout/stderr 都用 UTF-8:DSH 的 MCP 客户端以 UTF-8 读写 stdio,
    # 避免中文 Windows 默认 GBK 导致乱码或代理项错误。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
