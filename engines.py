"""搜索引擎抓取模块。

直接抓取中国大陆可直接访问的搜索引擎结果页(Baidu / Bing 国内版 / 360 / 搜狗),
模拟浏览器请求,不调用任何第三方搜索 API、不需要任何密钥。

返回统一结构: {"query", "engine", "count", "results": [{title, url, snippet}]}
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

import cache as _cache
from breaker import EngineCircuitBreaker
from config import CONFIG
from rank import rerank, score_result, parse_query, domain_token_match


BREAKER = EngineCircuitBreaker(
    fail_threshold=CONFIG.get("engine_breaker_fail_threshold", 3),
    cooldown_sec=CONFIG.get("engine_breaker_cooldown_sec", 300),
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 百度触发安全验证时,页面标题或验证域名会出现这些标记。
_BAIDU_VERIFY_TITLE_MARKERS = ("百度安全验证", "安全验证")
_BAIDU_VERIFY_DOMAINS = ("wappass.baidu.com", "verify.baidu.com")


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _result(title: str, url: str, snippet: str) -> dict:
    return {"title": title, "url": url, "snippet": snippet}


def _soup(html: str) -> BeautifulSoup:
    """优先用 lxml(更健壮),未安装时回退到标准库 html.parser。"""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


async def _compensate_snippet(results: list[dict], query: str) -> list[dict]:
    """Repair missing SERP snippets with bounded keyword-in-context page text.

    This is best-effort only.  It is deliberately bounded and concurrent so a changed
    360 layout does not turn one search into an unbounded sequential crawl.
    """
    phrases, terms = parse_query(query)
    keywords = [k for k in terms + phrases if len(k) >= 2]
    if not keywords:
        return results

    threshold = int(CONFIG.get("snippet_compensation_min_chars", 20))
    limit = max(0, int(CONFIG.get("snippet_compensation_max_results", 3)))
    targets = [r for r in results if len((r.get("snippet") or "").strip()) < threshold and r.get("url")][:limit]
    if not targets:
        return results

    async def repair(client: httpx.AsyncClient, result: dict) -> None:
        try:
            response = await client.get(result["url"])
            response.raise_for_status()
            text = _soup(response.text).get_text(" ", strip=True)[:2000]
            lower = text.lower()
            for keyword in keywords:
                index = lower.find(keyword.lower())
                if index >= 0:
                    start, end = max(0, index - 100), min(len(text), index + len(keyword) + 100)
                    result["snippet"] = f"...{text[start:end]}..."
                    result["snippet_source"] = "fallback_crawl"
                    return
            if text:
                result["snippet"] = f"{text[:150]}..."
                result["snippet_source"] = "fallback_crawl_preview"
        except Exception:
            # A supplemental crawl must never invalidate an otherwise usable result.
            return

    timeout = float(CONFIG.get("snippet_compensation_timeout_sec", 8))
    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True, trust_env=False) as client:
        await asyncio.gather(*(repair(client, result) for result in targets))
    return results


def _quality_average(results: list[dict]) -> float:
    """Return the mean relevance of up to the first three ranked candidates."""
    sample = results[:3]
    return sum(float(result.get("relevance", 0.0)) for result in sample) / len(sample) if sample else 0.0


def heuristic_fallback_extract(html: str, query: str, base_url: str, max_results: int) -> list[dict]:
    """Conservative structural fallback for layout drift, not a bypass mechanism.

    It only accepts visible HTTP(S) links with a plausible title and removes obvious
    navigation/javascript links.  Returning an empty list remains valid for a genuine
    no-result page.
    """
    terms = [term.lower() for term in parse_query(query)[1] if len(term) > 1]
    candidates, seen = [], set()
    for a in _soup(html).find_all("a", href=True):
        title = _text(a)
        href = urljoin(base_url, a.get("href", ""))
        if not href.startswith(("http://", "https://")) or not 8 <= len(title) <= 160:
            continue
        if href in seen or (terms and not any(term in title.lower() for term in terms)):
            continue
        parent = a.parent
        context = _text(parent)
        snippet = context.replace(title, "", 1).strip()[:300] if context else ""
        candidates.append(_result(title, href, snippet))
        seen.add(href)
        if len(candidates) >= max_results:
            break
    return candidates


async def search_baidu(query: str, max_results: int = 10) -> list[dict]:
    url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": max_results, "ie": "utf-8", "f": "8"}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
    html = r.text
    soup = _soup(html)
    title_text = _text(soup.find("title"))
    if (
        any(marker in title_text for marker in _BAIDU_VERIFY_TITLE_MARKERS)
        or any(domain in html for domain in _BAIDU_VERIFY_DOMAINS)
    ):
        raise RuntimeError("百度触发了安全验证(反爬),请稍后重试,或改用 engine='bing' / '360'")

    out: list[dict] = []
    # 百度结果容器和标题/摘要的 class 经常变,这里做多套兜底
    for div in soup.select("div.result, div.c-container, div[class*='c-container']"):
        a = div.select_one("h3 a") or div.select_one("h3[class*='title'] a")
        if not a:
            continue
        title = _text(a)
        href = urljoin("https://www.baidu.com/", a.get("href") or "")
        snip = div.select_one(
            "span[class*='content-right'], span.c-abstract, div.c-abstract, "
            "[class*='abstract'], .c-span-last, div[class*='content']"
        )
        snippet = _text(snip)
        if not title and not href:
            continue
        out.append(_result(title, href, snippet))
        if len(out) >= max_results:
            break
    return out or heuristic_fallback_extract(html, query, "https://www.baidu.com/", max_results)


async def search_bing(query: str, max_results: int = 10) -> list[dict]:
    # cn.bing.com 为中国大陆可直接访问的必应,结构稳定、反爬较弱。
    # 已知限制:对中文多词查询排序极度偏向单字词,例如 "东铁线 旺角东 红磡"
    # 会被"东"字字典页劫持。+运算符/AND/引号/去空格均无法修复。
    # 复杂中文查询建议用 baidu / 360,或使用 search_multi 多引擎综合。
    url = "https://cn.bing.com/search"
    params = {"q": query, "count": max_results, "setlang": "zh-hans", "mkt": "zh-CN"}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
    soup = _soup(r.text)
    out: list[dict] = []
    for li in soup.select("li.b_algo"):
        h2 = li.select_one("h2 a")
        if not h2:
            continue
        title = _text(h2)
        href = urljoin("https://cn.bing.com/", h2.get("href") or "")
        cap = li.select_one(".b_caption p, .b_caption, p")
        snippet = _text(cap)
        out.append(_result(title, href, snippet))
        if len(out) >= max_results:
            break
    return out or heuristic_fallback_extract(r.text, query, "https://cn.bing.com/", max_results)


async def search_360(query: str, max_results: int = 10) -> list[dict]:
    url = "https://www.so.com/s"
    params = {"q": query}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
    soup = _soup(r.text)
    out: list[dict] = []
    for li in soup.select("li.res-list, li[class*='res-list']"):
        a = li.select_one("h3 a, h3[class*='title'] a")
        if not a:
            continue
        title = _text(a)
        href = urljoin("https://www.so.com/", a.get("href") or "")
        desc = li.select_one("p.res-desc, .res-desc, [class*='res-desc']")
        snippet = _text(desc)
        out.append(_result(title, href, snippet))
        if len(out) >= max_results:
            break
    return out or heuristic_fallback_extract(r.text, query, "https://www.so.com/", max_results)


async def search_sogou(query: str, max_results: int = 10) -> list[dict]:
    url = "https://www.sogou.com/web"
    params = {"query": query}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
    soup = _soup(r.text)
    out: list[dict] = []
    for div in soup.select("div.vrwrap, div.rb, div[class*='vrwrap']"):
        a = div.select_one("h3.vr-title a, h3 a")
        if not a:
            continue
        title = _text(a)
        href = urljoin("https://www.sogou.com/", a.get("href") or "")
        desc = div.select_one(".text-layout, .str-text-info, p")
        snippet = _text(desc)
        out.append(_result(title, href, snippet))
        if len(out) >= max_results:
            break
    return out or heuristic_fallback_extract(r.text, query, "https://www.sogou.com/", max_results)


ENGINES = {
    "baidu": search_baidu,
    "bing": search_bing,
    "360": search_360,
    "sogou": search_sogou,
}

# 引擎自动回退链(借鉴 SearXNG 的 engine fallback):主引擎失败或结果相关性过低时按序尝试。
# 主要针对 Bing CN 对中文多词查询的「单字词劫持」。
FALLBACK_CHAIN = {
    "bing": ["baidu", "360"],
    "sogou": ["baidu", "360"],
    "baidu": ["360", "bing"],
    "360": ["baidu", "bing"],
}


async def _engine_results(engine: str, query: str, cap: int) -> tuple[list[dict], bool]:
    """Fetch one engine and return ``(results, from_cache)``.

    The source flag lets callers distinguish an actual successful request from a cache
    hit, which must not reset a circuit breaker cooldown.
    """
    cache_key = {"engine": engine, "query": query, "cap": cap}
    ttl = float(CONFIG.get("search_cache_ttl_sec", 0) or 0)
    cached = _cache.get("search", ttl_sec=ttl, **cache_key) if ttl > 0 else None
    if cached is not None:
        return list(cached), True
    results = await ENGINES[engine](query, max_results=cap)
    if ttl > 0:
        _cache.put("search", results, **cache_key)
    return results, False


async def _engine_once(engine: str, query: str, cap: int):
    """调一个引擎并做相关性重排/过滤。返回 (kept, dropped, raw_count, from_cache)。"""
    results, from_cache = await _engine_results(engine, query, cap)
    raw_count = len(results)
    if CONFIG.get("relevance_filter", True):
        kept, dropped = rerank(
            query, results,
            min_score=float(CONFIG.get("relevance_min_score", 0.05)),
            relative=float(CONFIG.get("relevance_relative", 0.35)),
            phrase_gate=float(CONFIG.get("relevance_phrase_gate", 0.35)),
            domain_bonus=float(CONFIG.get("relevance_domain_bonus", 0.2)),
        )
    else:
        kept, dropped = results, 0
    return kept, dropped, raw_count, from_cache


async def search(query: str, engine: str = "baidu", max_results: int = 10) -> dict:
    """单引擎搜索 + 相关性重排过滤 + 引擎自动回退。

    - 相关性过滤(relevance_filter):按查询词给结果打分重排,过滤低分项;
    - 自动回退(engine_fallback):当引擎抛错(如反爬)或过滤后结果太少时,
      按 FALLBACK_CHAIN 依次尝试备选引擎,取「过滤后结果最多」的一次返回。
    返回里 engine 为实际产出结果的引擎;若发生回退,附 fallback_from 标注原始引擎。
    """
    engine = (engine or "baidu").lower()
    if engine not in ENGINES:
        raise ValueError(f"不支持的搜索引擎: {engine},可选: {', '.join(ENGINES)}")
    cap = max(1, min(max_results, 30))

    chain = [engine]
    if CONFIG.get("engine_fallback", True):
        chain += [e for e in FALLBACK_CHAIN.get(engine, []) if e not in chain]

    keep_min = int(CONFIG.get("relevance_keep_min", 2))
    best = None          # (kept, dropped, used_engine)
    errors: dict = {}
    skipped: dict = {}
    for eng in chain:
        if CONFIG.get("engine_breaker_enabled", True) and not await BREAKER.allow(eng):
            health = await BREAKER.status(eng)
            skipped[eng] = f"cooldown ({health['cooldown_remaining_sec']}s remaining)"
            continue
        try:
            kept, dropped, raw_count, from_cache = await _engine_once(eng, query, cap)
            # SERP parsers sometimes yield titles/URLs but lose their descriptions.
            # Repair before judging quality so recovered evidence participates in score.
            if kept and not from_cache:
                kept = await _compensate_snippet(kept, query)
                if CONFIG.get("relevance_filter", True):
                    kept, extra_dropped = rerank(
                        query, kept,
                        min_score=float(CONFIG.get("relevance_min_score", 0.05)),
                        relative=float(CONFIG.get("relevance_relative", 0.35)),
                        phrase_gate=float(CONFIG.get("relevance_phrase_gate", 0.35)),
                        domain_bonus=float(CONFIG.get("relevance_domain_bonus", 0.2)),
                    )
                    dropped += extra_dropped
        except Exception as ex:  # 引擎失败:记录并继续回退
            errors[eng] = f"{type(ex).__name__}: {ex}"
            if CONFIG.get("engine_breaker_enabled", True):
                await BREAKER.record_failure(eng, ex)
            continue
        if not from_cache and CONFIG.get("engine_breaker_enabled", True):
            await BREAKER.record_success(eng)

        quality = _quality_average(kept)
        min_quality = float(CONFIG.get("relevance_quality_min_avg", 0.15))
        quality_failed = len(kept) >= keep_min and quality < min_quality
        if quality_failed:
            # This is query-scoped fallback evidence, not an engine health failure:
            # a rare query must not open the transport circuit for a healthy engine.
            errors[f"{eng}_quality_fail"] = f"top-3 avg relevance too low: {quality:.3f}"
        if best is None or (len(kept), quality) > (len(best[0]), _quality_average(best[0])):
            best = (kept, dropped, eng)
        if len(kept) >= keep_min and not quality_failed:
            break

    if best is None:
        diagnostics = {**errors, **skipped}
        raise RuntimeError(f"所有搜索引擎均不可用: {diagnostics}")

    kept, dropped, used = best
    # Bounded adaptive retrieval: expand only once when the current engine supplied
    # candidates but their aggregate evidence remains weak.  The larger cap is part
    # of the cache key, so it cannot overwrite the shallow retrieval cache entry.
    adaptive_max = min(30, max(cap, int(CONFIG.get("adaptive_retrieval_max_results", 10))))
    adaptive_trigger = float(CONFIG.get("adaptive_retrieval_quality_trigger", 0.2))
    if (
        CONFIG.get("adaptive_retrieval_enabled", True)
        and CONFIG.get("relevance_filter", True)
        and kept
        and _quality_average(kept) < adaptive_trigger
        and cap < adaptive_max
    ):
        expanded = await search(query, engine=used, max_results=adaptive_max)
        expanded["adaptive_retrieval"] = {"initial_cap": cap, "expanded_cap": adaptive_max, "reason": "low_relevance"}
        return expanded

    out = {
        "query": query,
        "engine": used,
        "count": len(kept),
        "results": kept,
    }
    if used != engine:
        out["fallback_from"] = engine
    if dropped:
        out["filtered_out"] = dropped
    if errors:
        out["engine_errors"] = errors
    if skipped:
        out["engine_skipped"] = skipped
    return out


def _norm_url(url: str) -> str:
    """归一化 URL 用于去重:只去协议/去 www/去片段,保留查询串。

    注意:不能剥离查询串——百度/360 的跳转链接(link?url=... / link?m=...)
    的查询串才是真实目的地的标识,剥离会把不同结果塌缩成同一条。
    """
    u = (url or "").strip().lower()
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.split("#", 1)[0]


_TITLE_NORM_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _norm_title(title: str) -> str:
    """标题归一化(跨引擎近似去重键):小写,仅保留中文/字母/数字。"""
    return _TITLE_NORM_RE.sub("", (title or "").lower())


def _merge_by_title(merged: list[dict], query: str = "") -> list[dict]:
    """二次合并:归一化标题相同的条目合并(不同引擎的跳转链接常指向同一页面)。

    - engines 取并集,engine_count 相应重算(跨引擎共识越强排越前);
    - URL 保留更可能是直链的一条:非跳转链接(不含 /link?)优先,
      其次域名与查询词互含者(疑似官网)优先;
    - snippet 保留最长的(信息量最大),title 随之;
    - 落选 URL 记入 duplicates,信息不丢。
    空标题不参与合并。
    """
    phrases, terms = parse_query(query)
    dom_terms = terms + phrases

    def _is_jump(u: str) -> bool:
        return "/link?" in (u or "")

    def _url_better(new: str, base: str) -> bool:
        nj, bj = _is_jump(new), _is_jump(base)
        if nj != bj:
            return bj and not nj
        return domain_token_match(new, dom_terms) and not domain_token_match(base, dom_terms)

    by_title: dict[str, dict] = {}
    order: list[str] = []
    for idx, m in enumerate(merged):
        key = _norm_title(m.get("title", "")) or f"__notitle{idx}"
        if key not in by_title:
            order.append(key)
            by_title[key] = dict(m)
            continue
        base = by_title[key]
        for e in m.get("engines", []):
            if e not in base["engines"]:
                base["engines"].append(e)
        if len(m.get("snippet", "")) > len(base.get("snippet", "")):
            base["snippet"] = m["snippet"]
            if m.get("title"):
                base["title"] = m["title"]
        if _url_better(m.get("url", ""), base.get("url", "")):
            base.setdefault("duplicates", []).append(base["url"])
            base["url"] = m["url"]
        else:
            base.setdefault("duplicates", []).append(m.get("url", ""))
        base["_rank"] = min(base.get("_rank", idx), m.get("_rank", idx))

    out = []
    for key in order:
        m = by_title[key]
        m["engine_count"] = len(m["engines"])
        out.append(m)
    return out


async def search_multi(query: str, engines: Optional[list[str]] = None, max_results: int = 5) -> dict:
    """多搜索引擎综合搜索:并发查询多个引擎,按归一化 URL 聚合去重。

    - 单个引擎失败不影响整体(错误记入 errors)
    - 同一条结果命中多个引擎时合并,取最长摘要,并按命中引擎数加权排序
    - 每条结果带 engine(首个来源)、engine_count(命中引擎数)、engines(全部来源)
    """
    chosen = [e.lower() for e in (engines or list(ENGINES.keys())) if e.lower() in ENGINES]
    if not chosen:
        chosen = list(ENGINES.keys())
    cap = max(1, min(max_results, 20))

    async def _one(e: str):
        if CONFIG.get("engine_breaker_enabled", True) and not await BREAKER.allow(e):
            health = await BREAKER.status(e)
            return e, None, f"cooldown ({health['cooldown_remaining_sec']}s remaining)"
        try:
            results, from_cache = await _engine_results(e, query, cap)
            # Multi-search is where 360's absent SERP descriptions most hurt the
            # synthesis model, so repair fresh SERPs before choosing the longest snippet.
            if not from_cache:
                results = await _compensate_snippet(results, query)
        except Exception as ex:  # 单引擎失败不拖垮整体
            if CONFIG.get("engine_breaker_enabled", True):
                await BREAKER.record_failure(e, ex)
            return e, None, f"{type(ex).__name__}: {ex}"
        if not from_cache and CONFIG.get("engine_breaker_enabled", True):
            await BREAKER.record_success(e)
        return e, results, None

    agg: dict[str, dict] = {}
    order: list[str] = []
    per_engine: dict = {}
    errors: dict = {}

    for e, results, err in await asyncio.gather(*[_one(e) for e in chosen]):
        if err is not None:
            per_engine[e] = 0
            errors[e] = err
            continue
        per_engine[e] = len(results)
        for r in results:
            key = _norm_url(r.get("url", "")) or r.get("url", "")
            if key in agg:
                # 命中多个引擎:追加来源,保留更长的摘要
                agg[key]["engines"].append(e)
                if len(r.get("snippet", "")) > len(agg[key].get("snippet", "")):
                    agg[key]["snippet"] = r.get("snippet", "")
                    agg[key]["title"] = r.get("title", "") or agg[key]["title"]
            else:
                order.append(key)
                agg[key] = {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                    "engines": [e],
                }

    merged = []
    for idx, key in enumerate(order):
        a = agg[key]
        merged.append({
            "title": a["title"],
            "url": a["url"],
            "snippet": a["snippet"],
            "engine": a["engines"][0],
            "engine_count": len(a["engines"]),
            "engines": a["engines"],
            "_rank": idx,
        })

    # 二次合并:不同引擎跳转链接指向同一页面时,按归一化标题去重(取并集、留直链)
    merged = _merge_by_title(merged, query)

    # 加权排序:命中引擎越多越靠前;同引擎数按「与查询的相关度」降序,再按首次出现顺序。
    # 相关度排序能压住 Bing 单字词劫持带来的无关项;传 URL 以便域名权威加成。
    # 注意:search_multi 定位为综合广搜,只重排、不过滤——保留更多线索(含近义/期刊页),
    # 靠相关度把它们压到尾部即可,避免误删用户可能想要的实体。
    if CONFIG.get("relevance_filter", True):
        for m in merged:
            relevance = score_result(
                query, m["title"], m["snippet"], m.get("url", ""),
                phrase_gate=float(CONFIG.get("relevance_phrase_gate", 0.35)),
                domain_bonus=float(CONFIG.get("relevance_domain_bonus", 0.2)),
            )
            # A Bing-only hit has historically been particularly prone to CJK
            # single-character hijacking.  Keep authoritative domains exempt and use
            # a soft penalty so fresh/niche legitimate sources remain discoverable.
            if (
                m["engine_count"] == 1
                and m["engines"] == ["bing"]
                and not any(domain in (m.get("url") or "").lower() for domain in (".gov.cn", ".edu.cn", ".org.cn"))
            ):
                relevance *= float(CONFIG.get("bing_single_source_penalty", 0.4))
            m["relevance"] = round(relevance, 3)
        merged.sort(key=lambda x: (-x["engine_count"], -x["relevance"], x["_rank"]))
    else:
        merged.sort(key=lambda x: (-x["engine_count"], x["_rank"]))
    for m in merged:
        m.pop("_rank", None)

    return {
        "query": query,
        "engines_used": chosen,
        "count": len(merged),
        "per_engine": per_engine,
        "errors": errors,
        "results": merged,
    }


async def resolve_url(url: str, client: Optional[httpx.AsyncClient] = None) -> str:
    """跟随重定向,把百度/360 的跳转链接还原成真实 URL(用于后续抓取)。

    路线 A · F4:只要「最终 URL」,不要正文。优先用 HEAD(0 正文字节);
    若服务器不支持 HEAD,退回 GET 但只读响应头就关闭,不下载整页正文。
    这样避免了「还原跳转下载一次正文 + 抓取又下载一次」的双重浪费。
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True, trust_env=False)
    hdrs = {**HEADERS, "Referer": "https://www.baidu.com/"}
    try:
        r = await client.head(url, headers=hdrs)
        return str(r.url)
    except Exception:
        # HEAD 被拒(部分站点只允许 GET):流式 GET,拿到重定向后的 URL 立即关闭
        try:
            async with client.stream("GET", url, headers=hdrs) as resp:
                return str(resp.url)
        except Exception:
            return url
    finally:
        if own:
            await client.aclose()


if __name__ == "__main__":
    import asyncio
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async def _demo():
        for eng in ("bing", "baidu", "360", "sogou"):
            try:
                r = await search("人工智能 最新进展", engine=eng, max_results=5)
                print(f"\n===== {eng} ({r['count']} 条) =====")
                for x in r["results"]:
                    print(f"- {x['title']}\n  {x['url']}\n  {x['snippet'][:80]}")
            except Exception as e:
                print(f"\n===== {eng} 失败 =====\n{type(e).__name__}: {e}")

    asyncio.run(_demo())
