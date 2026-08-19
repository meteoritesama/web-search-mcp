# web-search-mcp

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
| 搜索默认 | `default_engine` / `multi_engines` / `search_max_results` 等 | 引擎与条数 |
| 抓取默认 | `scrape_max_chars` / `scrape_describe_images` 等 | 正文上限、是否描述图片 |
| 提取默认 | `extract_max_chars` / `extract_chunk_chars` | 三阶段提取参数 |
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

- "搜索一下『大模型 RAG 最新进展』" → `search_web(query="大模型 RAG 最新进展", engine="bing")`
- "抓取并解析这个网页,并告诉我里面的图片是什么" → `scrape_url(url="https://...", describe_images=true)`
- "帮我搜『比特币 行情』并总结前 3 篇文章" → `search_and_extract(query="比特币 行情", engine="bing", max_results=3)`

搜索引擎选择:

| engine | 说明 |
|---|---|
| `baidu` | 默认,百度;返回的 URL 是跳转链接,`search_and_extract` 会自动还原 |
| `bing` | 必应国内版,结果 URL 干净、反爬最弱,**最推荐用于"搜索+抓取"** |
| `360` | 360 搜索 |
| `sogou` | 搜狗(反爬较强,偶尔失败) |

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
- `engines.py` —— 搜索引擎抓取模块(百度/必应/360/搜狗)
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

## 已知限制

- 搜索结果偶尔会包含广告(百度 `baidu.php?url=...` 是广告链接,无法还原,抓取时会被跳过/报错,属正常);
- 搜索引擎反爬可能导致偶发失败,换个引擎即可;
- 抓取大页 / 多图时较慢,务必在 DSH 配置里调大 `toolCallTimeoutMs`;
- 图片描述质量取决于你本地视觉模型本身。
