# 部署文件清单

本次「本地搜索 + Crawl4AI 抓取 + LM Studio 图片描述」MCP 工具部署所涉及的全部文件与目录。

---

## 一、项目本体(自建的 MCP 工具)

目录:`C:\Users\LiangYuelin\Desktop\workspace\web-search-mcp\`

| 文件 | 说明 |
|---|---|
| `config.py` | ★ **集中配置文件**:所有可变配置(模型名/端点/默认参数)都在这里 |
| `server.py` | MCP 服务入口(手写 MCP stdio 协议,零 mcp/pydantic 依赖) |
| `engines.py` | 搜索引擎抓取模块(百度/必应/360/搜狗)+ 相关度重排 + 引擎自动回退 |
| `rank.py` | 搜索结果相关度评分/重排/过滤(第二轮优化,零依赖) |
| `vision.py` | LM Studio 视觉模型图片描述 + LLM 文本对话 |
| `cache.py` | 磁盘缓存模块(路线 A · F3:抓取结果 / 图片描述复用,纯标准库) |
| `requirements.txt` | Python 依赖清单(含可选 Pillow、trafilatura) |
| `README.md` | 使用与部署文档 |
| `OPTIMIZATION.md` | 性能优化方案(路线 A 已落地;第二轮优化已落地;路线 B 换模型方案待选) |
| `.env.example` | 视觉模型环境变量示例 |
| `cordis.example.yml` | DSH 接入配置示例(与实际部署一致) |
| `.crawl4ai/` | crawl4ai 数据目录(**首次抓取时自动生成**,由 `CRAWL4_AI_BASE_DIRECTORY` 指定到项目内) |
| `.cache/` | 路线 A 磁盘缓存目录(**首次命中写入时自动生成**;`cache.clear()` 或手动删除即可清空) |
| `.tmp/` | 沙箱残留空目录(可手动删除) |
| `__pycache__/` | Python 字节码缓存(可删) |

---

## 二、DeepSeek Harness 配置(核心改动在这里)

目录:`C:\Users\LiangYuelin\.dsh\`

| 文件 | 说明 |
|---|---|
| `profiles\web\cordis.patch.yml` | ★ **我写入的插件配置**(`mcp-websearch` 条目) |
| `profiles\web\cordis.yml` | 根配置,DSH 每次启动自动重写为 `[]`(勿手改) |
| `profiles\web\package.json` | profile 清单(声明 `dsh-base` + `dsh-web-app` 两个 bundle) |
| `profiles\web\pnpm-workspace.yaml` | DSH 自动生成的模块解析回退文件 |
| `settings.yaml` | 全局设置(含 LM Studio provider 与模型 id:`qwen/qwen3.8-27b` 等) |
| `.credentials.yaml` | 凭据存储(本次未改动) |
| `cordis.patch.yml` | (可选)home 级 patch 层,本机未创建,优先级高于 profile 级 |

---

## 三、DSH 插件包(只读,由 npx 缓存提供)

目录:`C:\Users\LiangYuelin\AppData\Local\npm-cache\_npx\1e7f6d9597241db0\node_modules\@deepseek-ai\`

| 目录 | 说明 |
|---|---|
| `dsh-mcp-client\` | MCP 客户端桥接插件(把 MCP 工具注册给模型) |
| `dsh\` | DSH CLI(profile 启动、配置组合) |
| `dsh-base\` | 基础 bundle(其 `cordis.patch.yml` 是 patch 格式的参考) |
| `dsh-web-app\` | web 应用 bundle |

> 注意:该路径是 npx 的一次性缓存,可能随 npx 清理而变;DSH 通过 `dsh plugin` 管理时会把插件装到 profile 自身的 `node_modules`。

---

## 四、Python 解释器与依赖

| 路径 | 说明 |
|---|---|
| `C:\DevelopmentSoftware\Python\python.exe` | Python 3.13.7(DSH 配置里 `command` 用的就是这个) |
| `C:\DevelopmentSoftware\Python\Lib\site-packages\` | 已安装:crawl4ai 0.9.2、playwright、lxml、pydantic、httpx、beautifulsoup4、Pillow 12.2、trafilatura 2.2 等 |

---

## 五、Playwright 浏览器(Crawl4AI 抓取用)

目录:`C:\Users\LiangYuelin\AppData\Local\ms-playwright\`

| 子目录 | 说明 |
|---|---|
| `chromium-1234\` | 完整 Chromium |
| `chromium_headless_shell-1234\` | 无头 Shell |
| `ffmpeg-1011\` | 媒体解码 |
| `winldd-1007\` | 依赖扫描 |

---

## 六、LM Studio 视觉模型(图片描述用)

| 路径 | 说明 |
|---|---|
| `C:\Users\LiangYuelin\.lmstudio\models\lmstudio-community\Qwen3.8-27B-GGUF\` | Qwen3.8-27B 主模型 + `mmproj`(支持图片输入,推荐) |
| `C:\Users\LiangYuelin\.lmstudio\models\douyamv\Gemma-4-31B-JANG_4M-CRACK-GGUF\` | Gemma-4-31B 备用 |
| `C:\Users\LiangYuelin\.lmstudio\settings.json` | LM Studio 设置(含 `enableLocalService` 开关) |
| `C:\Users\LiangYuelin\AppData\Local\Programs\LM Studio\` | LM Studio 程序本体 |

---

## 关键路径速记

- **改配置** → `C:\Users\LiangYuelin\.dsh\profiles\web\cordis.patch.yml`
- **改代码** → `C:\Users\LiangYuelin\Desktop\workspace\web-search-mcp\`
- **换视觉模型** → 改 `cordis.patch.yml` 里的 `VISION_MODEL`,并在 LM Studio 加载对应模型
- **重启生效** → 重启 DSH(`dsh web`)
