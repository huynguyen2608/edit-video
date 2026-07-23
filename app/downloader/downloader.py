"""Tải video bằng yt-dlp.

Lưu ý vận hành:
- yt-dlp tự dùng file .part trong lúc tải và chỉ rename khi hoàn tất, nên module
  edit sẽ không nhặt nhầm file dở (ta còn chặn thêm bằng download_status trong DB).
- YouTube ngày càng chặn bot. Dùng cookies_from_browser để giảm rủi ro.
- yt-dlp đổi liên tục theo YouTube: cập nhật thường xuyên (pip install -U yt-dlp).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..logging_setup import get_logger
from ..paths import ensure_dir, video_dir

log = get_logger("downloader")

ProgressCb = Callable[[str, float, str], None]  # (video_id, percent, note)


def quality_format(height: int) -> str:
    """Lấy chất lượng tốt nhất không vượt mức chọn; tự hạ nếu thiếu đúng độ phân giải."""
    height = int(height or 0)
    if height <= 0:
        return "bestvideo+bestaudio/best"
    return (f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/worstvideo+bestaudio/worst")


class DownloadCancelled(Exception):
    """Dừng tải có chủ đích; không được ghi nhận như lỗi mạng/hệ thống."""


@dataclass
class DownloadResult:
    video_id: str
    ok: bool
    filepath: Optional[str] = None
    error: Optional[str] = None
    cancelled: bool = False


class YtDlpDownloader:
    def __init__(self, root_dir: str, fmt: str, cookies_from_browser: str = "",
                 cookies_file: str = ""):
        self.root_dir = root_dir
        self.fmt = fmt
        self.cookies_from_browser = cookies_from_browser.strip()
        self.cookies_file = cookies_file.strip()

    def _ydl_opts(self, out_dir: Path, video_id: str, progress_cb: Optional[ProgressCb],
                  cancel_cb: Optional[Callable[[], bool]] = None) -> dict:
        def hook(d: dict):
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("Người dùng đã dừng tải")
            if not progress_cb:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = (done / total * 100.0) if total else 0.0
                progress_cb(video_id, pct, "downloading")
            elif d.get("status") == "finished":
                progress_cb(video_id, 100.0, "merging")

        opts: dict = {
            "format": self.fmt,
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "writeinfojson": True,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,          # tránh treo vô hạn khi mạng đứng
            "noplaylist": True,            # URL playlist -> chỉ tải 1 video
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [hook],
            "postprocessor_hooks": [hook],
            "continuedl": True,
        }
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        elif self.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.cookies_from_browser,)
        return opts

    def download(self, video_id: str, channel_name: str, url: str,
                 progress_cb: Optional[ProgressCb] = None,
                 cancel_cb: Optional[Callable[[], bool]] = None) -> DownloadResult:
        try:
            import yt_dlp  # import trễ để app khởi động nhanh và báo lỗi rõ nếu thiếu
        except ImportError:
            return DownloadResult(video_id, False, error="Chưa cài yt-dlp (pip install -U yt-dlp)")

        out_dir = ensure_dir(video_dir(self.root_dir, channel_name, video_id))
        opts = self._ydl_opts(out_dir, video_id, progress_cb, cancel_cb)
        try:
            return self._download_with_opts(yt_dlp, opts, out_dir, video_id, url, cancel_cb)
        except DownloadCancelled as e:
            log.info("Đã dừng tải %s", video_id)
            return DownloadResult(video_id, False, error=str(e), cancelled=True)
        except Exception as e:
            msg = str(e).lower()
            locked = "cookie" in msg and any(x in msg for x in ("could not copy", "permission", "locked"))
            if locked and (self.cookies_from_browser or self.cookies_file):
                log.warning("Cookie bị khóa; thử lại %s không dùng cookie", video_id)
                retry = dict(opts)
                retry.pop("cookiesfrombrowser", None)
                retry.pop("cookiefile", None)
                try:
                    return self._download_with_opts(yt_dlp, retry, out_dir, video_id, url, cancel_cb)
                except DownloadCancelled as cancel_error:
                    return DownloadResult(video_id, False, error=str(cancel_error), cancelled=True)
                except Exception as retry_error:
                    e = retry_error
            log.exception("Lỗi tải %s", video_id)
            return DownloadResult(video_id, False, error=str(e))

    def _download_with_opts(self, yt_dlp, opts: dict, out_dir: Path,
                            video_id: str, url: str,
                            cancel_cb: Optional[Callable[[], bool]] = None) -> DownloadResult:
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("Người dùng đã dừng tải")
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Probe trước (không tải): bỏ qua video ĐANG LIVE/premiere — nếu tải sẽ
                # chạy vô hạn theo luồng trực tiếp và kẹt ở 'downloading'.
                probe = ydl.extract_info(url, download=False)
                if cancel_cb and cancel_cb():
                    raise DownloadCancelled("Người dùng đã dừng tải")
                status = (probe or {}).get("live_status")
                if (probe or {}).get("is_live") or status in ("is_live", "is_upcoming", "post_live"):
                    return DownloadResult(video_id, False,
                                          error=f"Bỏ qua: video đang LIVE/premiere (live_status={status}).")
                info = ydl.extract_info(url, download=True)
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("Người dùng đã dừng tải")
            fp = self._locate_output(out_dir, video_id, info)
            if not fp:
                return DownloadResult(video_id, False, error="Tải xong nhưng không thấy file output")
            log.info("Đã tải %s -> %s", video_id, fp)
            return DownloadResult(video_id, True, filepath=str(fp))

    @staticmethod
    def _locate_output(out_dir: Path, video_id: str, info: dict | None) -> Optional[Path]:
        # Ưu tiên tên theo id + đuôi phổ biến; fallback quét thư mục.
        for ext in ("mp4", "mkv", "webm", "mov"):
            cand = out_dir / f"{video_id}.{ext}"
            if cand.exists():
                return cand
        vids = [p for p in out_dir.iterdir()
                if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} and not p.name.endswith(".part")]
        return vids[0] if vids else None
