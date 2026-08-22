# web-search-mcp

> **访问策略说明**：稳定性升级以礼貌访问和降载为目标：短时缓存重复查询；引擎连续失败、限流或返回验证页面后在本进程内冷却，并改用备用引擎。插件不会实现 TLS/浏览器指纹伪造、Cookie 养号、验证码绕过、代理池或任何访问控制规避能力。

一个**完全本地、零外部 API、零密钥**的一体化 MCP 工具,给 DeepSeek Harness + LM Studio 提供:

1. **网页搜索** —— 直接抓取中国大陆可直接访问的搜索引擎结果页(百度 / 必应国内版 / 360 / 搜狗),不调用任何搜索 API;
2. **整页解析** —— 用 Crawl4AI(本地 Chromium)提取网页结构 + 文本 + 图片;
3. **图片描述** —— 用本地 LM Studio 视觉模型,把图片转成中文文字描述(图片理解发生在**服务端**,因此绕开了 DSH 丢弃二进制图片的限制)。

五个 MCP 工具:

| 工具 | 作用 |
|---|---|
| `search_web` | 单引擎搜索,返回标题 / URL / 摘要 |
| `search_multi` | **多引擎综合搜索**:并发查百度/必应/360/搜狗,按 URL 去重合并 |
| `scrape_url` | 抓取并解析整页(过滤后 markdown + 文本 + 图片 + 图片描述) |
| `search_and_extract` | 搜索 → 自动还原跳转链接 → 抓取解析前 N 条,一步到位 |
| `llm_extract` | **三阶段智能提取**:规则过滤 → 小模型逐块提取 → 大模型汇总 |

> 抓取的 markdown 默认做了三重降噪:①`PruningContentFilter` 过滤版(可用时);②剥离顶部导航条 + 删除页脚/版权/广告噪声行;③`max_chars` 上限(默认 20000 字符,超出截断)。避免广告等无效内容白白占据上下文。
>
> `llm_extract` 用本地 LLM 彻底解决正文抽取:①规则过滤网页 → ②`SMALL_MODEL`(小模型)逐块快速提取要点 → ③`LARGE_MODEL`(大模型)汇总成连贯总结。
>
> ⚠️ **模型切换省显存(默认)**:`config.py` 里 `model_switching=true` 时,单实例顺序切换——需要小模型时自动切到 `qwen3.5-4b`(并关闭思考),处理完再切回大模型 `qwen/qwen3.8-27b` 汇总,**同一时刻只加载一个模型,避免显存不够**。设为 `false` 则用双实例并行(需足够显存)。

---

## 配置文件(改这里即可)

所有可变配置集中在 **`config.py`**,日后维护只改这一个文件:

| 分组 | 关键项 | 说明 |
|---|---|---|
| LM Studio 连接 | `llm_base_url` / `llm_api_key` | 端点与密钥 |
| 视觉模型 | `vision_model` | 图片描述用的多模态模型 |
| LLM 提取 | `small_model` / `large_model` | 小模型快速提取 + 大模型汇总 |
| 搜索默认 | `default_engine` / `multi_engines` / `search_max_results`(5) / `multi_max_results`(3) | 引擎与条数(第四轮下调省 token) |
| 搜索相关度(第二轮) | `relevance_filter` / `relevance_min_score` / `engine_fallback` 等 | 结果按相关度重排过滤 + 引擎自动回退 |
| 搜索相关度(第三轮) | `relevance_phrase_gate` / `relevance_domain_bonus` | 引号短语门控 + 域名权威加成 |
| 抓取默认 | `scrape_max_chars`(15000) / `scrape_describe_images` 等 | 正文上限(第四轮 30000→15000)、是否描述图片 |
| 提取默认 | `extract_max_chars`(15000) / `extract_chunk_chars` | 三阶段提取参数(第四轮 30000→15000) |
| 解析质量(第二轮) | `use_trafilatura` / `compact_markdown` | trafilatura 正文抽取、紧凑 markdown(省上下文) |
| 性能优化(路线 A) | `cache_enabled` / `cache_ttl_hours` / `scrape_concurrency` / `vision_max_side` | 磁盘缓存、并行抓取限流、视觉图片降采样 |
| Crawl4AI | `crawl4ai_base_dir` | 数据目录(留空=项目内) |

> 环境变量(如 DSH `cordis.patch.yml` 的 `env` 段)仍可覆盖 `config.py` 的默认值,但日常改 `config.py` 即可。改完重启 DSH 生效。

---

## 架构

```
                    ┌──────────────────────────────┐
                    │  web-search-mcp (本进程)        │
关键词 ─────────────►│  1. 抓取 百度/必应/360/搜狗 结果页 │──► 搜索结果(标题/URL/摘要)
                    │  2. Crawl4AI 整页解析           │──► markdown / links / images
                    │  3. 下载图片 ─► LM Studio 视觉模型 │──► 图片中文描述(文本)
                    └──────────────────────────────┘
                              ▲ MCP stdio
                    ┌─────────┴──────────┐
                    │ DeepSeek Harness    │  (cordis.yml 里的 @deepseek-ai/dsh-mcp-client)
                    │ LM Studio(主模型)    │
                    └────────────────────┘
```

- 搜索、抓取、图片描述全部在本机完成;唯一的网络访问是"打开网页本身"(任何联网搜索都不可避免),**没有第三方 API、没有密钥、数据不出本机**。
- 图片描述是**服务端视觉**:Crawl4AI 只负责把图片 URL 提取出来,由本工具下载图片、调用 LM Studio 的视觉模型,把图片转成文字再返回给 DSH。因此 DSH 的 MCP 桥接层(会丢弃二进制图片)不是问题。

---

## 安装

### 1. 环境

- Python 3.10+ (Crawl4AI 建议 3.11 / 3.12;3.13 若遇到依赖问题可退回 3.12)
- 已装 Docker 可选(本项目**不需要** Docker;SearXNG 也不是必需的,搜索靠直接抓取)
- LM Studio 已启动并加载模型

### 2. 安装依赖(中国大陆用镜像)

```powershell
cd web-search-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 下载 Crawl4AI 用的 Chromium(仅抓取功能需要;只用搜索可跳过)
playwright install chromium
```

> 依赖说明:`httpx + beautifulsoup4` 必需(搜索 + MCP 传输);`lxml` 可选(未装会自动回退标准库);`crawl4ai` 仅抓取功能需要。**MCP 传输层只用 Python 标准库手写,不依赖 `mcp`/`pydantic`**,因此最坏情况下只装 `httpx + beautifulsoup4` 也能搜索。

### 3. 配置 LM Studio 视觉模型(可选,但要做图片描述就必须)

在 LM Studio 里加载一个**支持图片输入**的视觉模型,如 `Qwen2.5-VL-7B-Instruct`、`MiniCPM-V`、`LLaVA`。

设置环境变量(或写入 `.env`,但本工具不自动读 `.env`,请在启动命令里设置):

| 变量 | 默认 | 说明 |
|---|---|---|
| `VISION_BASE_URL` | `http://localhost:1234/v1` | LM Studio OpenAI 兼容端点 |
| `VISION_MODEL` | 空 | LM Studio 里加载的视觉模型名(不设置则跳过图片描述) |
| `VISION_API_KEY` | `lm-studio` | 本地服务任意非空字符串即可 |

> ⚠️ **单实例 vs 双实例**:LM Studio 同一时刻通常只加载一个模型。如果你的主对话模型不是视觉模型,建议再开一个 LM Studio 实例(换端口,如 `1235`)专门加载视觉模型,然后把 `VISION_BASE_URL` 指向 `http://localhost:1235/v1`。

---

## 接入 DeepSeek Harness

在 `cordis.yml` 的插件列表里加一段(示例见 `cordis.example.yml`):

```yaml
- id: mcp-websearch
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: websearch
    transport: stdio
    command: python
    args: ['C:/Users/LiangYuelin/Desktop/workspace/web-search-mcp/server.py']
    cwd: 'C:/Users/LiangYuelin/Desktop/workspace/web-search-mcp'
    env:
      VISION_BASE_URL: 'http://localhost:1234/v1'
      VISION_MODEL: 'qwen2.5-vl-7b-instruct'
      VISION_API_KEY: 'lm-studio'
    toolCallTimeoutMs: 300000   # 抓取 + 图片描述较慢,务必调大
```

- 如果用了 venv,把 `command` 改成 `.venv/Scripts/python.exe`(绝对路径)。
- 接入后模型会看到三个工具:`mcp__websearch__search_web`、`mcp__websearch__scrape_url`、`mcp__websearch__search_and_extract`。

---

## 使用示例

模型侧会自然调用工具,例如:

- "搜索一下『大模型 RAG 最新进展』" → `search_web(query="大模型 RAG 最新进展", engine="baidu")`
- "抓取并解析这个网页,并告诉我里面的图片是什么" → `scrape_url(url="https://...", describe_images=true)`
- "帮我搜『比特币 行情』并总结前 3 篇文章" → `search_and_extract(query="比特币 行情", engine="baidu", max_results=3)`

搜索引擎选择:

| engine | 说明 |
|---|---|
| `baidu` | **默认,推荐中文查询**;返回 URL 是跳转链接,`search_and_extract` 会自动还原 |
| `bing` | 必应国内版,URL 干净、反爬最弱;但**对中文多词查询容易返回无关结果**(单字词劫持),仅建议英文/简单查询或 `search_multi` 兜底 |
| `360` | 360 搜索,中文复杂查询结果质量好 |
| `sogou` | 搜狗(反爬较强,偶尔失败) |

> 中文复杂查询(含多词组合)建议使用 `search_multi`(多引擎综合,并发百度/360/搜狗,去重合并),或 `engine="baidu"` / `"360"`。Bing 的 `+` 运算符/AND 关键词/引号均无法修复其单字词劫持问题。

---

## 部署状态(本机)

已完成并在本机实测通过:
- 依赖已装:crawl4ai 0.9.2 + playwright + lxml + Chromium(经国内镜像);
- DSH 配置已写入 `~/.dsh/profiles/web/cordis.patch.yml`;
- 四个搜索引擎(百度/必应/360/搜狗)均返回结果;
- 百度跳转链接正确还原;
- MCP stdio 协议完整跑通(initialize / tools/list / tools/call / 错误处理 / 中文 UTF-8);
- `scrape_url`(整页 markdown + links + 图片)与 `search_and_extract`(搜索→还原→抓取→提图)端到端通过。

**唯一剩余的手动步骤(图片描述需要):**
1. 打开 LM Studio → 开启本地服务(端口 1234);
2. 加载视觉模型 `qwen/qwen3.8-27b`(带 mmproj,支持图片输入);
3. 重启 DSH(`dsh web`),即可在模型里看到 `mcp__websearch__*` 三个工具。

---

## 文件说明

- `server.py` —— MCP 服务入口(手写 MCP stdio,零 mcp/pydantic 依赖)
- `engines.py` —— 搜索引擎抓取模块(百度/必应/360/搜狗)+ 相关度重排 + 引擎自动回退
- `rank.py` —— 搜索结果相关度评分/重排/过滤(零依赖,修 Bing 单字词劫持)
- `vision.py` —— LM Studio 视觉模型图片描述
- `cache.py` —— 磁盘缓存模块(抓取结果 / 图片描述复用,纯标准库)
- `config.py` —— 集中配置(所有可变项)
- `requirements.txt` —— 依赖
- `.env.example` —— 视觉模型环境变量示例
- `cordis.example.yml` —— DSH 接入配置示例

## 性能优化(路线 A · 已落地)

针对 20GB 显存 + 32GB 内存的本机环境,做了四项**零新增大模型**的优化:

| 项 | 说明 | 效果 |
|---|---|---|
| **F1 阶段批处理** | `search_and_extract(use_llm_extract=true)` 的「模型切换」从每页 2 次降到每次调用 2 次(先切小模型批量提取,再切大模型批量汇总) | 3 页从 6 次装卸 → 2 次 |
| **F2 并行抓取** | 多页抓取用 `asyncio.gather` + `Semaphore(scrape_concurrency)` 限流并行 | Chromium I/O 密集,提速约 2~3× |
| **F3 磁盘缓存** | 抓取结果、图片描述按 (URL+参数) 哈希落盘,`cache_ttl_hours` 过期 | 实测命中 4.14s → 0.01s(>400×) |
| **F4 跳转还原不下载正文** | `resolve_url` 优先 HEAD,失败退回只读响应头的流式 GET | 省一次整页下载 |
| **F5 视觉图片降采样** | 送视觉模型前用 Pillow 把最长边缩到 `vision_max_side`(默认 800px) | 图片 token 大幅下降,更快更省 KV 显存 |

> F5 需要可选依赖 `Pillow`(已在 requirements.txt);未安装时自动跳过降采样,其余功能不受影响。
> 缓存目录默认在项目内 `.cache/`;设 `CACHE_ENABLED=false` 可整体关闭。

## 第二轮优化(命中率 + 解析质量 + 省上下文 · 已落地)

借鉴 SearXNG(元搜索容错)、Tavily(相关性过滤)、trafilatura(Common Crawl 级正文抽取)、Jina Reader(紧凑输出)的成熟做法:

| 项 | 说明 | 实测效果 |
|---|---|---|
| **相关度评分/重排** | `rank.py` 按查询词给结果打分(整词命中 title+2/snippet+1,bigram 部分命中给部分分),重排并过滤低分项,结果带 `relevance` 字段 | 无关字典页 0.000、强相关 0.556,区分清晰 |
| **引擎自动回退** | `search()` 若引擎报错或过滤后结果太少,按 `FALLBACK_CHAIN` 换引擎重试,取相关结果最多的一次;`fallback_from` 标注原始引擎 | Bing 劫持查询自动回退百度,返回东铁线相关结果 |
| **search_multi 相关度排序** | 合并结果按「命中引擎数 + 相关度」加权排序,压住单引擎无关项 | 首条即高度相关(rel≈0.48) |
| **trafilatura 正文抽取** | 优先用 trafilatura(Common Crawl 级)抽正文,新闻/文章/百科页去模板噪声最好;未装自动回退 Crawl4AI 过滤 | 百科页正文干净,导航/页脚剔除 |
| **紧凑 markdown** | 去图片引用、链接只留文字(URL 在 `links` 字段仍有) | 链接密集页省 **71%** 上下文 |
| **智能截断** | 在段落/行边界截断而非切句子,附截断标记 | 截断后可读性保留 |
| **正文上限下调** | `scrape_max_chars` / `extract_max_chars` 20000 → 15000 | 进一步控上下文 |

> trafilatura 为可选依赖(已加入 requirements.txt);未安装时自动回退,不影响其余功能。
> 相关度阈值可用 `RELEVANCE_MIN_SCORE` / `RELEVANCE_RELATIVE` / `RELEVANCE_KEEP_MIN` 调;关闭回退设 `ENGINE_FALLBACK=false`。

## 第三轮优化(精确命中率 · 已落地)

针对「同名异实体混淆」(如 ICHMT 既是**组织** International Centre for Heat and Mass Transfer,又是**期刊** International Communications in Heat and Mass Transfer)导致官方页被埋的问题:

| 项 | 说明 | 实测效果 |
|---|---|---|
| **引号短语感知** | 查询里 `"..."` 短语作为**整体**精确匹配(不再拆碎),命中大幅加权;结果完全没有引号短语 → 乘以 `phrase_gate` 衰减 | ichmt.org 官网从 #13(0.177)升到 #2(0.443);期刊被门控压到 0.06 |
| **停用词过滤** | for/and/in/the/的/了… 不参与打分,避免稀释 | 长查询区分度提升 |
| **域名权威加成** | 域名主部与查询特征词互含(如查询含 ICHMT、域名 ichmt.org)→ 疑似官方站点加 `domain_bonus` | MNHMT-2027 会议页 0.164→0.438 |
| **标题级去重合并** | 不同引擎的跳转链接常指向同一页面,按归一化标题二次合并(取引擎并集、留直链、留最长摘要) | 4×「Home\|ICHMT」→ 1 条 engine_count=3,直链 ichmt.org 排第一 |

> 新增配置:`RELEVANCE_PHRASE_GATE`(0.35)、`RELEVANCE_DOMAIN_BONUS`(0.2),均可 `.env` 覆盖。
> `search_multi` 只重排不过滤(综合广搜,保留近义/期刊页线索,靠相关度压到尾部);单引擎 `search_web` 仍过滤+回退。

## 第四轮优化(Token 预算压缩 · 已落地)

搜索类任务经常触发 token 上限被截断,根因是默认参数过大:

| 项 | 旧默认 | 新默认 | 省 Token |
|---|---|---|---|
| **scrape_max_chars** | 30000 | **15000** | 单页减半 |
| **extract_max_chars** | 30000 | **15000** | 单页减半 |
| **search_max_results** | 10 | **5** | 结果数减半 |
| **multi_max_results** | 5 | **3** | 每引擎 3 条(4 引擎共 12 条) |
| **search_and_extract max_results** | 3 | **2** | 抓取条数减少 |
| **工具描述压缩** | ~2500 tokens | **~1500 tokens** | 省 ~1000 tokens |

**典型搜索任务 Token 消耗对比**(search_multi + scrape 2 条):
- 旧:搜索 ~3000 + 抓取 2×30000=60000 字符 ≈ **~35000 tokens**
- 新:搜索 ~1500 + 抓取 2×15000=30000 字符 ≈ **~18000 tokens**
- **省 ~50%**

> 若某场景需要更多结果/更长正文,可在调用时显式传 `max_results`/`max_chars` 覆盖默认值。

## 已知限制

- 搜索结果偶尔会包含广告(百度 `baidu.php?url=...` 是广告链接,无法还原,抓取时会被跳过/报错,属正常);
- 搜索引擎反爬可能导致偶发失败,换个引擎即可;
- 抓取大页 / 多图时较慢,务必在 DSH 配置里调大 `toolCallTimeoutMs`;
- 图片描述质量取决于你本地视觉模型本身。
