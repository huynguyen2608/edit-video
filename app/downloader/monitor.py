"""Giám sát kênh qua RSS feed của YouTube.

RSS feed (https://www.youtube.com/feeds/videos.xml?channel_id=UC...) là cách phát
hiện video mới rẻ nhất: miễn phí, không tốn quota API, trả ~15 video mới nhất.
Ta CHỈ dùng nó để phát hiện; việc tải thực tế do downloader.py (yt-dlp) lo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import requests

from ..logging_setup import get_logger

log = get_logger("monitor")

_CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
_VIDEO_ID_RE = re.compile(r"(?:youtu\.be/|/shorts/|/embed/|/live/|/v/|[?&]v=)([0-9A-Za-z_-]{11})")


def extract_video_id(url_or_id: str) -> str:
    """Bóc video_id (11 ký tự) từ URL YouTube mọi dạng, hoặc trả thẳng nếu đã là id."""
    s = (url_or_id or "").strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s
    m = _VIDEO_ID_RE.search(s)
    return m.group(1) if m else ""
_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VideoRepurposeStudio/0.1"


@dataclass
class DiscoveredVideo:
    video_id: str
    title: str
    url: str
    published: str  # ISO 8601


def in_date_range(published: str, since: str = "", until: str = "") -> bool:
    """Kiểm tra ngày đăng ISO nằm trong khoảng đóng [since, until]."""
    day = (published or "")[:10]
    if (since or until) and not day:
        return False
    return not ((since and day < since) or (until and day > until))


def resolve_channel_id(url_or_id: str, timeout: int = 15) -> str:
    """Chuyển url kênh / handle (@abc) / channel_id thành channel_id (UC...).

    - .../channel/UC...   -> lấy trực tiếp
    - @handle, /c/, /user -> tải trang và bóc "channelId":"UC..."
    """
    s = (url_or_id or "").strip()
    if s.startswith("UC") and _CHANNEL_ID_RE.fullmatch(s):
        return s
    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", s)
    if m:
        return m.group(1)

    if not s.startswith("http"):
        # coi như handle
        s = "https://www.youtube.com/" + (s if s.startswith("@") else "@" + s)

    resp = requests.get(s, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    m = re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', resp.text) \
        or re.search(r'"externalId":"(UC[0-9A-Za-z_-]{22})"', resp.text) \
        or _CHANNEL_ID_RE.search(resp.text)
    if not m:
        raise ValueError(f"Không resolve được channel_id từ: {url_or_id}")
    return m.group(1) if m.lastindex else m.group(0)


def fetch_recent(channel_id: str, timeout: int = 15) -> list[DiscoveredVideo]:
    """Đọc RSS, trả danh sách video gần đây (mới -> cũ)."""
    feed = feedparser.parse(_RSS.format(cid=channel_id),
                            request_headers={"User-Agent": _UA})
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise RuntimeError(f"Không đọc được RSS cho {channel_id}: {getattr(feed, 'bozo_exception', '')}")
    out: list[DiscoveredVideo] = []
    for e in feed.entries:
        vid = getattr(e, "yt_videoid", "") or _extract_vid(getattr(e, "id", ""))
        if not vid:
            continue
        out.append(DiscoveredVideo(
            video_id=vid,
            title=getattr(e, "title", ""),
            url=getattr(e, "link", f"https://www.youtube.com/watch?v={vid}"),
            published=getattr(e, "published", ""),
        ))
    log.info("Kênh %s: RSS trả %d video", channel_id, len(out))
    return out


def fetch_history(channel_ref: str, channel_id: str = "", limit: int = 500) -> list[DiscoveredVideo]:
    """Duyệt lịch sử upload của kênh bằng yt-dlp, không tải video."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("Cần cài yt-dlp để quét lịch sử video của kênh.") from exc
    ref = (channel_ref or "").strip()
    if not ref:
        ref = f"https://www.youtube.com/channel/{channel_id}"
    if not ref.startswith("http"):
        ref = "https://www.youtube.com/" + (ref if ref.startswith("@") else "@" + ref)
    ref = ref.rstrip("/")
    if not ref.endswith("/videos"):
        ref += "/videos"
    opts = {
        "extract_flat": "in_playlist", "skip_download": True, "quiet": True,
        "no_warnings": True, "playlistend": max(1, int(limit)),
        "ignoreerrors": True, "lazy_playlist": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(ref, download=False) or {}
    out = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id") or extract_video_id(entry.get("url", ""))
        if not vid:
            continue
        published = ""
        upload_date = str(entry.get("upload_date") or "")
        if len(upload_date) == 8 and upload_date.isdigit():
            published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        elif entry.get("timestamp"):
            published = datetime.fromtimestamp(
                float(entry["timestamp"]), tz=timezone.utc).date().isoformat()
        out.append(DiscoveredVideo(
            video_id=vid, title=entry.get("title") or vid,
            url=entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            published=published,
        ))
    log.info("Kênh %s: yt-dlp trả %d video lịch sử", channel_id or channel_ref, len(out))
    return out


def _extract_vid(entry_id: str) -> str:
    # entry id dạng "yt:video:VIDEOID"
    return entry_id.rsplit(":", 1)[-1] if entry_id else ""
