"""QueueManager: cầu nối giữa kho (ExcelStore) và UI Queue.

Cung cấp danh sách Job để hiển thị, số liệu Dashboard, import file/folder (dedup, bỏ
video đã hoàn thành), danh sách id cần xử lý, và resume khi mở lại app. KHÔNG đụng
logic xử lý video (chỉ đọc/ghi trạng thái).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .editor import local_source
from .editor.stages import Status
from .paths import safe_name
from .store import ExcelStore


@dataclass
class Job:
    video_id: str
    title: str
    source_path: str
    output_dir: str
    status: str
    stage: str
    progress: int
    duration: float
    resolution: str
    fps: str
    error: str


def display_status(row) -> str:
    """Suy ra trạng thái Queue từ 1 dòng video (edit_status là gốc cho done/failed)."""
    ed = getattr(row, "edit_status", None) if not isinstance(row, dict) else row.get("edit_status")
    js = getattr(row, "job_status", None) if not isinstance(row, dict) else row.get("job_status")
    if js == Status.REMOVED:
        return Status.REMOVED
    if ed == "done":
        return Status.COMPLETED
    if js in (Status.PAUSED, Status.CANCELLED, Status.INTERRUPTED,
              Status.PREPARING, Status.PROCESSING, Status.RENDERING):
        return js
    if ed == "failed":
        return Status.FAILED
    if ed == "processing":
        return Status.PROCESSING
    return Status.WAITING


class QueueManager:
    def __init__(self, cfg, db: ExcelStore):
        self.cfg = cfg
        self.db = db

    def _output_dir(self, channel_name: str, video_id: str) -> str:
        return str(Path(self.cfg.editor.output_dir) / safe_name(channel_name or "Local") / video_id)

    def jobs(self) -> list[Job]:
        """Toàn bộ video đã có file (nguồn để edit) -> danh sách Job cho UI."""
        out: list[Job] = []
        for r in self.db.all_video_rows():
            if r.get("download_status") != "downloaded":
                continue
            if display_status(r) == Status.REMOVED:
                continue
            out.append(Job(
                video_id=r.get("video_id", ""),
                title=r.get("title") or r.get("video_id", ""),
                source_path=r.get("download_path") or "",
                output_dir=self._output_dir(r.get("channel_name", ""), r.get("video_id", "")),
                status=display_status(r),
                stage=r.get("stage") or "",
                progress=int(r.get("progress") or 0),
                duration=float(r.get("duration") or 0.0),
                resolution=r.get("resolution") or "",
                fps=str(r.get("fps") or ""),
                error=r.get("error") or "",
            ))
        return out

    def dashboard(self) -> dict:
        js = self.jobs()
        counts = {"processing": 0, "waiting": 0, "completed": 0, "failed": 0, "total": len(js)}
        prog_sum = 0.0
        for j in js:
            if j.status in Status.ACTIVE:
                counts["processing"] += 1
            elif j.status == Status.COMPLETED:
                counts["completed"] += 1
            elif j.status == Status.FAILED:
                counts["failed"] += 1
            else:
                counts["waiting"] += 1
            prog_sum += 100.0 if j.status == Status.COMPLETED else j.progress
        counts["overall"] = int(prog_sum / len(js)) if js else 0
        return counts

    # ---------------- thao tác hàng đợi ----------------
    def import_paths(self, paths) -> dict:
        """Kéo-thả / thêm file&folder vào Queue (dedup, bỏ video đã hoàn thành)."""
        return local_source.ingest_paths(self.db, paths)

    def next_pending(self) -> str | None:
        """Video kế tiếp CẦN xử lý (Waiting/Interrupted), theo thứ tự thêm (FIFO).
        KHÔNG tự chạy lại Failed (chỉ chạy lại khi bấm Retry)."""
        rows = sorted(self.db.all_video_rows(), key=lambda r: (r.get("added_at") or ""))
        for r in rows:
            if r.get("download_status") != "downloaded" or r.get("edit_status") == "done":
                continue
            if display_status(r) in (Status.WAITING, Status.INTERRUPTED):
                return r.get("video_id")
        return None

    def runnable_ids(self) -> list[str]:
        """Id CẦN xử lý: đã tải, chưa 'done', trạng thái Waiting/Interrupted/Failed."""
        ids = []
        for r in self.db.all_video_rows():
            if r.get("download_status") != "downloaded" or r.get("edit_status") == "done":
                continue
            if display_status(r) in Status.RUNNABLE:
                ids.append(r.get("video_id"))
        return ids

    def resume(self) -> None:
        self.db.resume_queue()

    def retry_failed(self) -> list[str]:
        ids = []
        for r in self.db.all_video_rows():
            if r.get("edit_status") == "failed" or display_status(r) == Status.FAILED:
                self.db.set_edit_status(r.get("video_id"), "pending")
                self.db.update_job(r.get("video_id"), job_status=Status.WAITING, persist=False)
                ids.append(r.get("video_id"))
        if ids:
            self.db._save()  # lưu 1 lần
        return ids

    def remove_from_queue(self, video_ids) -> int:
        """Ẩn video khỏi Queue nhưng giữ nguyên bản ghi, file tải và file đầu ra."""
        wanted = set(video_ids)
        count = 0
        for row in self.db.all_video_rows():
            video_id = row.get("video_id")
            if video_id in wanted and row.get("job_status") != Status.REMOVED:
                self.db.update_job(video_id, job_status=Status.REMOVED, persist=False)
                count += 1
        if count:
            self.db._save()
        return count

    def rebuild_queue(self) -> int:
        """Nạp lại các video đã tải từng bị loại khỏi Queue."""
        count = 0
        for row in self.db.all_video_rows():
            if (row.get("download_status") == "downloaded" and
                    row.get("job_status") == Status.REMOVED):
                status = Status.COMPLETED if row.get("edit_status") == "done" else Status.WAITING
                self.db.update_job(row.get("video_id"), job_status=status, persist=False)
                count += 1
        if count:
            self.db._save()
        return count
