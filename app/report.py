"""Xuất báo cáo tổng hợp từ log 'exports' + 'events' (nhóm theo ngày)."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path


def _day(ts) -> str:
    return (str(ts) or "")[:10] or "?"


def build_report(exports: list[dict], events: list[dict]) -> str:
    """Dựng nội dung báo cáo Markdown từ danh sách exports + events."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in exports:
        by_day[_day(e.get("exported_at"))].append(e)

    err = [e for e in events if str(e.get("level")).upper() == "ERROR"]
    lines = ["# Báo cáo xuất bản", "",
             f"- Tổng video đã xuất: **{len(exports)}**",
             f"- Số lỗi ghi nhận: **{len(err)}**", ""]
    for day in sorted(by_day, reverse=True):
        rows = by_day[day]
        lines.append(f"## {day} — {len(rows)} video")
        for e in rows:
            files = sum(1 for k in ("full_path", "short_path", "content_txt", "srt_path")
                        if e.get(k))
            lines.append(f"- `{e.get('video_id')}` ({e.get('channel_name')}) — "
                         f"{files} file @ {e.get('exported_at')}")
        lines.append("")
    if err:
        lines.append("## Lỗi gần đây")
        for e in err[-20:]:
            lines.append(f"- [{e.get('time')}] {e.get('source')}: {e.get('message')}")
    return "\n".join(lines) + "\n"


def write_report(store, out_path: str) -> str:
    """Ghi báo cáo ra file .md. Trả đường dẫn."""
    content = build_report(store.recent_exports(), store.recent_events())
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)
