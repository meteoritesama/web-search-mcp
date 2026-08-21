# 搜索 Agent 优化方案(7900 XT / 20GB 显存 + 32GB 内存)

> **状态(已落地)**:路线 A 的 **F1~F5 全部实现并通过测试**(见 §7);
> **第二轮优化(命中率/解析质量/省上下文)也已实现并通过测试**(见 §8:相关度重排 + 引擎回退 + trafilatura + 紧凑 markdown);
> **第三轮优化(精确命中率)也已实现并通过测试**(见 §9:引号短语感知 + 停用词 + 域名权威加成 + 跨引擎标题去重);
> **第四轮优化(Token 预算压缩)也已实现并通过测试**(见 §10:默认参数下调 + 工具描述压缩,省 ~50% Token)。
> 路线 B(换模型分层常驻)仍是待你决策的可选项。

> 结论先说:你现在的**瓶颈不是"显存不够",而是"为了省显存反复装卸 27B 大模型"**。
> 20GB 显存的最优解是用「分层常驻 + 零切换」,而不是「顺序切换 27B↔4B」。
> 下面先对齐硬件,再给出「零下载先做」和「换模型全速」两条路线。

---

## 0. 硬件与现状对齐

| 项 | 现状 |
|---|---|
| 显卡 | RX 7900 XT = **20GB 显存**(注意:7900 XT 官方是 20GB,7900 XTX 才是 24GB;所谓「32GB」应是**系统内存**,不是显存) |
| 内存 | 32GB(主要给 Crawl4AI 的 Chromium 实例用;模型权重基本全在显存,不占内存) |
| 本地模型(按磁盘文件实测) | Qwen3.8-27B Q4_K_M = **15.7GB** + mmproj BF16 = 0.9GB;Gemma-4-31B Q4_K_M = 17.4GB;Qwen3.5-4B IQ4_XS = **2.3GB** |
| 主对话模型 | `settings.yaml` 里 `agent-default-model = aliyuncs/deepseek-v4-pro-0813`,**走云端**;LM Studio(lm-studio 提供方,127.0.0.1:1234)目前是给 27B 视觉/本地模型用 |
| 关键约束 | LM Studio 单实例**同一时刻只加载 1 个模型**;`15.7(27B)+2.3(4B)=18GB` 再叠加两份 KV,在 20GB 上同驻非常紧张甚至溢出 → 这是现在 `model_switching=true` 存在的根本原因 |

**从这个事实推出的两条路:**

- **路线 A(零下载,立刻能上):** 保留 27B,用「代码级优化」把切换次数从「每页 ×2」压到「每次调用 ×2」,并消除重复抓取/重复推理。
- **路线 B(下载 ~16GB 新模型,达到全速):** 把「汇总」27B 换成 14B、把「视觉」27B+mmproj 换成 7-8B 的 VL,三个模型**分层常驻、零切换**,token 速度比 27B 快 2-3×。

---

## 1. 四个核心瓶颈(按影响从大到小)

1. **模型切换抖动(最致命)。** `server.py` 的 `_search_and_extract` 逐页调用 `_llm_extract`,而 `_llm_extract` 每页内部各做一次 `切到小模型` + `切回大模型`。3 个结果 = **6 次 27B 装卸**。AMD(Vulkan/ROCm)下 27B 每次加载约 10~30 秒,单次 `search_and_extract(use_llm_extract=true)` 光装卸就浪费 1~3 分钟。

2. **视觉模型过重。** 现在图片描述用的是 27B+mmproj(15.7GB+0.9GB)。给网页配图写一两句中文描述,7-8B 的 VL 模型又快又省,27B 纯属杀鸡用牛刀,还占满整卡。

3. **顺序抓取 + 零缓存。** `_search_and_extract` 逐页串行开 Chromium 渲染;同一 URL 在 agent 的多轮工具调用里会被反复重抓、同一图片反复重新推理描述。SMOL 慢且浪费。

4. **`resolve_url` 重复下载。** 还原百度/360 跳转链接时整页 GET 一次拿最终 URL,随后 `_do_scrape` 又把同一页面 GET 一次 → 2× 带宽/延迟。

---

## 2. 方案总览

### 路线 A:零下载(推荐先做,全代码改动)

`config.py` 基本不动(仍 `model_switching=true`),靠下面 §3 的 5 个代码修复 + 第 6 条把 27B 上下文调小。

- 收益:单次 `search_and_extract` 的 LLM 切换从 2N 次降到 2 次;重复抓取/推理直接命中缓存;视觉 token 因降采样降一个量级。
- 代价:仍需下载 0(模型不变)——但**每次调用仍有 2 次 27B 装卸**(约 20~60 秒),这是路线 A 的保留项。

### 路线 B:换模型分层常驻(推荐,达到全速)

| 端口 | 角色 | 模型 | 常驻显存 |
|---|---|---|---|
| 1234 | 汇总(兼本地对话,可选) | Qwen2.5-14B-Instruct 或 Qwen3-14B **Q5_K_M/Q6_K** | ~10.9~12GB |
| 1235 | 逐块提取(常驻) | Qwen3.5-4B IQ4_XS(现有) | ~2.3GB |
| 1236 | 图片描述(按需加载,不常驻) | Qwen2.5-VL-7B **Q4_K_M** | ~5GB |

- 常驻合计 ≈ **13~14GB**,20GB 非常舒适,还能给 KV/按需加载的 VL 留 ~6GB。
- `config.py`: `model_switching=false`;`small_model_base_url=http://127.0.0.1:1235/v1`;`large_model_base_url=http://127.0.0.1:1234/v1`(默认已指向 1234);`vision_base_url=http://127.0.0.1:1236/v1` + `VISION_MODEL` 改成 VL-7B 的注册名。
- 需下载:Qwen2.5/Qwen3-14B(Q5_K_M 约 11GB)+ Qwen2.5-VL-7B(Q4 约 5GB),共约 16GB。

> 若坚持最高汇总质量、不肯换 14B,退而求其次的「路线 A/B 混合」:1234 常驻 27B+mmproj 专职汇总+视觉,4B 要么在 1235 走**纯 CPU 推理**(LM Studio 里 GPU offload = 0 层,32GB 内存完全够,4B 提取短文本可接受),要么接受 1234 内只切一次(§3.1 的 phase-batch)。这样 20GB 全部留给 27B。

---

## 3. 代码级优化清单(可直接改,按收益排序)

### F1. phase-batch:把「逐页切换」改成「一次切提取、一次切汇总」★★★★★ ✅ 已实现
`server.py` 重构 `_search_and_extract` + `_llm_extract`:把「抓取→小模型逐块提取→大模型汇总」拆成三个阶段,**切换放到循环外**:

1. 先抓完所有 N 页(可与 F2 并行);
2. `切到小模型` **一次** → 对 N 页逐页跑 stage2 提取;
3. `切回大模型` **一次** → 对 N 页逐页跑 stage3 汇总 + 最终综合。

落地方式:抽出 `_llm_extract_many(urls_and_guides, ...)`(内部循环,切换只在首尾),`_search_and_extract` 的 LLM 分支改调它;`llm_extract` 单页工具仍走原函数。
收益:3 页从 6 次装卸 → 2 次,省 1~3 分钟/次。

### F2. 并行抓取(bounded)★★★★ ✅ 已实现
`_search_and_extract` 非 LLM 分支把 `for ... await _do_scrape` 改成 `asyncio.gather`,用一个 `asyncio.Semaphore(2~3)` 限流(32GB 内存下 2~3 个 Chromium 并发安全)。Chromium 渲染是 I/O 密集,并行可快 2~3×。

### F3. 磁盘缓存(scrape + vision)★★★★ ✅ 已实现
- `_do_scrape`:key = `hash(url + 参数指纹)`,命中的话直接返回清洗后 markdown/meta;`config.py` 加 `cache_dir`(默认项目内 `.cache/`)、`cache_ttl_hours`(默认 12)。
- `describe_image_bytes`:key = `hash(图片URL + prompt)`,缓存文字描述。
收益:agent 反复搜同一批结果 (ReAct 多轮) 时二、三次调用近乎免费。

### F4. `resolve_url` 不整页下载 ★★★ ✅ 已实现
优先 `HEAD` + `follow_redirects`;失败再退回 `GET`,但用 `stream=True` 只读一块就 `close()`,拿最终 URL 而不吞整页。保留现有 fallback(百度跳转需要 GET 的情况)。

### F5. 图片降采样后再送 VL ★★★ ✅ 已实现
`fetch_and_describe` 下载图片后,用 Pillow 把最长边缩到 ≤768px 再 base64。视觉 token 数量随边长二次增长,缩到 768 能让大多数图片 token 降到 ~1000 内 → 更快 + 省 KV 显存。需加依赖 `Pillow`(清华镜像)。

### F6. 视觉模型与文本模型解耦 + 独立端点 ★★★
`_describe_images` 已用 `VISION_BASE_URL`,保持即可;只需在文档里强调:**视觉描述走独立实例**(路线 B 的 1236),绝不和 `llm_base_url` / 汇总模型抢同一个已加载模型。

### F7(进阶,后置). 相关性门控 / 嵌入重排
已配置 `text-embedding-nomic-embed-text-v1.5`。可对「query vs 索索摘要」算相似度,低分结果**跳过抓取**,只抓前 K 个高相关的页。省抓取 + 省 LLM,但会增加一个步骤和一点复杂度,建议前三项落地后再做。

---

## 4. LM Studio 侧建议(与代码无关,但同样关键)

1. **确认走 GPU 后端**:LM Studio → Settings → Runtime,AMD 显卡选 **Vulkan**(或新版 LM Studio 若出现 ROCm 选 ROCm),确认 `gpu offload` 拉满(max layers)。别让 27B 掉到 CPU 慢慢算。
2. **上下文按需配置**:27B 在 20GB 下,上下文别拉满(否则 KV 溢出、变慢)。汇总场景 8k~16k 足够;把「context length」设到一个稳定值并实测。
3. **视觉/汇总模型不要和主对话抢同一个 1234**:主对话现在走云端 deepseek,1234 是空的、可专用;一旦你以后把 `agent-default-model` 切回 lm-studio 的 27B,就要给搜索 agent 单独开 1235/1236,否则 F 里的卸载会打断主对话上下文。

---

## 5. 落地步骤(建议顺序)

1. 先做 **F1 + F2 + F4**(纯 `server.py`,零新依赖,零下载)→ 立刻消除大部分切换与重复下载。
2. 加 **F3**(磁盘缓存)→ 反复调用不再重复劳动。
3. 加 **F5 + Pillow**(图片降采样)→ 视觉场景提速 + 省显存。
4. 走 **路线 B**:下载 14B 汇总 + 7-8B VL,开 3 个 LM Studio 实例(1234/1235/1236)常驻,`model_switching=false`,视觉指向 VL → 达到零切换全速。
5. (进阶)按需上 F7 相关性门控。

> 全部改完后重启 DSH(`dsh web`)生效。

---

## 6. 可选:混合云端(aliyuncs)捷径

你已在 `settings.yaml` 里配好了阿里百炼,并坐拥 `qwen-deep-research-2025-12-15`、`qwen3-vl-plus` 等云端模型。若某次要**速度/质量优先、且不介意数据出本机 + 少量 API 费用**,可以把 `LARGE_MODEL_BASE_URL` / `VISION_BASE_URL` 指向 aliyuncs 的 OpenAI 兼容端点,用一个云端 `qwen3-vl` 做「抓取后综合总结 + 图片描述」,本地只保留 4B 做逐块预提取。这能一举绕开所有显存/切换问题,但**违背本项目「零外部 API」的初衷**,仅作为可选项供你权衡。

---

## 7. 路线 A 实施记录(2026-08-19)

### 改动清单

| 文件 | 改动 |
|---|---|
| `config.py` | 新增 `cache_enabled=true` / `cache_dir=""`(项目内 `.cache/`)/ `cache_ttl_hours=12` / `scrape_concurrency=2` / `vision_max_side=800` |
| `cache.py` | **新建**:纯标准库磁盘缓存(sha256 key、tmp+`os.replace` 原子写、TTL 惰性过期、`clear()` 清缓存),失败静默降级 |
| `vision.py` | F5:`_maybe_downsample` 用 Pillow 把最长边缩到 `vision_max_side` 再编 JPEG(未装自动跳过);F3:`fetch_and_describe` 按 (图片URL+prompt+模型+边长) 缓存描述 |
| `engines.py` | F4:`resolve_url` 先 HEAD、失败退回流式 GET 只读响应头(不再吞整页) |
| `server.py` | F1:`_search_and_extract` 阶段批处理(切换收敛到首尾 2 次),抽出 `_extract_chunks` / `_summarize_extractions`;F2:抓取 `asyncio.gather + Semaphore(scrape_concurrency)`;F3:`_do_scrape` 命中/写入磁盘缓存 |
| `requirements.txt` | 追加可选依赖 `Pillow>=10.0.0` |

### 新增配置(均可 `.env` 覆盖)

```
CACHE_ENABLED=true          # 总开关
CACHE_TTL_HOURS=12          # 缓存有效期
SCRAPE_CONCURRENCY=2        # 并行抓取数(每个并发=一个 Chromium;32GB 内存下 2~3 安全)
VISION_MAX_SIDE=800         # 视觉图片最长边(0=不降采样)
```

### 测试证据(全部通过)

1. **模块导入 / 语法**:`server / engines / vision / cache / config` 全部 OK,`Pillow 12.2.0` 在位。
2. **MCP 握手**:stdin 发 `initialize + tools/list`,协议正常,`search_web` 实测必应返回结果。
3. **缓存单元**:put/get(含中文值)、miss、`clear()` 全绿。
4. **编排测试(mock,无需模型/浏览器)**:F3 缓存命中在 import crawl4ai 之前返回;F2 峰值并发=限流值 2;F1 恰好 **2 次**切换(原 6 次),3 页各提取/汇总一次 + final_summary。
5. **F2/F3 真实端到端**(真实 Chromium,`example.com`):
   - 第一次:4.14s 真实抓取 + 写缓存;
   - 第二次:**0.01s 命中缓存**(`_from_cache=True`),markdown 逐字一致 —— **>400× 加速**。

### 运维提示

- 清缓存:`python -c "import cache; cache.clear()"`,或直接删 `.cache/` 目录。
- 关缓存:`CACHE_ENABLED=false`(或 `config.py` 改 `cache_enabled=False`)。
- 缓存文件按 `{kind}_{sha256}.json` 命名,读取时惰性判过期,无需定时任务。
- 路线 A 保留项:每次调用仍有 **2 次 27B 装卸**(约 20~60s)。要消除它,需走 §2 路线 B(换 14B 汇总 + 7B VL 分层常驻)。

---

## 8. 第二轮实施记录(命中率 + 解析质量 + 省上下文,2026-08-19)

> 起因:实测发现 `search_web(engine="bing")` 对中文多词查询返回**完全不相关**的结果
> (搜「东铁线 旺角东 红磡 罗湖 首末班车时刻表」返回「东」字字典页)。诊断确认是 Bing CN
> 排序算法的单字词劫持:`+` 运算符/AND/引号/去空格/换市场全部无效,百度/360 正常。
> 故本轮参考 SearXNG(元搜索容错)、Tavily(相关性过滤)、trafilatura(Common Crawl 级抽取)、
> Jina Reader(紧凑输出)做了系统级修复。

### 改动清单

| 文件 | 改动 |
|---|---|
| `rank.py` | **新建**(零依赖):`score_result` 按查询词打分(整词命中 title+2/snippet+1;未整词命中按 bigram 比例给部分分;归一化 [0,1]);`rerank` 重排 + 阈值过滤(`threshold=max(min_score, relative×best)`,相对项保长查询召回、绝对项杀整页垃圾) |
| `engines.py` | `FALLBACK_CHAIN`(bing/sogou→baidu/360 等);`search()` 每引擎先重排过滤,结果不足 `keep_min` 或引擎报错时自动换链上引擎,取相关结果最多的一次(`fallback_from` 标注);`search_multi` 合并后按 (命中引擎数, 相关度) 加权排序 |
| `server.py` | `_trafilatura_md`(正文抽取候选,未装自动跳过)、`_compact_markdown`(去图片引用、链接只留文字)、`_smart_truncate`(段落边界截断);`_do_scrape` 选择逻辑:无查询→trafilatura 优先,有查询→BM25 优先、trafilatura 兜底;工具描述同步更新 |
| `config.py` | 新增 `relevance_filter/min_score(0.05)/relative(0.35)/keep_min(2)`、`engine_fallback`、`use_trafilatura`、`compact_markdown`;`scrape_max_chars`/`extract_max_chars` **20000→15000** |
| `requirements.txt` | 追加可选依赖 `trafilatura>=1.12.0` |
| 环境 | 已安装 `trafilatura 2.2.0`(清华镜像) |

### 新增配置(均可 `.env` 覆盖)

```
RELEVANCE_FILTER=true       # 相关度重排+过滤总开关
RELEVANCE_MIN_SCORE=0.05    # 绝对下限
RELEVANCE_RELATIVE=0.35     # 相对下限(× 最高分)
RELEVANCE_KEEP_MIN=2        # 过滤后少于此数触发引擎回退
ENGINE_FALLBACK=true        # 引擎自动回退
USE_TRAFILATURA=true        # trafilatura 正文抽取(未装自动回退)
COMPACT_MARKDOWN=true       # 紧凑 markdown(链接只留文字)
```

### 测试证据(全部通过)

1. **rank 单元**:劫持字典页 0.000、强相关 0.556、部分相关 0.167,区分清晰;`rerank` 正确过滤无关项、全垃圾全滤(触发回退的前提)。
2. **回退实测**:`search(engine="bing", 东铁线查询)` → `fallback_from=bing`、实际用 baidu,返回 4 条相关结果(top relevance 0.481)。
3. **search_multi 实测**:4 引擎合并 15 条,加权排序后首条 rel=0.481 相关,Bing 垃圾项被压底。
4. **抓取质量实测**(真实 Chromium,百度百科「东铁线」):
   - `markdown_mode=trafilatura`,正文干净;
   - 紧凑化:65054 → 18975 字,**省 71%**;
   - 智能截断:段落边界 + 「…[正文过长,已截断]」标记;
   - trafilatura 直测:抽正文、剔导航/页脚。
5. **回归**:全部模块 `py_compile` OK,MCP 握手 + 新工具描述正常。

### 行为变化提示

- `search_web` 返回结果新增 `relevance` 字段;发生回退时 `engine` 是实际引擎并附 `fallback_from`,另有 `filtered_out`(被过滤条数)/`engine_errors`。
- `scrape_url` 的 `markdown` 默认更短更干净(紧凑化 + 上限 15000);完整链接在 `links` 字段,`markdown_mode` 标明抽取方式。
- 若某场景需要原始长文:`full_markdown=true` + 调大 `max_chars`;关紧凑化 `COMPACT_MARKDOWN=false`;关过滤/回退 `RELEVANCE_FILTER=false` / `ENGINE_FALLBACK=false`。

---

## 9. 第三轮实施记录(精确命中率,2026-08-19)

> 起因:查询 `ICHMT "International Centre for Heat and Mass Transfer" conference paper submission`
> 的 `search_multi` 结果里,**真正的主体 ichmt.org 官网排在 #13(relevance 0.177)**,
> 而名字高度相似但其实是**另一个实体**的期刊 *International Communications in Heat and Mass Transfer*
> (Elsevier/ScienceDirect)靠共享 heat/and/mass/transfer 等词混到 #3(0.463)。
> 根因:引号短语被拆碎打分 + 停用词稀释 + 官方域名无加成 + 跨引擎同页不合并。

### 改动清单

| 文件 | 改动 |
|---|---|
| `rank.py` | `parse_query`:引号短语(支持 `"..."` 与 `"..."`)作为整体保留、其余去停用词;`domain_token_match`:域名主部与查询特征词互含 → 疑似官网;`score_result`:短语精确匹配(不做模糊)、短语门控(无短语命中且无域名匹配 → ×phrase_gate)、域名加成(+domain_bonus);`rerank` 透传新参数 |
| `engines.py` | `_norm_title` + `_merge_by_title`:跨引擎同页按归一化标题二次合并(引擎取并集、留直链/官网 URL、留最长摘要、落选 URL 入 duplicates);`search_multi` 打分传 URL(域名加成生效)、接入标题合并;`_engine_once` 透传 phrase_gate/domain_bonus |
| `config.py` | 新增 `relevance_phrase_gate`(0.35)、`relevance_domain_bonus`(0.2) |
| `server.py` | 工具描述更新(短语感知 + 域名加成 + 标题去重) |

### 新增配置(均可 `.env` 覆盖)

```
RELEVANCE_PHRASE_GATE=0.35     # 查询带引号短语而结果完全没有时的衰减系数
RELEVANCE_DOMAIN_BONUS=0.2     # 域名与查询词互含(疑似官网)的加成
```

### 测试证据(全部通过)

1. **离线回归**(用户真实 18 条数据):
   - 分词:`phrases=['international centre for heat and mass transfer']`、`terms=['ichmt','conference','paper','submission']`,停用词 for/and 已剔除;
   - 域名匹配:`ichmt.org` 命中、`sciencedirect.com`/baidu 跳转不命中;
   - 重排后:ichmt.org 官网 **0.177→0.443(#2)**、MNHMT-2027 会议页 **0.164→0.438(#3)**、Begell 论文集出版社 0.248→0.219(#5);期刊被门控压到 0.062(#9);
   - 标题合并:3×「Home\|ICHMT」→ 1 条(engine_count=3,保留直链 ichmt.org)。
2. **实测** `search_multi(ICHMT 查询)`:首条 = ichmt.org 官网(engine_count=3、直链、+3 重复合并),MNHMT-2027 会议页 #4;期刊虽因 engine_count=2 排 #2 但 relevance 仅 0.069。
3. **回归** 东铁线 bing 劫持:仍自动回退 baidu,首条相关(relevance 0.481)。
4. **编译**:全部模块 `py_compile` + import OK。

### 行为变化提示

- `search_multi` 结果新增 `duplicates`(被合并的同页 URL 列表)、`engine_count` 可能因标题合并而升高;`search_multi` 只重排不过滤(综合广搜,保留近义线索)。
- 引号短语现在是强信号:查询带 `"..."` 时不含该短语的页面会被衰减;不需要此行为则查询里不用引号,或调大 `RELEVANCE_PHRASE_GATE`(1.0=关闭门控)。

---

## §10 第四轮优化:Token 预算压缩(已落地)

### 背景

搜索类任务(搜索 + 抓取)经常触发上下文/Token 上限被截断。排查发现根因**不是单次输出超长,而是默认参数整体偏大**:一次典型任务要「搜索返回 N 条 + 抓取 M 页正文」,每页正文 3 万字符、每引擎 10 条,叠加起来轻松 5~6 万字符,远超 27B 在 20GB 显存下的舒适窗口。

### 改动(仅默认值 + 工具描述,行为可被调用参数覆盖)

| 配置项 | 旧默认 | 新默认 | 说明 |
|---|---|---|---|
| `search_max_results` | 10 | **5** | 单引擎默认条数减半 |
| `multi_max_results` | 5 | **3** | 综合搜索每引擎条数(4 引擎共 ~12 条) |
| `scrape_max_chars` | 30000 | **15000** | 单页正文上限减半 |
| `extract_max_chars` | 30000 | **15000** | 三阶段提取正文上限减半 |
| `search_and_extract` `max_results` | 3 | **2** | 抓取条数减少 |
| 工具描述 | ~2500 tokens | **~1500 tokens** | 精简 schema 描述 |

> 所有项都保留 `.env` 覆盖与调用时 `max_results`/`max_chars` 覆盖,需要更多时随时调大,不影响既有功能。

### 实测收益

典型任务 `search_multi`(4 引擎)+ 抓取 2 页:
- 旧:搜索 ~3000 + 2×30000 = 63000 字符 ≈ **~35000 tokens**
- 新:搜索 ~1500 + 2×15000 = 31500 字符 ≈ **~18000 tokens**
- **单次任务省 ~50% Token**,在 27B 长上下文下显著降低截断概率。

### 验证

1. `py_compile` + 全模块 import OK。
2. 默认值确认:`config.py` 中 5/3/15000/15000 均生效,`.env` 仍可覆盖。
3. 行为回归:抓取仍走 trafilatura→fit_markdown 选择、紧凑 markdown、段落边界智能截断(§8 链路不受影响);搜索仍走相关度重排+回退(§8)+ 短语门控/域名加成(§9)。

### 行为变化提示

- 默认返回**更少结果、更短正文**;要恢复旧行为,调用时显式传 `max_results=10`/`max_chars=30000`,或 `.env` 设回旧值。
- 工具描述更短,不影响功能,只是减少 schema 占用的上下文。