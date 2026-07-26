"""ScanService: điều phối quét kênh + tải, và lịch chạy tự động 30 phút.

- scan_once(): dùng chung cho cả job định kỳ lẫn nút "Quét" thủ công.
- _lock: chặn hai lần quét chạy đè lên nhau (auto job vs bấm nút).
- callbacks: đẩy log/tiến độ/thay đổi trạng thái ra ngoài (GUI subscribe).
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import AppConfig
from ..store import ExcelStore
from ..logging_setup import get_logger
from . import monitor
from .downloader import YtDlpDownloader, quality_format

log = get_logger("scan")

LogCb = Callable[[str], None]
StatusCb = Callable[[str, str, str], None]  # (video_id, field, value)


class ScanService:
    # Khóa DÙNG CHUNG cho MỌI instance (nút "Quét ngay" tạo một ScanService, job tự
    # động là một ScanService khác). Khóa ở cấp class đảm bảo hai phiên quét không
    # chạy đè lên nhau dù là hai instance khác nhau — đúng như README cam kết.
    _scan_lock = threading.Lock()

    def __init__(self, cfg: AppConfig, db: ExcelStore,
                 on_log: Optional[LogCb] = None,
                 on_status: Optional[StatusCb] = None,
                 on_progress: Optional[Callable[[str, float, str], None]] = None,
                 on_downloaded: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.db = db
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda a, b, c: None)
        self.on_progress = on_progress or (lambda a, b, c: None)
        # Gọi khi MỘT video tải xong -> báo sang module edit (hàng đợi) để sửa.
        self.on_downloaded = on_downloaded or (lambda vid: None)
        self._stats_lock = threading.Lock()   # gộp thống kê khi tải song song
        self._cancel_lock = threading.Lock()
        self._stop_downloads = threading.Event()
        self._cancelled_videos: set[str] = set()
        self._running = threading.Event()
        self._sched: Optional[BackgroundScheduler] = None
        self._downloader = YtDlpDownloader(
            cfg.download.root_dir, quality_format(getattr(cfg.download, "quality_height", 1080)),
            cfg.download.cookies_from_browser,
            cfg.download.cookies_file,
        )

    # ---------------- lịch tự động ----------------
    def start_scheduler(self) -> None:
        if self._sched:
            return
        self._sched = BackgroundScheduler(daemon=True)
        self._sched.add_job(
            self.scan_once, "interval",
            minutes=max(1, self.cfg.download.scan_interval_minutes),
            id="scan", max_instances=1, coalesce=True,
        )
        self._sched.start()
        self._log(f"Đã bật quét tự động mỗi {self.cfg.download.scan_interval_minutes} phút.")

    def stop_scheduler(self) -> None:
        if self._sched:
            self._sched.shutdown(wait=False)
            self._sched = None

    def cancel_video(self, video_id: str) -> None:
        with self._cancel_lock:
            self._cancelled_videos.add(video_id)

    def cancel_all_downloads(self) -> None:
        self._stop_downloads.set()

    def is_running(self) -> bool:
        """True while this service owns the shared scan/download run."""
        return self._running.is_set()

    def _is_cancelled(self, video_id: str) -> bool:
        with self._cancel_lock:
            return self._stop_downloads.is_set() or video_id in self._cancelled_videos

    # ---------------- quét ----------------
    def scan_once(self, discover_only: bool = False, reconcile: bool = False) -> dict:
        """Quét mọi kênh, phát hiện video mới, tải video pending.

        reconcile=True: đối chiếu kho với file thực tế TRƯỚC khi quét — video
        'downloaded' mất file được reset về pending (tải lại) hoặc xóa (nếu local).

        Trả thống kê {discovered, downloaded, failed}. An toàn khi gọi từ nhiều
        nguồn nhờ _lock (lần quét thứ hai sẽ bỏ qua nếu đang chạy).
        """
        if not ScanService._scan_lock.acquire(blocking=False):
            self._log("Đang có phiên quét chạy — bỏ qua lần này.")
            return {"skipped": True}
        self._stop_downloads.clear()
        with self._cancel_lock:
            self._cancelled_videos.clear()
        self._running.set()
        stats = {"discovered": 0, "downloaded": 0, "failed": 0, "cancelled": 0,
                 "discover_only": discover_only, "reconcile": reconcile,
                 "reconciled_reset": 0, "reconciled_removed": 0}
        try:
            if reconcile:
                from .reconcile import reconcile_library
                rec = reconcile_library(self.db)
                stats["reconciled_reset"] = len(rec["reset"])
                stats["reconciled_removed"] = len(rec["removed"])
                if rec["reset"] or rec["removed"]:
                    self._log(f"  🔄 đối chiếu file: {len(rec['reset'])} video mất file → tải lại, "
                              f"{len(rec['removed'])} video local mất file → xóa khỏi bảng.")
                for vid in rec["reset"]:
                    self.on_status(vid, "download_status", "pending")
                for vid in rec["removed"]:
                    self.on_status(vid, "removed", "")
            self._log("Bắt đầu quét kênh…")
            if not discover_only and self.cfg.download.retry_failed:
                n = self.db.reset_failed_downloads()
                if n:
                    self._log(f"  ↻ thử tải lại {n} video lỗi trước đó.")
            for ch in self.cfg.download.channels:
                if self._stop_downloads.is_set():
                    break
                self._discover_channel(ch, stats)
            if not discover_only and not self._stop_downloads.is_set():
                self._download_pending(stats)
            stats["stopped"] = self._stop_downloads.is_set()
            suffix = f", đã dừng {stats['cancelled']}" if stats["cancelled"] else ""
            self._log(f"Quét xong: mới {stats['discovered']}, tải {stats['downloaded']}, lỗi {stats['failed']}{suffix}.")
        finally:
            self._running.clear()
            ScanService._scan_lock.release()
        return stats

    def _discover_channel(self, ch, stats: dict) -> None:
        try:
            cid = ch.channel_id or monitor.resolve_channel_id(ch.url or ch.name)
            self.db.upsert_channel(cid, ch.name, ch.url)
            videos = (monitor.fetch_history(
                ch.url or ch.channel_id or cid, cid,
                limit=getattr(self.cfg.download, "history_limit", 500))
                if getattr(self.cfg.download, "history_scan", False)
                else monitor.fetch_recent(cid))
            for v in videos:
                days = int(getattr(self.cfg.download, "lookback_days", 1))
                days = days if days in {1, 7, 30, 60} else 1
                since = (date.today() - timedelta(days=days)).isoformat()
                until = date.today().isoformat()
                if not monitor.in_date_range(v.published, since, until):
                    if (since or until) and not v.published:
                        self._log(f"  ! bỏ qua {v.video_id}: không xác định được ngày đăng")
                    continue
                if self.db.add_discovered(v.video_id, cid, ch.name, v.title, v.url, v.published):
                    stats["discovered"] += 1
                    self._log(f"  + mới: {v.title} ({v.video_id})")
                    self.on_status(v.video_id, "row", "new")
            self.db.mark_channel_scanned(cid)
        except Exception as e:
            self._log(f"  ! lỗi kênh {ch.name}: {e}")
            log.exception("discover fail %s", ch.name)

    def _download_pending(self, stats: dict) -> None:
        # Đọc lại cấu hình (thư mục tải/format/cookies) để đổi trong GUI có hiệu lực ngay
        # cả với ScanService nền chạy dài hạn.
        self._downloader.root_dir = self.cfg.download.root_dir
        self._downloader.fmt = quality_format(getattr(self.cfg.download, "quality_height", 1080))
        self._downloader.cookies_from_browser = self.cfg.download.cookies_from_browser.strip()
        self._downloader.cookies_file = self.cfg.download.cookies_file.strip()
        rows = self.db.pending_downloads()
        maxp = max(1, int(getattr(self.cfg.download, "max_parallel", 1)))
        if maxp <= 1 or len(rows) <= 1:
            for row in rows:
                if self._stop_downloads.is_set():
                    break
                self._download_one(row, stats)
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=maxp) as ex:
                list(ex.map(lambda r: self._download_one(r, stats), rows))

    def _download_one(self, row, stats: dict) -> None:
        if self._is_cancelled(row.video_id):
            return
        # Giành quyền tải nguyên tử; nếu phiên/thread khác đã giành thì bỏ qua.
        if not self.db.claim_download(row.video_id):
            return
        self.on_status(row.video_id, "download_status", "downloading")
        res = self._downloader.download(
            row.video_id, row.channel_name, row.url, progress_cb=self.on_progress,
            cancel_cb=lambda: self._is_cancelled(row.video_id),
        )
        if res.ok:
            self.db.set_download_status(row.video_id, "downloaded", path=res.filepath)
            self.db.log_event("download", f"tải xong {row.video_id} -> {res.filepath}")
            self.on_status(row.video_id, "download_status", "downloaded")
            with self._stats_lock:
                stats["downloaded"] += 1
            try:
                self.on_downloaded(row.video_id)   # báo sang hàng đợi edit
            except Exception:
                log.exception("on_downloaded lỗi %s", row.video_id)
        elif res.cancelled:
            self.db.set_download_status(row.video_id, "paused", error=None)
            self.db.log_event("download", f"đã dừng tải {row.video_id}")
            self.on_status(row.video_id, "download_status", "paused")
            with self._stats_lock:
                stats["cancelled"] += 1
            self._log(f"  ‖ đã dừng tải {row.video_id}; dữ liệu tải dở được giữ lại.")
        else:
            self.db.set_download_status(row.video_id, "failed", error=res.error)
            self.db.log_event("download", f"tải lỗi {row.video_id}: {res.error}", level="ERROR")
            self.on_status(row.video_id, "download_status", "failed")
            with self._stats_lock:
                stats["failed"] += 1
            self._log(f"  ! tải lỗi {row.video_id}: {res.error}")

    def _log(self, msg: str) -> None:
        log.info(msg)
        self.on_log(msg)
