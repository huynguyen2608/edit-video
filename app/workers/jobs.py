"""Worker QThread để UI KHÔNG bị đơ khi quét/tải.

Callback của ScanService gọi từ thread nền; chuyển tiếp qua Qt signal (queued ->
chạy trên UI thread an toàn). Việc edit không dùng QThread ở đây mà do
EditQueueService (một luồng hàng đợi liên tục) đảm nhận — xem app/editor/edit_service.py.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from ..config import AppConfig
from ..store import ExcelStore
from ..downloader.scheduler import ScanService


class ScanWorker(QThread):
    log = Signal(str)
    status = Signal(str, str, str)         # (video_id, field, value)
    progress = Signal(str, float, str)     # (video_id, percent, note)
    downloaded = Signal(str)               # (video_id) tải xong -> UI đẩy sang edit
    done = Signal(dict)

    def __init__(self, cfg: AppConfig, db: ExcelStore, discover_only: bool = False,
                 reconcile: bool = False):
        super().__init__()
        self.cfg = cfg
        self.db = db
        self.discover_only = discover_only
        self.reconcile = reconcile
        self.service = ScanService(
            self.cfg, self.db,
            on_log=self.log.emit,
            on_status=lambda a, b, c: self.status.emit(a, b, c),
            on_progress=lambda a, b, c: self.progress.emit(a, b, c),
            on_downloaded=self.downloaded.emit,
        )

    def cancel_video(self, video_id: str) -> None:
        self.service.cancel_video(video_id)

    def cancel_all(self) -> None:
        self.service.cancel_all_downloads()

    def run(self) -> None:
        stats = self.service.scan_once(discover_only=self.discover_only,
                                       reconcile=self.reconcile)
        self.done.emit(stats)


class ManualDownloadWorker(QThread):
    """Tải THỦ CÔNG 1 video theo URL/ID (chạy nền). Ép tải kể cả video đang kẹt/failed."""
    log = Signal(str)
    status = Signal(str, str, str)  # video_id, field, value
    progress = Signal(str, float, str)  # (video_id, %, note)
    done = Signal(dict)   # {ok, video_id, filepath, error}

    def __init__(self, cfg: AppConfig, db: ExcelStore, url: str):
        super().__init__()
        self.cfg = cfg
        self.db = db
        self.url = url
        self.video_id = ""
        self._cancel = threading.Event()

    def cancel(self, video_id: str | None = None) -> bool:
        if video_id and self.video_id and video_id != self.video_id:
            return False
        self._cancel.set()
        return True

    def run(self) -> None:
        from ..downloader.monitor import extract_video_id
        from ..downloader.downloader import YtDlpDownloader, quality_format
        vid = extract_video_id(self.url)
        self.video_id = vid or ""
        if not vid:
            self.done.emit({"ok": False, "error": "Không tìm thấy video id trong URL.", "url": self.url})
            return
        # đăng ký nếu mới; ép trạng thái downloading (kể cả video đang kẹt/failed)
        self.db.add_discovered(vid, "manual", "Tải thủ công", self.url, self.url, "")
        self.db.set_download_status(vid, "downloading")
        self.status.emit(vid, "download_status", "downloading")
        self.log.emit(f"Đang tải thủ công: {vid}")
        dl = YtDlpDownloader(self.cfg.download.root_dir,
                             quality_format(getattr(self.cfg.download, "quality_height", 1080)),
                             self.cfg.download.cookies_from_browser,
                             self.cfg.download.cookies_file)
        res = dl.download(vid, "manual", self.url,
                          progress_cb=lambda a, b, c: self.progress.emit(a, b, c),
                          cancel_cb=self._cancel.is_set)
        if res.ok:
            self.db.set_download_status(vid, "downloaded", path=res.filepath)
            self.status.emit(vid, "download_status", "downloaded")
            self.db.log_event("download", f"tải thủ công xong {vid} -> {res.filepath}")
        elif res.cancelled:
            self.db.set_download_status(vid, "paused", error=None)
            self.status.emit(vid, "download_status", "paused")
            self.db.log_event("download", f"đã dừng tải thủ công {vid}")
        else:
            self.db.set_download_status(vid, "failed", error=res.error)
            self.status.emit(vid, "download_status", "failed")
            self.db.log_event("download", f"tải thủ công lỗi {vid}: {res.error}", level="ERROR")
        self.done.emit({"ok": res.ok, "cancelled": res.cancelled, "video_id": vid,
                        "filepath": res.filepath, "error": res.error})


class CheckChannelWorker(QThread):
    """Kiểm tra 1 kênh: resolve channel_id + đọc RSS đếm video (chạy nền, có mạng)."""
    done = Signal(dict)   # {ok, channel_id, count, title, error, ref}

    def __init__(self, ref: str):
        super().__init__()
        self.ref = ref

    def run(self) -> None:
        try:
            from ..downloader import monitor   # import trễ (cần feedparser/requests)
            cid = monitor.resolve_channel_id(self.ref)
            vids = monitor.fetch_recent(cid)
            self.done.emit({"ok": True, "channel_id": cid, "count": len(vids),
                            "title": vids[0].title if vids else "", "ref": self.ref})
        except Exception as e:
            self.done.emit({"ok": False, "error": str(e), "ref": self.ref})
