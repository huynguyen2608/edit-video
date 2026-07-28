"""Preset theo nền tảng: 1 cú áp bộ cài đặt phù hợp (khung, độ dài, an toàn chữ)."""
from __future__ import annotations

# name -> (nhãn hiển thị, dict cài đặt)
PLATFORM_PRESETS: dict[str, dict] = {
    "tiktok":  {"label": "TikTok (9:16, 60s)",  "target_aspect": "9:16",
                "short_seconds": 60,  "sub_position": "bottom"},
    "reels":   {"label": "Instagram Reels (9:16, 90s)", "target_aspect": "9:16",
                "short_seconds": 90,  "sub_position": "bottom"},
    "shorts":  {"label": "YouTube Shorts (9:16, 60s)", "target_aspect": "9:16",
                "short_seconds": 60,  "sub_position": "middle"},
    "square":  {"label": "Vuông (1:1, 60s)", "target_aspect": "1:1",
                "short_seconds": 60,  "sub_position": "bottom"},
    "youtube": {"label": "YouTube ngang (16:9, 100s)", "target_aspect": "16:9",
                "short_seconds": 100, "sub_position": "bottom"},
}


def apply_platform_preset(cfg, name: str) -> bool:
    """Áp preset vào cfg.editor. Trả True nếu tên hợp lệ."""
    p = PLATFORM_PRESETS.get(name)
    if not p:
        return False
    e = cfg.editor
    e.target_aspect = p["target_aspect"]
    e.export.short_seconds = p["short_seconds"]
    # Preset chỉ đổi bố cục/vị trí; không ghi đè cỡ chữ người dùng đã chọn.
    e.subtitle.position = p["sub_position"]
    return True
