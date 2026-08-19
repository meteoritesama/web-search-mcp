"""集中配置文件 —— 所有可变配置都在这一个文件里,日后改这里即可。

使用说明:
1. 直接修改本文件的默认值;
2. 环境变量(如 DSH cordis.patch.yml 的 env 段)可覆盖这里的值(留空则用默认);
3. 改完重启 DSH 生效。

所有值都是"完全本地、零外部 API、零密钥"。
"""
import os


def _env(name: str, default: str):
    """环境变量优先:非空则用环境变量,否则用默认值。"""
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


CONFIG = {
    # ============================================================
    # LM Studio 连接(通用)
    # ============================================================
    "llm_base_url": _env("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
    "llm_api_key": _env("LLM_API_KEY", "lm-studio"),

    # ============================================================
    # 视觉模型(图片描述 scrape_url / search_and_extract 用)
    # 需在 LM Studio 加载一个「支持图片输入」的模型
    # ============================================================
    "vision_base_url": _env("VISION_BASE_URL", "http://127.0.0.1:1234/v1"),
    "vision_api_key": _env("VISION_API_KEY", "lm-studio"),
    # 本机已装: qwen/qwen3.8-27b(带 mmproj,推荐) / gemma-4-31b-jang_4m-crack
    "vision_model": _env("VISION_MODEL", "qwen/qwen3.8-27b"),

    # ============================================================
    # LLM 提取(llm_extract 工具:小模型快速提取 → 大模型汇总)
    # ============================================================
    # model_switching=True(默认):单实例顺序切换模型省显存——
    #   需要小模型时切到小模型,处理完再切回大模型汇总,同一时刻只加载一个模型。
    #   此时 small/large 都用 llm_base_url(同一实例),自动做加载/卸载。
    # model_switching=False:双实例并行(实例1=大模型,实例2=小模型),速度快但占双份显存。
    "model_switching": _env("MODEL_SWITCHING", "true").lower() in ("1", "true", "yes", "on"),
    # 小模型:逐块快速提取(本机 unsloth 版 Qwen3.5-4B,注册 id 为 qwen3.5-4b)
    "small_model": _env("SMALL_MODEL", "qwen3.5-4b"),
    "small_model_base_url": _env("SMALL_MODEL_BASE_URL", ""),  # 切换模式下忽略,用 llm_base_url
    # 大模型:最终汇总
    "large_model": _env("LARGE_MODEL", "qwen/qwen3.8-27b"),
    "large_model_base_url": _env("LARGE_MODEL_BASE_URL", ""),  # 留空用 llm_base_url

    # ============================================================
    # 搜索默认参数
    # ============================================================
    "default_engine": "baidu",                          # 单引擎搜索默认引擎
    "search_max_results": 10,                           # 单引擎默认条数
    "multi_engines": ["baidu", "bing", "360", "sogou"], # 综合搜索默认引擎
    "multi_max_results": 5,                             # 综合搜索每引擎条数

    # ============================================================
    # 抓取默认参数(scrape_url / search_and_extract)
    # ============================================================
    "scrape_max_chars": 20000,         # markdown 上限,超出截断(控制上下文)
    "scrape_describe_images": False,   # 是否默认用视觉模型描述图片(较慢)
    "scrape_max_images": 5,            # 最多描述多少张图片

    # ============================================================
    # 三阶段提取默认参数(llm_extract / search_and_extract 的 use_llm_extract)
    # ============================================================
    "extract_max_chars": 20000,        # 阶段1 规则过滤后正文上限
    "extract_chunk_chars": 4000,       # 阶段2 分块大小(喂给小模型每块字符数)
    # search_and_extract 是否默认启用三阶段 LLM 提取(出综合总结)
    "search_extract_use_llm": False,

    # ============================================================
    # 性能优化(路线 A:F1 阶段批处理 / F2 并行抓取 / F3 磁盘缓存 / F5 图片降采样)
    # ============================================================
    # 磁盘缓存:抓取结果 / 图片描述按 (URL+参数) 哈希复用,避免反复渲染与重复推理
    "cache_enabled": _env("CACHE_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
    "cache_dir": _env("CACHE_DIR", ""),            # 留空 = 项目内 .cache/
    "cache_ttl_hours": float(_env("CACHE_TTL_HOURS", "12")),
    # 并行抓取上限(每个并发 = 一个 Chromium 实例,吃内存;32GB 内存下 2~3 安全)
    "scrape_concurrency": int(_env("SCRAPE_CONCURRENCY", "2")),
    # 送视觉模型前的图片最长边上限(像素);0 = 不降采样。
    # 降采样能大幅减少图片 token → 更快、更省 KV 显存(需 Pillow,未装自动跳过)
    "vision_max_side": int(_env("VISION_MAX_SIDE", "800")),

    # ============================================================
    # Crawl4AI
    # ============================================================
    # 数据目录;留空则用本项目目录(自包含,推荐)
    "crawl4ai_base_dir": "",
}

# 便捷回退:小/大模型的 base_url 留空时,统一用 llm_base_url
CONFIG["small_model_base_url"] = CONFIG["small_model_base_url"] or CONFIG["llm_base_url"]
CONFIG["large_model_base_url"] = CONFIG["large_model_base_url"] or CONFIG["llm_base_url"]
