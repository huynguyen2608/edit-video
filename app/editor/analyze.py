"""Phân tích để chọn đoạn HIGHLIGHT cho bản short (đoạn "sôi động" nhất).

Ý tưởng: đo độ to (RMS) theo thời gian bằng ffmpeg astats + ametadata print, rồi chọn
cửa sổ dài `short_seconds` có độ to trung bình cao nhất -> điểm bắt đầu bản short.

parse_astats / best_window là hàm THUẦN nên test được không cần ffmpeg.
"""
from __future__ import annotations

import re
import subprocess

from ..logging_setup import get_logger

log = get_logger("analyze")

_PTS = re.compile(r"pts_time:(-?\d+(?:\.\d+)?)")
_RMS = re.compile(r"Overall\.RMS_level=(-?\d+(?:\.\d+)?|-?inf|nan)")


def parse_astats(text: str) -> list[tuple[float, float]]:
    """Bóc (pts_time, RMS_level) từ stdout của astats+ametadata print."""
    out: list[tuple[float, float]] = []
    t = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            m = _PTS.search(line)
            if m:
                t = float(m.group(1))
        elif "Overall.RMS_level=" in line and t is not None:
            m = _RMS.search(line)
            if m:
                v = m.group(1)
                out.append((t, -120.0 if ("inf" in v or "nan" in v) else float(v)))
    return out


def loudness_series(path: str, ffmpeg: str = "ffmpeg") -> list[tuple[float, float]]:
    p = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path,
         "-af", "astats=metadata=1:reset=1,ametadata=mode=print:file=-", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return parse_astats(p.stdout)


def best_window(series: list[tuple[float, float]], window_s: float,
                total_dur: float) -> float:
    """Trả thời điểm BẮT ĐẦU của cửa sổ dài window_s có loudness TB cao nhất.

    Nếu video ngắn hơn cửa sổ -> 0.0. ebur128 dùng -inf (~-120) cho đoạn im lặng.
    """
    if not series or total_dur <= window_s:
        return 0.0
    # loudness -inf hiển thị rất nhỏ; kẹp sàn để không lệch.
    pts = [(t, max(-120.0, m)) for t, m in series]
    best_start, best_avg = 0.0, -1e9
    for i, (start, _) in enumerate(pts):
        if start > total_dur - window_s:
            break
        end = start + window_s
        vals = [m for t, m in pts if start <= t < end]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        if avg > best_avg:
            best_avg, best_start = avg, start
    return best_start


def pick_highlight_start(path: str, window_s: float, ffmpeg: str = "ffmpeg") -> float:
    """Đo loudness bằng ffmpeg rồi trả điểm bắt đầu đoạn sôi động nhất (giây)."""
    try:
        series = loudness_series(path, ffmpeg)
        total = series[-1][0] if series else 0.0
        start = best_window(series, window_s, total)
        log.info("Highlight cho %s: bắt đầu %.1fs (dài %.0fs)", path, start, window_s)
        return start
    except Exception:
        log.exception("Lỗi dò highlight, dùng đầu video.")
        return 0.0
