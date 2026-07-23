"""EditWorker: xử lý hàng đợi edit trên QThread, phát signal cho UI (không polling,
không block UI thread). KHÔNG đổi logic xử lý — dùng lại EditPipeline, chỉ thêm quan
sát tiến trình qua callback stage/render và ProgressTracker.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

from PySide6.QtCore import QThread, Signal

from ..config import AppConfig
from ..store import ExcelStore
from ..queue_manager import QueueManager
from ..editor.pipeline import EditPipeline
from ..editor.cancel import EditCancelled
from ..editor.cancel import run_cancellable
from ..editor.stages import ProgressTracker, Stage, Status, active_stages, fmt_eta


def _probe_meta(path: str, cancel_cb=None) -> dict:
    """ffprobe nhanh: duration, resolution (WxH), fps. Trả rỗng nếu lỗi."""
    try:
        out = run_cancellable(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", path],
            cancel_cb=cancel_cb, capture_output=True, text=True).stdout
        d = json.loads(out)
        st = (d.get("streams") or [{}])[0]
        w, h = st.get("width"), st.get("height")
        rate = st.get("r_frame_rate", "0/1")
        try:
            num, den = rate.split("/")
            fps = round(float(num) / float(den), 2) if float(den) else 0
        except Exception:
            fps = 0
        dur = float(d.get("format", {}).get("duration", 0) or 0)
        return {"resolution": f"{w}x{h}" if w and h else "", "fps": fps, "duration": dur}
    except EditCancelled:
        raise
    except Exception:
        return {}


def _status_for_stage(stage: str) -> str:
    if stage == Stage.RENDERING:
        return Status.RENDERING
    if stage in (Stage.READING, Stage.SMART_CROP):
        return Status.PREPARING
    if stage == Stage.COMPLETED:
        return Status.COMPLETED
    return Status.PROCESSING


class EditWorker(QThread):
    # ---- signals (UI cập nhật theo signal) ----
    status_changed = Signal(str, str)        # (video_id, status)
    stage_changed = Signal(str, str)         # (video_id, stage)
    progress_changed = Signal(str, int, str)  # (video_id, percent, eta_str)
    job_completed = Signal(str)              # (video_id)
    job_failed = Signal(str, str)            # (video_id, error)
    job_cancelled = Signal(str)              # (video_id)
    log = Signal(str, str)                   # (video_id, message)
    queue_finished = Signal()

    def __init__(self, cfg: AppConfig, db: ExcelStore, device: str = "auto"):
        super().__init__()
        self.cfg = cfg
        self.db = db
        self.device = device
        self.qm = QueueManager(cfg, db)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._last_emit = 0.0

    # ---------------- điều khiển ----------------
    def request_stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def _wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.15)

    # ---------------- vòng xử lý ----------------
    def run(self) -> None:
        while not self._stop.is_set():
            self._wait_if_paused()
            if self._stop.is_set():
                break
            vid = self.qm.next_pending()
            if not vid:
                break
            self._process(vid)
        self.queue_finished.emit()

    def _active_stages(self) -> list[str]:
        e = self.cfg.editor
        return active_stages(
            smart_crop=(e.fill_missing == "none" and e.crop_mode == "auto"),
            audio_sep=e.audio.separate_speech,
            speech=(e.export.make_content_txt or e.subtitle.enabled
                    or (e.intro_hook.enabled and e.intro_hook.auto)),
            subtitle=e.subtitle.enabled,
            translation=bool(e.subtitle.enabled and e.subtitle.translate_to),
        )

    def _emit_progress(self, vid: str, tr: ProgressTracker, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < 0.2:   # tiết chế, tránh spam UI
            return
        self._last_emit = now
        self.progress_changed.emit(vid, tr.percent(), fmt_eta(tr.eta_seconds(now)))

    def _process(self, vid: str) -> None:
        row = self.db.get_video(vid)
        if not row or not row.download_path:
            return
        tr = ProgressTracker(self._active_stages(), start_time=time.time())
        try:
            meta = _probe_meta(row.download_path, self._stop.is_set)
        except EditCancelled:
            self.db.set_edit_status(vid, "pending", error=None)
            self.db.update_job(vid, job_status=Status.INTERRUPTED, stage="")
            self.status_changed.emit(vid, Status.INTERRUPTED)
            self.job_cancelled.emit(vid)
            return
        self.db.update_job(vid, job_status=Status.PREPARING, stage=Stage.READING, progress=0,
                           duration=meta.get("duration"), resolution=meta.get("resolution"),
                           fps=meta.get("fps"))
        self.status_changed.emit(vid, Status.PREPARING)

        def on_stage(v, stage):
            tr.set_stage(stage)
            status = _status_for_stage(stage)
            self.db.update_job(v, job_status=status, stage=stage, progress=tr.percent())
            self.stage_changed.emit(v, stage)
            self.status_changed.emit(v, status)
            self._emit_progress(v, tr, force=True)

        def on_render(v, frac):
            tr.set_stage_fraction(frac)
            self.db.update_job(v, progress=tr.percent(), persist=False)
            self._emit_progress(v, tr)

        pipe = EditPipeline(self.cfg, self.db, on_log=lambda m: self.log.emit(vid, m),
                            device=self.device, on_stage=on_stage, on_render_progress=on_render,
                            cancel_cb=self._stop.is_set)
        try:
            pipe.process_one_by_id(vid)   # pipeline tự set edit_status done/failed
        except EditCancelled:
            self.db.update_job(vid, job_status=Status.INTERRUPTED, stage="")
            self.status_changed.emit(vid, Status.INTERRUPTED)
            self.job_cancelled.emit(vid)
            return
        done = self.db.get_video(vid)
        if done and done.edit_status == "done":
            self.db.update_job(vid, job_status=Status.COMPLETED, stage=Stage.COMPLETED, progress=100)
            self.progress_changed.emit(vid, 100, "00:00")
            self.status_changed.emit(vid, Status.COMPLETED)
            self.job_completed.emit(vid)
        else:
            drow = next((r for r in self.db.all_video_rows() if r.get("video_id") == vid), {})
            err = drow.get("error") or "lỗi không rõ"
            self.db.update_job(vid, job_status=Status.FAILED)
            self.status_changed.emit(vid, Status.FAILED)
            self.job_failed.emit(vid, err)
