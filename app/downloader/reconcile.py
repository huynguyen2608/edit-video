"""Đối chiếu kho video với file thực tế trên đĩa và tự chữa.

Dùng khi người dùng xóa/di chuyển file trong folder tải: DB vẫn ghi 'downloaded'
nhưng file không còn -> biên tập sẽ lỗi 'Không thấy file tải'. Hàm dưới đối chiếu
lại và:
  - Còn file  -> giữ nguyên.
  - Mất file, video YouTube (tải lại được) -> reset về pending (tải lại + edit lại).
  - Mất file, video LOCAL (nhập từ máy, không có nguồn để tải lại) -> xóa khỏi kho.
"""
from __future__ import annotations

import os
from typing import Callable


def is_local_video(row: dict) -> bool:
    """Video nhập từ folder máy: không có nguồn YouTube để tải lại."""
    return (str(row.get("video_id", "")).startswith("local_")
            or row.get("channel_id") == "local")


def reconcile_library(store, exists: Callable[[str], bool] = os.path.exists) -> dict:
    """Đối chiếu mọi video 'downloaded' với đĩa. Sửa kho tại chỗ.

    Trả {'reset': [video_id...], 'removed': [video_id...]} — id đã reset để tải lại
    và id local đã bị xóa. `exists` tiêm được để test không cần file thật.
    """
    reset: list[str] = []
    removed: list[str] = []
    for row in store.all_video_rows():
        if row.get("download_status") != "downloaded":
            continue
        path = row.get("download_path") or ""
        if path and exists(path):
            continue                      # còn file -> giữ nguyên
        vid = row.get("video_id")
        if is_local_video(row):
            removed.append(vid)
        else:
            reset.append(vid)
    if removed:
        store.remove_videos(removed)
    for vid in reset:
        store.reset_for_redownload(vid)
    return {"reset": reset, "removed": removed}
