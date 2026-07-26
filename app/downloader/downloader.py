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


# Từ khóa nhận diện lỗi đọc/giải mã cookie trình duyệt (Chrome app-bound encryption,
# DPAPI, keyring, file cookie bị khóa…). yt-dlp #10927. -> thử lại KHÔNG dùng cookie.
_COOKIE_ERROR_HINTS = ("cookie", "dpapi", "decrypt", "keyring", "could not copy",
                       "failed to load cookies")


def _looks_like_cookie_error(exc: BaseException) -> bool:
    """True nếu lỗi (hoặc nguyên nhân lồng nhau) liên quan đọc/giải mã cookie.

    Duyệt cả chuỗi __cause__/__context__ vì message ngoài cùng của yt-dlp có thể
    chỉ là 'Failed to decrypt with DPAPI' (không chứa chữ 'cookie').
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    blob = " | ".join(parts).lower()
    return any(hint in blob for hint in _COOKIE_ERROR_HINTS)


# YouTube yêu cầu đăng nhập (khi tải không cookie bị chặn như bot).
_AUTH_HINTS = ("sign in to confirm", "not a bot", "confirm you", "authentication",
               "cookies", "login required", "private video", "members-only")

# Hướng dẫn khi cookie trình duyệt không dùng được MÀ YouTube lại đòi đăng nhập.
COOKIE_HELP = (
    "Không tải được: cookie trình duyệt không dùng được (Chrome đang MỞ nên khóa file "
    "cookie, hoặc Chrome mới mã hóa cookie kiểu app-bound), trong khi YouTube đòi đăng "
    "nhập để xác minh không phải bot.\n\n"
    "Cách khắc phục (chọn 1):\n"
    "1) Đóng HẲN Chrome (thoát cả biểu tượng ở khay) rồi bấm Tải lại.\n"
    "2) Xuất cookie ra file: cài tiện ích 'Get cookies.txt LOCALLY', vào youtube.com "
    "(đã đăng nhập) rồi xuất; sau đó ở ô Cookie chọn 'file…' và trỏ tới file .txt vừa xuất.\n"
    "3) Nếu là video công khai: đổi ô Cookie sang '(không dùng)'."
)


def _needs_authentication(exc: BaseException) -> bool:
    return any(hint in str(exc).lower() for hint in _AUTH_HINTS)


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
            # Đặt tên file theo TIÊU ĐỀ video (dễ đọc) thay vì id; windowsfilenames để
            # loại ký tự cấm trên Windows, trim_file_name chặn tràn giới hạn 260 ký tự.
            "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
            "windowsfilenames": True,
            "trim_file_name": 120,
            "merge_output_format": "mp4",
            # Chỉ tải đúng 1 file video; KHÔNG kèm thumbnail/.info.json (không dùng tới).
            "writethumbnail": False,
            "writeinfojson": False,
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
            has_cookies = bool(self.cookies_from_browser or self.cookies_file)
            if has_cookies and _looks_like_cookie_error(e):
                log.warning("Không đọc được cookie trình duyệt (%s); thử lại %s KHÔNG dùng cookie.",
                            self.cookies_from_browser or self.cookies_file, video_id)
                retry = dict(opts)
                retry.pop("cookiesfrombrowser", None)
                retry.pop("cookiefile", None)
                try:
                    return self._download_with_opts(yt_dlp, retry, out_dir, video_id, url, cancel_cb)
                except DownloadCancelled as cancel_error:
                    return DownloadResult(video_id, False, error=str(cancel_error), cancelled=True)
                except Exception as retry_error:
                    # Cookie hỏng + tải-không-cookie bị YouTube chặn (đòi đăng nhập)
                    # -> trả hướng dẫn rõ ràng thay vì traceback yt-dlp khó hiểu.
                    if _needs_authentication(retry_error):
                        log.warning("Tải %s cần cookie nhưng cookie không dùng được.", video_id)
                        return DownloadResult(video_id, False, error=COOKIE_HELP)
                    e = retry_error
            elif has_cookies and _needs_authentication(e):
                # Cookie đọc được nhưng vẫn bị đòi đăng nhập (cookie hết hạn/không đủ quyền).
                return DownloadResult(video_id, False, error=COOKIE_HELP)
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
        # 1) Đường dẫn thật yt-dlp báo về (chính xác nhất khi tên file theo title).
        if info:
            for r in (info.get("requested_downloads") or []):
                fp = r.get("filepath") or r.get("_filename")
                if fp and Path(fp).exists():
                    return Path(fp)
            fp = info.get("_filename")
            if fp:
                merged = Path(fp).with_suffix(".mp4")
                if merged.exists():
                    return merged
                if Path(fp).exists():
                    return Path(fp)
        # 2) Tên theo id (các bản tải cũ) + đuôi phổ biến.
        for ext in ("mp4", "mkv", "webm", "mov"):
            cand = out_dir / f"{video_id}.{ext}"
            if cand.exists():
                return cand
        # 3) Quét thư mục (mỗi video 1 thư mục riêng nên chỉ còn đúng 1 video).
        vids = [p for p in out_dir.iterdir()
                if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} and not p.name.endswith(".part")]
        return vids[0] if vids else None
