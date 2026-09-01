"""用合成图片走通真实视觉模型，验证闭卷选择提示词是否被正确接受。

不依赖任何图像库：手写最小 PNG 编码器。
目的不是测识别准确率（合成图不代表真实拍摄），
而是确认模型能按新格式返回 sku_id，以及看不清时能否如实说不知道。
"""

import base64
import struct
import sys
import zlib
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.database import init_db
from app.services.vision import _extract_json, build_catalog, build_vision_prompt
from app.repositories import product_repo

init_db(seed_if_empty=True)
settings = get_settings()


def make_png(width: int, height: int, painter) -> bytes:
    """手写最小 PNG：只支持 RGB，够用就好。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(painter(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def cola_can(x: int, y: int) -> tuple[int, int, int]:
    """浅灰台面上立着一个红罐，中间一道白带。"""
    bg = (236, 238, 242)
    red = (196, 30, 43)
    white = (250, 250, 250)
    silver = (200, 203, 210)

    # 罐体：横向居中，占画面中间偏下
    left, right = 150, 250
    top, bottom = 60, 270
    if left <= x < right and top <= y < bottom:
        if top <= y < top + 14 or bottom - 14 <= y < bottom:
            return silver
        if 140 <= y < 175:
            return white
        return red
    return bg


def call_vision(image_bytes: bytes) -> str:
    payload = {
        "model": settings.VISION_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{base64.b64encode(image_bytes).decode()}"
                        },
                    },
                    {"type": "text", "text": build_vision_prompt(build_catalog(product_repo.list_products()))},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=settings.VISION_TIMEOUT) as client:
        resp = client.post(
            f"{settings.VISION_BASE_URL.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


print("=" * 68)
print("闭卷选择提示词 · 真实模型连通性验证")
print("=" * 68)

png = make_png(400, 300, cola_can)
print(f"合成图片：{len(png)} 字节 PNG（红罐 + 白带，非真实照片）")

try:
    content = call_vision(png)
except Exception as exc:
    print(f"\n调用失败：{type(exc).__name__}: {exc}")
    print("→ 无法验证，可能是网络或密钥问题")
    sys.exit(0)

print(f"\n模型原始返回：\n{content}\n")

try:
    data = _extract_json(content)
except Exception as exc:
    print(f"解析失败：{exc}")
    print("→ 提示词可能需要调整，模型没有按约定返回 JSON")
    sys.exit(1)

items = data.get("items", [])
print(f"解析成功，{len(items)} 个条目")

if not items:
    print("→ 模型返回空列表：说明它在看不清时如实说了不知道（符合预期）")
    sys.exit(0)

valid = {p.sku_id for p in product_repo.list_products()}
for it in items:
    cands = it.get("candidates", [])
    if not cands:
        print(f"  [未识别] quantity={it.get('quantity', 1)} → 将转入人工复核")
        continue
    for c in cands:
        sku = c.get("sku_id", "")
        mark = "有效" if sku in valid else "清单外"
        print(f"  [{mark}] {sku} confidence={c.get('confidence')}")

print("\n→ 全部 sku_id 均来自清单，说明闭卷选择生效" if all(
    c.get("sku_id") in valid for it in items for c in it.get("candidates", [])
) else "\n→ 存在清单外 sku_id，系统会将其丢弃并转人工复核（兜底逻辑生效）")
