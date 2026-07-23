"""Nguồn video LOCAL: nạp video từ một folder để edit độc lập, KHÔNG cần module
quét/tải YouTube.

Luồng: người dùng bỏ file video vào folder đầu vào -> ingest_folder() đăng ký vào kho
(coi như đã 'downloaded') -> trả danh sách video CẦN edit (mới thêm hoặc chưa edit /
đã fail lần trước) để đẩy vào hàng đợi edit.

video_id sinh ổn định theo đường dẫn tuyệt đối (hash) nên bấm lại nhiều lần không tạo
trùng; video đã edit xong ('done') sẽ được bỏ qua.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..logging_setup import get_logger
from ..paths import safe_name

log = get_logger("local_source")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
# File OUTPUT do app tạo -> KHÔNG import lại làm nguồn.
_OUTPUT_SUFFIXES = ("_full.mp4", "_short.mp4")


def list_videos(folder: str | Path, recursive: bool = True) -> list[Path]:
    """Liệt kê file video trong folder (mặc định ĐỆ QUY, kể cả thư mục con).

    Bỏ qua file output do chính app tạo (…_full.mp4 / …_short.mp4) để tránh nhặt nhầm
    khi thư mục xuất nằm bên trong thư mục nguồn.
    """
    p = Path(folder)
    if not p.is_dir():
        return []
    it = p.rglob("*") if recursive else p.iterdir()
    out: list[Path] = []
    for f in it:
        if not (f.is_file() and f.suffix.lower() in VIDEO_EXTS):
            continue
        if f.name.lower().endswith(_OUTPUT_SUFFIXES):
            continue
        out.append(f)
    return sorted(out)


def video_id_for(path: str | Path) -> str:
    """ID ổn định theo đường dẫn tuyệt đối: local_<tên>_<hash8>."""
    p = Path(path)
    try:
        key = str(p.resolve())
    except Exception:
        key = str(p)
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    return f"local_{safe_name(p.stem, 32)}_{h}"


def ingest_paths(store, paths, channel_name: str | None = None) -> dict:
    """Nạp danh sách ĐƯỜNG DẪN (file và/hoặc folder) — dùng cho drag&drop vào Queue.

    File lẻ -> nạp trực tiếp; folder -> quét đệ quy. Kênh (nhóm output) mặc định lấy
    tên thư mục cha của mỗi file. Trả thống kê + id CẦN edit (bỏ video đã 'done').
    """
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += list_videos(p)
        elif (p.is_file() and p.suffix.lower() in VIDEO_EXTS
              and not p.name.lower().endswith(_OUTPUT_SUFFIXES)):
            files.append(p)
    # bỏ trùng đường dẫn
    seen, uniq = set(), []
    for f in files:
        k = str(f).lower()
        if k not in seen:
            seen.add(k); uniq.append(f)

    added, eligible, skipped_done = 0, [], 0
    for f in uniq:
        vid = video_id_for(f)
        ch = channel_name or safe_name(f.parent.name) or "Local"
        if store.add_local_video(vid, ch, f.name, str(f)):
            added += 1
        row = store.get_video(vid)
        if row and row.edit_status == "failed":
            store.set_edit_status(vid, "pending")
            row = store.get_video(vid)
        if row and row.edit_status == "pending" and row.download_status == "downloaded":
            eligible.append(vid)
        elif row and row.edit_status == "done":
            skipped_done += 1
    log.info("Ingest %d đường dẫn: %d file, mới %d, cần edit %d, bỏ qua %d",
             len(paths), len(uniq), added, len(eligible), skipped_done)
    return {"total": len(uniq), "added": added, "eligible": eligible,
            "skipped_done": skipped_done}


def ingest_folder(store, folder: str | Path, channel_name: str | None = None) -> dict:
    """Nạp mọi video trong folder vào kho; trả thống kê + danh sách id CẦN edit.

    - channel_name: nhóm output theo tên này (mặc định lấy tên folder) ->
      output/<channel>/<video_id>/.
    - 'eligible': id ở trạng thái cần edit (pending). Video 'failed' lần trước được
      reset về pending để edit lại; 'done' bị bỏ qua.
    """
    files = list_videos(folder)
    channel = channel_name or safe_name(Path(folder).name) or "Local"
    added = 0
    eligible: list[str] = []
    skipped_done = 0
    for f in files:
        vid = video_id_for(f)
        if store.add_local_video(vid, channel, f.name, str(f)):
            added += 1
        row = store.get_video(vid)
        if row and row.edit_status == "failed":
            store.set_edit_status(vid, "pending")   # cho phép edit lại
            row = store.get_video(vid)
        if row and row.edit_status == "pending" and row.download_status == "downloaded":
            eligible.append(vid)
        elif row and row.edit_status == "done":
            skipped_done += 1
    log.info("Ingest %s: tổng %d, mới %d, cần edit %d, bỏ qua đã xong %d",
             folder, len(files), added, len(eligible), skipped_done)
    return {"total": len(files), "added": added, "eligible": eligible,
            "skipped_done": skipped_done, "channel": channel}
