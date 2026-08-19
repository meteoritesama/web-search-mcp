"""LM Studio 本地视觉模型图片描述。

通过 LM Studio 的 OpenAI 兼容端点(/v1/chat/completions)把图片以 base64 形式
发给本地视觉模型,拿到文字描述。完全本地、零外部 API、零密钥。
"""
from __future__ import annotations

import base64
import io
from urllib.parse import urljoin

import httpx

import cache as _cache
from config import CONFIG

DEFAULT_PROMPT = "请用一两句中文简洁描述这张图片的内容、主体和关键信息。"

# 可选依赖:Pillow 用于「送视觉模型前降采样」(路线 A · F5)。
# 未安装时自动跳过降采样,功能不受影响。安装:pip install Pillow
try:
    from PIL import Image

    _HAS_PIL = True
except ImportError:
    Image = None
    _HAS_PIL = False

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

_EXT_MIME = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _mime_from_content_type(ct: str) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    return ct if ct.startswith("image/") else "image/jpeg"


def _mime_from_url(url: str) -> str:
    u = url.lower().split("?")[0]
    for ext, mime in _EXT_MIME.items():
        if u.endswith(ext):
            return mime
    return "image/jpeg"


def _maybe_downsample(data: bytes, mime: str) -> tuple[bytes, str]:
    """路线 A · F5:把最长边缩到 vision_max_side 再重编码为 JPEG。

    视觉 token 数随边长平方增长,降采样能显著减少图片 token →
    推理更快、KV 显存更省。只处理静态 png/jpeg/webp;
    GIF(可能动图)/bmp 原样返回;Pillow 未安装或失败也原样返回。
    """
    max_side = int(CONFIG.get("vision_max_side", 0) or 0)
    if not _HAS_PIL or max_side <= 0:
        return data, mime
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        return data, mime
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if max(w, h) <= max_side:
            return data, mime
        scale = max_side / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, mime


async def describe_image_bytes(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    data: bytes,
    mime: str,
    prompt: str = DEFAULT_PROMPT,
) -> str:
    """把图片字节发给 LM Studio 视觉模型,返回文字描述。"""
    data, mime = _maybe_downsample(data, mime)  # F5:先降采样,省 token / 省 KV 显存
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 400,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = await client.post(
        base_url.rstrip("/") + "/chat/completions",
        json=payload,
        headers=headers,
        timeout=180,
    )
    r.raise_for_status()
    j = r.json()
    try:
        msg = j["choices"][0]["message"]
        # 推理模型(如 Qwen3.5)会把思考过程放在 reasoning_content,答案在 content;
        # content 为空时回退到 reasoning_content,避免拿到空串。
        content = (msg.get("content") or "").strip()
        if not content:
            content = (msg.get("reasoning_content") or "").strip()
        return content
    except (KeyError, IndexError, AttributeError, TypeError):
        raise RuntimeError(f"LM Studio 返回格式异常: {str(j)[:200]}")


async def fetch_and_describe(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    img_url: str,
    page_url: str,
    prompt: str = DEFAULT_PROMPT,
    fetch_timeout: int = 30,
) -> str:
    """下载图片(处理相对路径)并调用本地视觉模型描述。

    路线 A · F3:同一 (图片URL, 提示词, 模型, 降采样边长) 命中磁盘缓存则直接返回,
    避免重复下载与重复视觉推理。
    """
    full = urljoin(page_url, img_url)
    ck = {
        "url": full,
        "prompt": prompt,
        "model": model,
        "max_side": int(CONFIG.get("vision_max_side", 0) or 0),
    }
    hit = _cache.get("vision", **ck)
    if hit is not None:
        return hit

    r = await client.get(full, headers=_UA, timeout=fetch_timeout)
    r.raise_for_status()
    mime = _mime_from_content_type(r.headers.get("content-type")) or _mime_from_url(full)
    desc = await describe_image_bytes(client, base_url, api_key, model, r.content, mime, prompt)
    _cache.put("vision", desc, **ck)
    return desc


async def chat(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: int = 180,
    reasoning_effort: str | None = None,
) -> str:
    """调用 LM Studio(OpenAI 兼容)的文本对话,返回助手回复文本。

    reasoning_effort 可传 none/minimal/low/medium/high/xhigh;传 None 则不发送该字段。
    小模型做提取时建议传 "none" 关闭思考,避免把 token 花在思考过程上。
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = await client.post(
        base_url.rstrip("/") + "/chat/completions",
        json=payload,
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    try:
        msg = j["choices"][0]["message"]
        # 推理模型(如 Qwen3.5)会把思考过程放在 reasoning_content,答案在 content;
        # content 为空时回退到 reasoning_content,避免拿到空串。
        content = (msg.get("content") or "").strip()
        if not content:
            content = (msg.get("reasoning_content") or "").strip()
        return content
    except (KeyError, IndexError, AttributeError, TypeError):
        raise RuntimeError(f"LM Studio 返回格式异常: {str(j)[:200]}")
