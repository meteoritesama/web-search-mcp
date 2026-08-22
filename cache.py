"""磁盘缓存模块(路线 A · F3)——纯标准库实现,零依赖。

用途:
- scrape 缓存:同一 URL + 同一抓取参数直接命中,避免反复开 Chromium 渲染;
- vision 缓存:同一图片 URL + 同一提示词直接命中,避免重复下载与重复视觉推理。

设计:
- key = kind + sha256(规范化参数 JSON),值为 JSON 文件 {t: 写入时间戳, v: 内容};
- 过期由 config.py 的 cache_ttl_hours 控制;cache_enabled=false 时全部读写降级为 no-op;
- 任何异常都按「未命中 / 写入失败」静默处理——缓存不是关键路径,绝不因缓存报错影响主流程。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

from config import CONFIG

_CACHE_DIR = CONFIG["cache_dir"] or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache"
)
_TTL_SEC = max(0.0, float(CONFIG["cache_ttl_hours"])) * 3600.0
ENABLED = bool(CONFIG["cache_enabled"])


def _mk_key(kind: str, **params) -> str:
    raw = json.dumps({"kind": kind, **params}, ensure_ascii=False, sort_keys=True)
    return f"{kind}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _path(key: str) -> str:
    return os.path.join(_CACHE_DIR, key + ".json")


def get(kind: str, ttl_sec: float | None = None, **params):
    """读缓存:命中且未过期返回值,否则 None。

    ``ttl_sec`` 允许搜索等高频、短生命周期数据使用比全局抓取缓存更短的 TTL。
    """
    if not ENABLED:
        return None
    try:
        with open(_path(_mk_key(kind, **params)), "r", encoding="utf-8") as f:
            obj = json.load(f)
        ttl = _TTL_SEC if ttl_sec is None else max(0.0, float(ttl_sec))
        if ttl <= 0.0 or time.time() - float(obj.get("t", 0)) > ttl:
            return None
        return obj.get("v")
    except Exception:
        return None


def put(kind: str, value, **params) -> None:
    """写缓存(先写临时文件再原子替换,避免半截文件)。"""
    if not ENABLED:
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        p = _path(_mk_key(kind, **params))
        # Unique temporary names prevent concurrent writers from clobbering each other.
        tmp = p + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "v": value}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def clear() -> int:
    """清空缓存目录,返回删除的文件数(供维护用)。"""
    n = 0
    try:
        for name in os.listdir(_CACHE_DIR):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(_CACHE_DIR, name))
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n
