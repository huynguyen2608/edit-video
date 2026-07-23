"""Trạng thái job, công đoạn (stage) và ProgressTracker cho Queue Manager.

Tách riêng để test được không cần GUI. ProgressTracker tính % tổng và ETA THẬT dựa
trên trọng số công đoạn + tiến trình từng công đoạn (không dùng progress giả).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------- Trạng thái job ----------------
class Status:
    WAITING = "Waiting"
    PREPARING = "Preparing"
    PROCESSING = "Processing"
    RENDERING = "Rendering"
    COMPLETED = "Completed"
    FAILED = "Failed"
    PAUSED = "Paused"
    CANCELLED = "Cancelled"
    INTERRUPTED = "Interrupted"
    REMOVED = "Removed"                  # ẩn khỏi hàng đợi, không xóa video/DB

    ACTIVE = {PREPARING, PROCESSING, RENDERING}   # đang chạy
    DONE = {COMPLETED}                            # không chạy lại
    # có thể đưa vào hàng đợi để (tiếp tục) xử lý:
    RUNNABLE = {WAITING, INTERRUPTED, FAILED, PAUSED}


# ---------------- Công đoạn ----------------
class Stage:
    READING = "Reading Metadata"
    SMART_CROP = "Smart Crop"
    AUDIO = "Audio Processing"
    SPEECH = "Speech Recognition"
    SUBTITLE = "Subtitle"
    TRANSLATION = "Translation"
    TTS = "TTS"
    RENDERING = "Rendering"
    SAVING = "Saving"
    COMPLETED = "Completed"


# Thứ tự + trọng số (ước lượng thời gian tương đối) để tính % tổng.
_STAGE_ORDER = [Stage.READING, Stage.SMART_CROP, Stage.AUDIO, Stage.SPEECH,
                Stage.SUBTITLE, Stage.TRANSLATION, Stage.TTS, Stage.RENDERING, Stage.SAVING]
STAGE_WEIGHTS = {
    Stage.READING: 1, Stage.SMART_CROP: 3, Stage.AUDIO: 18, Stage.SPEECH: 16,
    Stage.SUBTITLE: 2, Stage.TRANSLATION: 5, Stage.TTS: 5, Stage.RENDERING: 48,
    Stage.SAVING: 2,
}


def active_stages(*, smart_crop: bool, audio_sep: bool, speech: bool,
                  subtitle: bool, translation: bool, tts: bool = False) -> list[str]:
    """Danh sách công đoạn THỰC SỰ chạy cho 1 job (để chia % đúng, bỏ công đoạn tắt)."""
    flags = {
        Stage.READING: True,
        Stage.SMART_CROP: smart_crop,
        Stage.AUDIO: audio_sep,
        Stage.SPEECH: speech,
        Stage.SUBTITLE: subtitle,
        Stage.TRANSLATION: translation,
        Stage.TTS: tts,
        Stage.RENDERING: True,
        Stage.SAVING: True,
    }
    return [s for s in _STAGE_ORDER if flags.get(s)]


@dataclass
class ProgressTracker:
    """Theo dõi % tổng + ETA cho 1 job dựa trên công đoạn hiện tại và tiến trình của nó."""
    stages: list[str]
    start_time: float
    current: str = ""
    stage_fraction: float = 0.0        # 0..1 tiến trình của công đoạn hiện tại
    _total_w: float = field(default=0.0, init=False)

    def __post_init__(self):
        self._total_w = float(sum(STAGE_WEIGHTS.get(s, 0) for s in self.stages)) or 1.0
        if not self.current and self.stages:
            self.current = self.stages[0]

    def set_stage(self, stage: str) -> None:
        self.current = stage
        self.stage_fraction = 0.0

    def set_stage_fraction(self, frac: float) -> None:
        self.stage_fraction = max(0.0, min(1.0, float(frac)))

    def overall(self) -> float:
        """% tổng 0..1: (trọng số các stage đã xong) + (stage hiện tại * tiến trình).

        Bền với stage KHÔNG nằm trong danh sách active (vd Smart Crop bị bỏ): dựa vào
        thứ tự TOÀN CỤC để xác định 'đã xong', không đếm nhầm.
        """
        if self.current == Stage.COMPLETED:
            return 1.0
        cur_idx = _STAGE_ORDER.index(self.current) if self.current in _STAGE_ORDER else len(_STAGE_ORDER)
        done = sum(STAGE_WEIGHTS.get(s, 0) for s in self.stages
                   if s in _STAGE_ORDER and _STAGE_ORDER.index(s) < cur_idx)
        cur = (STAGE_WEIGHTS.get(self.current, 0) * self.stage_fraction
               if self.current in self.stages else 0.0)
        return max(0.0, min(1.0, (done + cur) / self._total_w))

    def percent(self) -> int:
        return int(round(self.overall() * 100))

    def eta_seconds(self, now: float) -> Optional[float]:
        """ETA còn lại (giây) theo tốc độ trung bình. None nếu chưa đủ dữ liệu."""
        frac = self.overall()
        elapsed = now - self.start_time
        if frac <= 0.01 or elapsed <= 0:
            return None
        return elapsed * (1.0 - frac) / frac

    def elapsed(self, now: float) -> float:
        return max(0.0, now - self.start_time)


def fmt_eta(seconds: Optional[float]) -> str:
    """Định dạng ETA/elapsed gọn: --:-- nếu None, mm:ss hoặc hh:mm:ss."""
    if seconds is None:
        return "--:--"
    s = int(max(0, seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
