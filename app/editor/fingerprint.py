"""Fingerprint kỹ thuật: biến đổi NHẸ và XÁC ĐỊNH theo video_id để mỗi bản xuất
khác nhau đôi chút mà vẫn ưu tiên giữ chất lượng.

deltas_for() thuần & xác định (cùng video_id -> cùng kết quả) nên test được.
Không có cam kết rằng các biến đổi này thay đổi quyền sử dụng hoặc kết quả Content ID.
"""
from __future__ import annotations

import copy
import hashlib


def _unit(byte: int) -> float:
    """0..255 -> -1..1."""
    return (byte / 255.0) * 2.0 - 1.0


def deltas_for(video_id: str, strength: float = 1.0) -> dict:
    """Sinh các biến đổi nhỏ xác định theo video_id.

    speed ±2%, brightness ±0.04, saturation ±0.06, zoom ±2%, FPS ± mức cấu hình,
    có thể lật ngang.
    """
    h = hashlib.md5((video_id or "").encode("utf-8")).digest()
    s = max(0.0, min(2.0, float(strength)))
    return {
        "strength": s,
        "speed_mul": 1.0 + _unit(h[0]) * 0.02 * s,
        "brightness": round(_unit(h[1]) * 0.04 * s, 4),
        "saturation": round(1.0 + _unit(h[2]) * 0.06 * s, 4),
        "zoom_add": int(round(abs(_unit(h[3])) * 2.0 * s)),
        "flip": h[4] % 2 == 0,
        "fps_unit": _unit(h[5]),
    }


def apply(ecfg, video_id: str):
    """Trả BẢN SAO ecfg đã áp fingerprint (không đụng cfg gốc dùng chung)."""
    d = deltas_for(video_id, getattr(ecfg, "fingerprint_strength", 1.0))
    e = copy.deepcopy(ecfg)
    e.speed = max(0.25, min(4.0, float(e.speed) * d["speed_mul"]))
    e.zoom_fill_percent = int(e.zoom_fill_percent) + d["zoom_add"]
    if d["flip"]:
        e.flip_horizontal = not e.flip_horizontal
    fps_percent = max(
        0.0, min(0.5, float(getattr(e, "fingerprint_fps_percent", 0.1))))
    # Thuộc tính runtime trên bản sao, không ghi đè FPS nguồn trong cấu hình.
    e._fingerprint_fps_multiplier = (
        1.0 + d["fps_unit"] * (fps_percent / 100.0) * d.get("strength", 1.0))
    e.color_grading.enabled = True
    e.color_grading.brightness = round(float(e.color_grading.brightness) + d["brightness"], 4)
    e.color_grading.saturation = round(float(e.color_grading.saturation) * d["saturation"], 4)
    return e
