"""搜索结果相关性评分与重排 —— 借鉴 SearXNG 元搜索与 Tavily AI 搜索的相关性模块。

零依赖、纯 Python。三轮演进:
1. 修 Bing CN「单字词劫持」:无关结果得分≈0,过滤并触发引擎回退;
2. 短语感知 + 停用词过滤 + 域名权威加成(本轮):
   - 引号短语(支持 "..." 与 “...”):作为**整体**精确匹配,命中大幅加权;
     查询含引号短语而结果完全没有 → 乘以 phrase_gate 衰减(用户明确要这个实体);
   - 停用词(for/and/的/了…)不参与打分,避免稀释;
   - 域名与查询特征词互含(如查询含 ICHMT、域名 ichmt.org)→ 视为官方站点加成。

评分单元(短语与词各算 1 个单元,每单元满分 3):
- 单元出现在 title +2、snippet +1;
- 普通词未整词命中时按 bigram 命中比例给部分分(封顶 1);短语只做精确匹配;
总分归一化到 [0,1],再按「短语门控 + 域名加成」修正,写入 relevance 字段。

过滤阈值(见 rerank):threshold = max(min_score, relative * best_score)
"""
from __future__ import annotations

import re

# ---- 停用词:不参与打分,避免稀释相关度(仅整词相等才剔除,安全) ----
_STOPWORDS = frozenset(
    # 英文
    "a an the of for and or in on at to from by with as is are was were be been being "
    "it its this that these those i you he she they we not no do does did can could "
    "will would should may might into about over under between per via vs etc".split()
    # 中文常见虚词(整词相等才剔除)
    + ["的", "了", "是", "在", "有", "和", "与", "及", "或", "就", "都", "而", "也",
       "及", "并", "或", "被", "把", "对", "于", "等", "之", "其", "该", "此"]
)

_QUOTE_RE = re.compile(r'["“”]([^"“”]+)["“”]')


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """把查询解析为 (引号短语列表, 剩余特征词列表),全部小写。

    - 引号短语保持整体(用户明确要求精确匹配的内容);
    - 剩余部分按空格切分并剔除停用词与纯标点。
    """
    q = (query or "").lower()
    phrases = [p.strip() for p in _QUOTE_RE.findall(q) if p.strip()]
    rest = _QUOTE_RE.sub(" ", q)
    terms = []
    for t in rest.split():
        t = t.strip(".,;:!?()[]{}<>|/\\'`~·、,。;:!?()【】《》")
        if len(t) >= 1 and t not in _STOPWORDS and not all(ch in ".,;:!?-_" for ch in t):
            terms.append(t)
    return phrases, terms


def _host_of(url: str) -> str:
    try:
        host = url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return ""
    return host.split("@")[-1].split(":")[0].lower()


def domain_token_match(url: str, terms: list[str]) -> bool:
    """域名主部(去掉 www、取倒数第二段)与查询特征词互相包含 → 疑似官方站点。

    例:查询含 "ichmt"、URL 为 https://www.ichmt.org/... → 主部 "ichmt" 命中。
    """
    host = _host_of(url or "")
    if not host:
        return False
    labels = [l for l in host.split(".") if l]
    if len(labels) < 2:
        return False
    sld = labels[-2]
    if sld in ("www", "m", "en") and len(labels) >= 3:
        sld = labels[-3]
    if len(sld) < 3:
        return False
    for t in terms:
        tl = t.lower()
        if len(tl) >= 3 and (tl == sld or tl in sld or sld in tl):
            return True
    return False


def score_result(
    query: str,
    title: str,
    snippet: str,
    url: str = "",
    phrase_gate: float = 0.35,
    domain_bonus: float = 0.2,
) -> float:
    """对单条结果打分,返回 [0,1]。"""
    phrases, terms = parse_query(query)
    units = phrases + terms
    if not units:
        return 0.0
    t = (title or "").lower()
    s = (snippet or "").lower()
    if not t and not s:
        return 0.0

    total = 0.0
    phrase_hit = False
    for unit in units:
        w = 0.0
        if unit in t:
            w += 2.0
        if unit in s:
            w += 1.0
        if w == 0.0 and unit not in phrases and len(unit) >= 2:
            # 普通词的部分匹配:bigram 命中比例(短语不做模糊匹配,精确才算数)
            grams = [unit[i:i + 2] for i in range(len(unit) - 1)]
            hits = sum((2.0 if g in t else 0.0) + (1.0 if g in s else 0.0) for g in grams)
            w = min(1.0, hits / (2.0 * len(grams)))
        if w > 0.0 and unit in phrases:
            phrase_hit = True
        total += w
    score = total / (3.0 * len(units))

    # 短语门控:查询带引号短语而结果完全没有 → 大概率不是用户要的实体,衰减
    dom_match = domain_token_match(url, terms + phrases)
    if phrases and not phrase_hit and not dom_match:
        score *= phrase_gate
    # 官方域名加成:域名与查询特征词互含
    if dom_match:
        score = min(1.0, score + domain_bonus)
    return score


def rerank(
    query: str,
    results: list[dict],
    min_score: float = 0.05,
    relative: float = 0.35,
    phrase_gate: float = 0.35,
    domain_bonus: float = 0.2,
) -> tuple[list[dict], int]:
    """按相关度降序重排并过滤低分结果。

    返回 (保留的结果列表, 被过滤条数)。每条结果附加 relevance 字段(保留 3 位)。
    空输入原样返回。
    """
    if not results:
        return [], 0
    scored = []
    for i, r in enumerate(results):
        sc = score_result(
            query, r.get("title", ""), r.get("snippet", ""), r.get("url", ""),
            phrase_gate=phrase_gate, domain_bonus=domain_bonus,
        )
        r["relevance"] = round(sc, 3)
        scored.append((sc, i, r))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0][0]
    threshold = max(min_score, relative * best)
    kept = [r for sc, _, r in scored if sc >= threshold]
    return kept, len(results) - len(kept)
