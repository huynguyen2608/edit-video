"""Quy tắc trạng thái chuẩn dùng chung cho kho dữ liệu, hàng đợi và giao diện."""
from __future__ import annotations

from .editor.stages import Status


_CONTROL_STATES = {
    Status.PAUSED, Status.CANCELLED, Status.INTERRUPTED, Status.REMOVED,
}


def _get(row, key: str, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def canonical_job_status(row) -> str:
    """Trạng thái duy nhất được dùng để hiển thị và lựa chọn job."""
    download = str(_get(row, "download_status", "") or "")
    edit = str(_get(row, "edit_status", "") or "")
    job = str(_get(row, "job_status", "") or "")
    if job == Status.REMOVED:
        return Status.REMOVED
    if download != "downloaded":
        return Status.WAITING
    if edit == "done":
        return Status.COMPLETED
    if edit == "failed":
        return Status.FAILED
    if job in _CONTROL_STATES | Status.ACTIVE:
        return job
    if edit == "processing":
        return Status.PROCESSING
    return Status.WAITING


def job_status_for_edit(download_status: str, edit_status: str,
                        current_job_status: str = "") -> str:
    """Suy ra job_status khi edit_status đổi, đồng thời bảo toàn lệnh điều khiển."""
    current = str(current_job_status or "")
    if current == Status.REMOVED:
        return current
    if edit_status == "done":
        return Status.COMPLETED
    if edit_status == "failed":
        return Status.FAILED
    if download_status != "downloaded":
        return current
    if edit_status == "processing":
        return current if current in Status.ACTIVE else Status.PREPARING
    if edit_status == "pending":
        return current if current in _CONTROL_STATES else Status.WAITING
    return current or Status.WAITING


def normalize_persisted_row(row: dict, interrupted: bool = False) -> bool:
    """Sửa trạng thái cũ/mâu thuẫn ngay trong một row; trả True nếu có thay đổi."""
    before = (
        row.get("download_status"), row.get("edit_status"),
        row.get("job_status"), row.get("stage"), row.get("progress"),
    )
    if interrupted and row.get("download_status") in ("downloading", "cancelling"):
        row["download_status"] = "paused"
    if interrupted and (
            row.get("edit_status") == "processing"
            or row.get("job_status") in Status.ACTIVE):
        row["edit_status"] = "pending"
        row["job_status"] = Status.INTERRUPTED
        row["stage"] = ""
    else:
        row["job_status"] = job_status_for_edit(
            row.get("download_status") or "", row.get("edit_status") or "pending",
            row.get("job_status") or "")
    if row.get("edit_status") == "done":
        row["stage"] = "Completed"
        row["progress"] = 100
    return before != (
        row.get("download_status"), row.get("edit_status"),
        row.get("job_status"), row.get("stage"), row.get("progress"),
    )
