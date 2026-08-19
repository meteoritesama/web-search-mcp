"""搜索引擎抓取模块。

直接抓取中国大陆可直接访问的搜索引擎结果页(Baidu / Bing 国内版 / 360 / 搜狗),
模拟浏览器请求,不调用任何第三方搜索 API、不需要任何密钥。

返回统一结构: {"query", "engine", "count", "results": [{title, url, snippet}]}
"""
from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 百度触发安全验证时,页面里会出现这些标记
_BAIDU_VERIFY_MARKERS = ("百度安全验证", "wappass.baidu.com", "verify.baidu.com", "安全验证")


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


async def search_baidu(query: str, max_results: int = 10) -> list[dict]:
    url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": max_results, "ie": "utf-8", "f": "8"}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
    html = r.text
    if any(m in html for m in _BAIDU_VERIFY_MARKERS):
        raise RuntimeError("百度触发了安全验证(反爬),请稍后重试,或改用 engine='bing' / '360'")

    soup = _soup(html)
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
    return out


async def search_bing(query: str, max_results: int = 10) -> list[dict]:
    # cn.bing.com 为中国大陆可直接访问的必应,结构稳定、反爬较弱
    url = "https://cn.bing.com/search"
    params = {"q": query, "count": max_results, "setlang": "zh-hans", "mkt": "zh-CN"}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
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
    return out


async def search_360(query: str, max_results: int = 10) -> list[dict]:
    url = "https://www.so.com/s"
    params = {"q": query}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
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
    return out


async def search_sogou(query: str, max_results: int = 10) -> list[dict]:
    url = "https://www.sogou.com/web"
    params = {"query": query}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as c:
        r = await c.get(url, params=params)
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
    return out


ENGINES = {
    "baidu": search_baidu,
    "bing": search_bing,
    "360": search_360,
    "sogou": search_sogou,
}


async def search(query: str, engine: str = "baidu", max_results: int = 10) -> dict:
    engine = (engine or "baidu").lower()
    fn = ENGINES.get(engine)
    if fn is None:
        raise ValueError(f"不支持的搜索引擎: {engine},可选: {', '.join(ENGINES)}")
    results = await fn(query, max_results=max(1, min(max_results, 30)))
    return {"query": query, "engine": engine, "count": len(results), "results": results}


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
        try:
            return e, await ENGINES[e](query, max_results=cap), None
        except Exception as ex:  # 单引擎失败不拖垮整体
            return e, None, f"{type(ex).__name__}: {ex}"

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

    # 加权排序:命中引擎越多越靠前;同分按首次出现顺序
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
