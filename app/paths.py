"""Tiện ích đường dẫn. Dùng video_id để đặt tên thư mục thay vì tiêu đề
để tránh ký tự cấm của Windows và lỗi đường dẫn dài >260 ký tự.
"""
from __future__ import annotations

import re
from pathlib import Path

_WIN_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, max_len: int = 60) -> str:
    """Chuẩn hoá tên an toàn cho Windows (dùng cho tên kênh, tiêu đề hiển thị)."""
    cleaned = _WIN_FORBIDDEN.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(" .")  # Windows cấm tên kết thúc bằng space/dot
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip(" .")
    return cleaned or "untitled"


def channel_dir(root: str | Path, channel_name: str) -> Path:
    return Path(root) / safe_name(channel_name)


def video_dir(root: str | Path, channel_name: str, video_id: str) -> Path:
    """<root>/<channel>/<video_id>/  — video_id đã an toàn sẵn (ký tự [A-Za-z0-9_-])."""
    return channel_dir(root, channel_name) / safe_name(video_id, max_len=32)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
