"""Dò điểm focus (vùng action chính) cho chế độ crop tự động.

Thực tế: "tự động chọn đúng vùng action" cho kết quả hoàn hảo là bài toán khó.
Bản này dùng dò khuôn mặt (OpenCV Haar, không cần tải model ngoài) làm mặc định,
fallback về tâm khung nếu không thấy mặt. Muốn tốt hơn: thay bằng YOLO person/
object hoặc saliency (ultralytics đã có trong requirements-ml.txt) — interface
detect_focus() giữ nguyên nên nâng cấp không ảnh hưởng phần còn lại.

Chế độ manual: người dùng chọn vùng trên GUI, truyền focus trực tiếp, không gọi ở đây.
"""
from __future__ import annotations

import json
import subprocess
import math
import statistics
from dataclasses import dataclass

from ..logging_setup import get_logger

log = get_logger("smart_crop")


@dataclass
class Dimensions:
    width: int
    height: int
    duration: float
    fps: float = 0.0


def probe_dimensions(path: str, ffprobe: str = "ffprobe") -> Dimensions:
    """Đọc kích thước + thời lượng bằng ffprobe."""
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    st = data["streams"][0]
    dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    rate = str(st.get("avg_frame_rate") or "0/1")
    try:
        num, den = rate.split("/", 1)
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return Dimensions(int(st["width"]), int(st["height"]), dur, fps)


def has_audio(path: str, ffprobe: str = "ffprobe") -> bool:
    """True nếu file có ít nhất 1 track audio. Không chắc -> True (an toàn luồng cũ)."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return bool(out.stdout.strip())
    except Exception:
        return True


def audio_codec(path: str, ffprobe: str = "ffprobe") -> str:
    """Tên codec audio đầu tiên; rỗng nếu không đọc được."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return out.stdout.strip().lower()
    except Exception:
        return ""


def audio_channels(path: str, ffprobe: str = "ffprobe") -> int:
    """Số kênh của track audio đầu tiên; không đọc được thì coi là stereo."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return max(1, int((out.stdout or "").strip()))
    except Exception:
        return 2


def grab_frame(path: str, out_png: str, at_seconds: float = 1.0,
               ffmpeg: str = "ffmpeg") -> str:
    """Trích 1 khung hình ra PNG để dùng làm preview chọn vùng crop thủ công."""
    from pathlib import Path as _P
    _P(out_png).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-ss", str(at_seconds), "-i", path, "-frames:v", "1", out_png],
        check=True, capture_output=True,
    )
    return out_png


def detect_focus(path: str, samples: int = 12) -> tuple[float, float]:
    """Trả điểm focus chuẩn hoá (fx, fy) trong [0,1]. Mặc định tâm nếu không dò được."""
    try:
        import cv2  # import trễ
    except ImportError:
        log.warning("Chưa cài opencv-python; dùng tâm khung.")
        return 0.5, 0.5

    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face = cv2.CascadeClassifier(cascade_path)
        cap = cv2.VideoCapture(path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        if total <= 0:
            cap.release()
            return 0.5, 0.5
        step = max(1, total // max(1, samples))
        points: list[tuple[float, float]] = []
        previous: tuple[float, float] | None = None
        idx = 0
        while idx < total:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face.detectMultiScale(gray, 1.2, 5, minSize=(40, 40))
            candidates = [((x + fw / 2) / w, (y + fh / 2) / h, fw * fh)
                          for (x, y, fw, fh) in faces]
            if candidates:
                if previous is None:
                    chosen = max(candidates, key=lambda p: p[2])
                else:
                    # Bám chủ thể gần vị trí trước; diện tích lớn giúp tránh nhảy sang mặt nền.
                    chosen = min(candidates, key=lambda p:
                                 math.hypot(p[0] - previous[0], p[1] - previous[1]) -
                                 min(0.25, p[2] / max(1, w * h)))
                previous = (chosen[0], chosen[1])
                points.append(previous)
            idx += step
        cap.release()
        if points:
            fx, fy = smooth_focus_points(points)
            log.info("Focus dò được: (%.3f, %.3f) từ %d mẫu tracking", fx, fy, len(points))
            return _clamp(fx), _clamp(fy)
    except Exception:
        log.exception("Lỗi dò focus, dùng tâm khung.")
    return 0.5, 0.5


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def smooth_focus_points(points: list[tuple[float, float]], alpha: float = 0.35) -> tuple[float, float]:
    """Khử outlier bằng median rồi EMA để focus không nhảy giữa các chủ thể."""
    if not points:
        return 0.5, 0.5
    mx = statistics.median(p[0] for p in points)
    my = statistics.median(p[1] for p in points)
    kept = [p for p in points if math.hypot(p[0] - mx, p[1] - my) <= 0.35] or points
    x, y = kept[0]
    for px, py in kept[1:]:
        x = alpha * px + (1 - alpha) * x
        y = alpha * py + (1 - alpha) * y
    return _clamp(x), _clamp(y)
