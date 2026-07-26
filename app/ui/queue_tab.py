"""Giao diện quản lý hàng đợi biên tập video."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QThread, QSize, QTimer
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..editor import export, smart_crop
from ..editor.stages import Status, fmt_eta
from ..queue_manager import QueueManager
from ..paths import existing_output_video_path
from ..workers.edit_worker import EditWorker
from .theme import make_columns_resizable

_HEADERS = ["", "Video", "Trạng thái", "Công đoạn / Tiến trình", "Còn lại", "Thao tác"]
_THUMB = QSize(96, 54)

_STATUS_TEXT = {
    Status.WAITING: "Đang chờ", Status.PREPARING: "Đang chuẩn bị",
    Status.PROCESSING: "Đang xử lý", Status.RENDERING: "Đang xuất video",
    Status.COMPLETED: "Hoàn thành", Status.FAILED: "Lỗi",
    Status.PAUSED: "Tạm dừng", Status.CANCELLED: "Đã hủy",
    Status.INTERRUPTED: "Bị gián đoạn",
}
_STAGE_TEXT = {
    "Reading Metadata": "Đọc thông tin video", "Smart Crop": "Căn khung hình",
    "Audio Processing": "Xử lý âm thanh", "Speech Recognition": "Nhận diện lời nói",
    "Subtitle": "Tạo phụ đề", "Translation": "Dịch phụ đề", "TTS": "Tạo giọng đọc",
    "Rendering": "Kết xuất video", "Saving": "Lưu kết quả", "Completed": "Hoàn tất",
}
_STATUS_COLORS = {
    Status.WAITING: ("#475569", "#f1f5f9"), Status.INTERRUPTED: ("#9a3412", "#fff7ed"),
    Status.PAUSED: ("#9a3412", "#fff7ed"), Status.PREPARING: ("#1d4ed8", "#eff6ff"),
    Status.PROCESSING: ("#1d4ed8", "#eff6ff"), Status.RENDERING: ("#1d4ed8", "#eff6ff"),
    Status.COMPLETED: ("#15803d", "#f0fdf4"), Status.FAILED: ("#b91c1c", "#fef2f2"),
    Status.CANCELLED: ("#475569", "#f1f5f9"),
}


def _fmt_dur(sec) -> str:
    s = int(float(sec or 0))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _open(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.startfile(path)
    except Exception:
        pass


class ThumbnailWorker(QThread):
    ready = Signal(str, str)

    def __init__(self, jobs, cache_dir):
        super().__init__()
        self._jobs, self._cache, self._stop = jobs, cache_dir, False

    def stop(self):
        self._stop = True

    def run(self):
        for vid, src in self._jobs:
            if self._stop:
                return
            png = os.path.join(self._cache, f"{vid}.png")
            if not os.path.exists(png):
                try:
                    smart_crop.grab_frame(src, png, at_seconds=1.0)
                except Exception:
                    continue
            self.ready.emit(vid, png)


class DashboardWidget(QFrame):
    """Thanh trạng thái gọn; badge lỗi/chờ có thể dùng như bộ lọc."""
    filter_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("queueDashboard")
        self.setStyleSheet("#queueDashboard{background:#f8fafc;border:1px solid #e2e8f0;border-radius:7px;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        title = QVBoxLayout()
        heading = QLabel("Hàng đợi biên tập")
        heading.setStyleSheet("font-size:16px;font-weight:600;")
        self.state = QLabel("● Sẵn sàng")
        self.state.setStyleSheet("color:#15803d;")
        title.addWidget(heading)
        title.addWidget(self.state)
        lay.addLayout(title)
        lay.addSpacing(12)

        self._badges = {}
        for key, text in (("processing", "Đang xử lý"), ("waiting", "Đang chờ"),
                          ("completed", "Hoàn thành"), ("failed", "Lỗi")):
            b = QPushButton(f"0  {text}")
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.filter_requested.emit(k))
            lay.addWidget(b)
            self._badges[key] = (b, text)
        lay.addStretch(1)
        progress = QVBoxLayout()
        self.summary = QLabel("0/0 video hoàn thành")
        self.summary.setAlignment(Qt.AlignRight)
        self.bar = QProgressBar()
        self.bar.setFixedWidth(260)
        self.bar.setFormat("%p%")
        progress.addWidget(self.summary)
        progress.addWidget(self.bar)
        lay.addLayout(progress)

    def update_stats(self, d: dict) -> None:
        for key, (button, text) in self._badges.items():
            button.setText(f"{d.get(key, 0)}  {text}")
        self.summary.setText(f"{d.get('completed', 0)}/{d.get('total', 0)} video hoàn thành")
        self.bar.setValue(int(d.get("overall", 0)))

    def set_running(self, running: bool, paused: bool = False) -> None:
        if paused:
            self.state.setText("● Đang tạm dừng")
            self.state.setStyleSheet("color:#b45309;")
        elif running:
            self.state.setText("● Đang xử lý")
            self.state.setStyleSheet("color:#2563eb;")
        else:
            self.state.setText("● Sẵn sàng")
            self.state.setStyleSheet("color:#15803d;")


class EditQueueWidget(QTableWidget):
    paths_dropped = Signal(list)
    job_selected = Signal(str)
    action_requested = Signal(str, str)

    def __init__(self):
        super().__init__(0, len(_HEADERS))
        self.setHorizontalHeaderLabels(_HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setStyleSheet("""
            QTableWidget { border:1px solid #e2e8f0; border-radius:6px;
                background:#fff; alternate-background-color:#fafbfc;
                selection-background-color:#e8f1ff; selection-color:#0f172a; }
            QTableWidget::item { border-bottom:1px solid #eef2f7; padding:3px; }
            QTableWidget::item:hover { background:#f1f5f9; color:#0f172a; }
            QTableWidget::item:selected { background:#e8f1ff; color:#0f172a; }
            QHeaderView::section { background:#f8fafc; color:#334155; border:0;
                border-right:1px solid #e2e8f0; border-bottom:1px solid #dbe3ee;
                padding:6px; font-weight:600; }
            QTableWidget QPushButton { background:#fff; color:#334155;
                border:1px solid #d8e0ea; border-radius:5px; }
            QTableWidget QPushButton:hover { background:#edf4ff; color:#1d4ed8;
                border-color:#9bbcf1; }
            QTableWidget QPushButton:pressed { background:#dbeafe; }
        """)
        self.setIconSize(_THUMB)
        self.verticalHeader().setDefaultSectionSize(_THUMB.height() + 12)
        self.verticalHeader().hide()
        make_columns_resizable(
            self, [1.0, 2.8, 1.1, 2.5, 0.8, 1.0], min_width=100)
        self.setAcceptDrops(True)
        self._rows, self._jobs = {}, {}
        self.itemSelectionChanged.connect(self._on_sel)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            e.acceptProposedAction()

    def _on_sel(self):
        vid = self.selected_id()
        if vid:
            self.job_selected.emit(vid)

    def selected_id(self):
        rows = self.selectionModel().selectedRows()
        return self.item(rows[0].row(), 1).data(Qt.UserRole) if rows else None

    def selected_ids(self) -> list[str]:
        return [self.item(i.row(), 1).data(Qt.UserRole) for i in self.selectionModel().selectedRows()]

    def select_video(self, vid: str) -> None:
        row = self._rows.get(vid)
        if row is not None:
            self.selectRow(row)
            self.scrollToItem(self.item(row, 1), QAbstractItemView.EnsureVisible)

    def set_jobs(self, jobs) -> None:
        selected = self.selected_id()
        self.setRowCount(len(jobs))
        self._rows.clear()
        self._jobs = {j.video_id: j for j in jobs}
        for row, job in enumerate(jobs):
            self._rows[job.video_id] = row
            self.setItem(row, 0, QTableWidgetItem())
            title = QTableWidgetItem(f"{job.title}\n{_fmt_dur(job.duration)}")
            title.setData(Qt.UserRole, job.video_id)
            title.setToolTip(job.title)
            self.setItem(row, 1, title)
            self.setItem(row, 2, QTableWidgetItem())
            self._paint_status(row, job.status, job.error)
            self._set_progress_cell(row, job.stage, job.progress)
            self.setItem(row, 4, QTableWidgetItem("—"))
            self._set_action(row, job)
            if job.video_id == selected:
                self.selectRow(row)

    def _paint_status(self, row, status, error=""):
        fg, bg = _STATUS_COLORS.get(status, ("#334155", "#f8fafc"))
        host = QWidget(); box = QHBoxLayout(host)
        box.setContentsMargins(8, 14, 8, 14)
        badge = QLabel(_STATUS_TEXT.get(status, status or "Đang chờ"))
        badge.setAlignment(Qt.AlignCenter)
        badge.setToolTip(error or badge.text())
        badge.setStyleSheet(
            f"color:{fg};background:{bg};border:1px solid {bg};"
            "border-radius:9px;padding:2px 7px;font-weight:500;"
        )
        box.addWidget(badge)
        self.setCellWidget(row, 2, host)

    def _set_progress_cell(self, row, stage, pct):
        cell = QWidget(); lay = QVBoxLayout(cell)
        lay.setContentsMargins(4, 4, 4, 4); lay.setSpacing(2)
        label = QLabel(_STAGE_TEXT.get(stage, stage) if stage else "Chưa bắt đầu")
        label.setProperty("stageLabel", True)
        bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(int(pct or 0)); bar.setFixedHeight(16)
        lay.addWidget(label); lay.addWidget(bar)
        self.setCellWidget(row, 3, cell)

    def _set_action(self, row, job):
        if job.status == Status.COMPLETED:
            text, action = "Phát", "play"
        elif job.status in Status.ACTIVE:
            text, action = "Tạm dừng", "pause_current"
        else:
            text, action = "Xử lý ngay", "process_now"
        host = QWidget(); box = QHBoxLayout(host)
        box.setContentsMargins(8, 12, 8, 12)
        b = QPushButton(text); b.setFixedHeight(30)
        b.clicked.connect(lambda _=False, v=job.video_id, a=action: self.action_requested.emit(v, a))
        box.addWidget(b)
        self.setCellWidget(row, 5, host)

    def apply_filter(self, text: str, status_key: str) -> None:
        query = text.strip().lower()
        for vid, row in self._rows.items():
            job = self._jobs[vid]
            status_ok = (status_key == "all" or
                         status_key == "processing" and job.status in Status.ACTIVE or
                         status_key == "waiting" and job.status not in Status.ACTIVE | {Status.COMPLETED, Status.FAILED} or
                         status_key == "completed" and job.status == Status.COMPLETED or
                         status_key == "failed" and job.status == Status.FAILED)
            text_ok = not query or query in job.title.lower() or query in job.video_id.lower()
            self.setRowHidden(row, not (status_ok and text_ok))

    def set_thumbnail(self, vid: str, png: str) -> None:
        row = self._rows.get(vid)
        if row is not None and os.path.exists(png):
            self.item(row, 0).setIcon(QIcon(QPixmap(png).scaled(_THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def set_status(self, vid: str, status: str) -> None:
        row = self._rows.get(vid)
        if row is not None:
            self._jobs[vid].status = status
            self._paint_status(row, status, self._jobs[vid].error)
            self._set_action(row, self._jobs[vid])

    def set_stage(self, vid: str, stage: str) -> None:
        row = self._rows.get(vid)
        if row is not None:
            self._jobs[vid].stage = stage
            cell = self.cellWidget(row, 3)
            if cell:
                label = cell.findChild(QLabel)
                if label: label.setText(_STAGE_TEXT.get(stage, stage))

    def set_progress(self, vid: str, pct: int, eta: str) -> None:
        row = self._rows.get(vid)
        if row is not None:
            cell = self.cellWidget(row, 3)
            if cell:
                bar = cell.findChild(QProgressBar)
                if bar: bar.setValue(int(pct))
            self.item(row, 4).setText(eta)


class VideoInspectorWidget(QFrame):
    """Xem trước và chi tiết gọn của video đang chọn."""
    retry_requested = Signal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg, self._job = cfg, None
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 0, 0, 0)
        self.preview_heading = QLabel("Xem trước")
        self.preview_heading.setStyleSheet("font-size:14px;font-weight:600;")
        lay.addWidget(self.preview_heading)
        self.img = QLabel("Chọn một video để xem thông tin\nvà bản xem trước")
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setMinimumSize(260, 220)
        self.img.setMaximumHeight(300)
        self.img.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.img.setStyleSheet("background:#0f172a;color:#cbd5e1;border-radius:6px;")
        lay.addWidget(self.img)
        self.title = QLabel("Chưa chọn video"); self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight:600;")
        lay.addWidget(self.title)
        self.status = QLabel("—"); self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        lay.addWidget(self.progress)
        grid = QGridLayout()
        self.meta = {}
        for row, (key, caption) in enumerate((("source", "Nguồn"), ("output", "Đầu ra"),
                                               ("resolution", "Độ phân giải"), ("duration", "Thời lượng"))):
            grid.addWidget(QLabel(caption + ":"), row, 0)
            val = QLabel("—"); val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setToolTip(""); val.setWordWrap(False)
            grid.addWidget(val, row, 1); self.meta[key] = val
        lay.addLayout(grid)
        self.error = QLabel(); self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#b91c1c;background:#fef2f2;padding:6px;border-radius:4px;")
        self.error.hide(); lay.addWidget(self.error)
        actions = QHBoxLayout()
        self.btn_preview = QPushButton("Xem bản chỉnh sửa")
        self.btn_preview.setToolTip("Tạo một khung hình theo cấu hình edit hiện tại")
        self.btn_preview.clicked.connect(self.make_preview)
        self.btn_play = QPushButton("Phát video"); self.btn_play.clicked.connect(self._play)
        self.btn_open = QPushButton("Mở thư mục"); self.btn_open.clicked.connect(self._open_dir)
        self.btn_retry = QPushButton("Thử lại"); self.btn_retry.clicked.connect(self._retry)
        for button in (self.btn_preview, self.btn_play, self.btn_open, self.btn_retry): actions.addWidget(button)
        lay.addLayout(actions)
        self._technical_log = []
        self._sync_buttons()

    def set_job(self, job) -> None:
        self._job = job
        if not job:
            return
        self.title.setText(job.title)
        self.status.setText(f"{_STATUS_TEXT.get(job.status, job.status)} · {_STAGE_TEXT.get(job.stage, job.stage) or 'Chưa bắt đầu'}")
        self.progress.setValue(int(job.progress or 0))
        values = {"source": job.source_path or "—", "output": job.output_dir or "—",
                  "resolution": job.resolution or "—", "duration": _fmt_dur(job.duration)}
        for key, value in values.items():
            shown = value if len(value) < 54 else "…" + value[-51:]
            self.meta[key].setText(shown); self.meta[key].setToolTip(value)
        self.error.setText("Lỗi: " + job.error if job.error else "")
        self.error.setVisible(bool(job.error))
        self._technical_log.clear()
        self._sync_buttons()
        if job.status == Status.COMPLETED:
            self._show_output_thumb(job)
        else:
            self._show_source_thumb(job)

    def clear_job(self) -> None:
        self._job = None
        self.preview_heading.setText("Xem trước")
        self.img.clear(); self.img.setText("Chọn một video để xem thông tin\nvà bản xem trước")
        self.title.setText("Chưa chọn video"); self.status.setText("—"); self.progress.setValue(0)
        for value in self.meta.values(): value.setText("—")
        self.error.hide(); self._technical_log.clear(); self._sync_buttons()

    def _sync_buttons(self):
        has = self._job is not None
        completed = has and self._job.status == Status.COMPLETED
        self.btn_preview.setEnabled(has)
        self.btn_play.setEnabled(completed)
        self.btn_open.setEnabled(has)
        self.btn_retry.setVisible(has and self._job.status == Status.FAILED)

    def set_stage(self, stage):
        if self._job:
            self._job.stage = stage
            self.status.setText(f"{_STATUS_TEXT.get(self._job.status, self._job.status)} · {_STAGE_TEXT.get(stage, stage)}")

    def set_status(self, status):
        if self._job:
            self._job.status = status
            stage = _STAGE_TEXT.get(self._job.stage, self._job.stage) or "Chưa bắt đầu"
            self.status.setText(f"{_STATUS_TEXT.get(status, status)} · {stage}")
            self._sync_buttons()

    def set_progress(self, pct, _eta, _elapsed=""):
        self.progress.setValue(int(pct))

    def append_log(self, msg):
        self._technical_log.append(str(msg))

    def _show_source_thumb(self, job):
        self.preview_heading.setText("Xem trước · Video nguồn")
        self.img.clear()
        self.img.setText("Đang tải khung hình xem trước…")
        if not job.source_path or not os.path.exists(job.source_path):
            self.img.setText("Không tìm thấy file video nguồn")
            return
        try:
            png = os.path.join(tempfile.gettempdir(), f"vrs_source_{job.video_id}.png")
            if not os.path.exists(png):
                smart_crop.grab_frame(job.source_path, png, at_seconds=1.0)
            self.img.setPixmap(QPixmap(png).scaled(
                self.img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            self.img.setText("Không tạo được ảnh xem trước")

    def _show_output_thumb(self, job):
        self.preview_heading.setText("Xem trước · Video đầu ra")
        full = str(existing_output_video_path(job.output_dir, job.source_path, job.video_id, "full"))
        if os.path.exists(full):
            try:
                png = os.path.join(tempfile.gettempdir(), f"vrs_prev_{job.video_id}.png")
                smart_crop.grab_frame(full, png, at_seconds=0.5)
                self.img.setPixmap(QPixmap(png).scaled(self.img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            except Exception:
                pass

    def make_preview(self):
        job = self._job
        if not job or not job.source_path or not os.path.exists(job.source_path):
            QMessageBox.information(self, "Xem thử", "Video không còn file nguồn để tạo xem thử.")
            return
        try:
            png = os.path.join(tempfile.gettempdir(), f"vrs_preview_{job.video_id}.png")
            export.preview_frame(self.cfg.editor, job.source_path, png, at_seconds=1.0)
            self.img.setPixmap(QPixmap(png).scaled(self.img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.preview_heading.setText("Xem trước · Theo cấu hình edit")
        except Exception as exc:
            QMessageBox.warning(self, "Không tạo được xem thử", str(exc))

    def _play(self):
        if self._job:
            full = str(existing_output_video_path(
                self._job.output_dir, self._job.source_path, self._job.video_id, "full"))
            _open(full)

    def _open_dir(self):
        if self._job: _open(self._job.output_dir)

    def _retry(self):
        if self._job: self.retry_requested.emit(self._job.video_id)


class QueueTab(QWidget):
    # Notify the main window whenever queue state persisted in the DB changes.
    data_changed = Signal()
    # Tiến trình biên tập (video_id, %, eta) -> để tab Tải hiện % xử lý.
    edit_progress = Signal(str, int, str)

    def __init__(self, cfg, db, device: str = "auto"):
        super().__init__()
        self.cfg, self.db, self.device = cfg, db, device
        self.qm = QueueManager(cfg, db)
        self._worker = None
        self._thumb = None
        self._current = None
        self._starts = {}
        self._cache = tempfile.mkdtemp(prefix="vrs_thumb_")

        root = QVBoxLayout(self); root.setContentsMargins(12, 10, 12, 10); root.setSpacing(8)
        self.setStyleSheet("""
            QueueTab QPushButton, QueueTab QToolButton {
                min-height:24px; padding:2px 10px; background:#fff; color:#334155;
                border:1px solid #d8e0ea; border-radius:5px; }
            QueueTab QPushButton:hover, QueueTab QToolButton:hover {
                background:#f1f5f9; color:#1e40af; border-color:#a9bdd8; }
            QueueTab QPushButton:pressed, QueueTab QToolButton:pressed { background:#e2e8f0; }
            QueueTab QPushButton:disabled { background:#f8fafc; color:#a8b2c1;
                border-color:#e5eaf0; }
            QueueTab QPushButton#primaryQueueButton {
                background:#2563eb; color:#fff; border-color:#2563eb; font-weight:600; }
            QueueTab QPushButton#primaryQueueButton:hover {
                background:#1d4ed8; color:#fff; border-color:#1d4ed8; }
        """)
        self.dash = DashboardWidget(); self.dash.filter_requested.connect(self._filter_from_badge)
        root.addWidget(self.dash)
        root.addLayout(self._build_controls())
        self.queue = EditQueueWidget()
        self.queue.paths_dropped.connect(self._on_paths_dropped)
        self.queue.job_selected.connect(self._on_job_selected)
        self.queue.action_requested.connect(self._on_row_action)
        self.queue.itemSelectionChanged.connect(self._selection_changed)
        self.preview = VideoInspectorWidget(cfg)
        self.preview.retry_requested.connect(self._retry_one)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.queue); split.addWidget(self.preview)
        split.setStretchFactor(0, 7); split.setStretchFactor(1, 3)
        split.setSizes([900, 390])
        root.addWidget(split, 1)
        self.refresh()

    def _build_controls(self):
        row = QHBoxLayout(); row.setSpacing(6)
        self.search = QLineEdit(); self.search.setPlaceholderText("Tìm theo tên hoặc mã video…")
        self.search.setClearButtonEnabled(True); self.search.setMinimumWidth(220); self.search.setMaximumWidth(320)
        self.search.textChanged.connect(self._apply_filter); row.addWidget(self.search)
        self.filter = QComboBox()
        for text, key in (("Tất cả trạng thái", "all"), ("Đang xử lý", "processing"),
                          ("Đang chờ", "waiting"), ("Hoàn thành", "completed"), ("Lỗi", "failed")):
            self.filter.addItem(text, key)
        self.filter.currentIndexChanged.connect(self._apply_filter); row.addWidget(self.filter)
        self.selected_label = QLabel(); self.selected_label.hide(); row.addWidget(self.selected_label)
        row.addStretch(1)
        self.btn_remove_selected = QPushButton("Xóa đã chọn")
        self.btn_remove_selected.setToolTip(
            "Giữ Ctrl hoặc Shift để chọn nhiều dòng, sau đó xóa khỏi hàng đợi")
        self.btn_remove_selected.clicked.connect(self.on_remove_selected)
        self.btn_remove_selected.setEnabled(False)
        self.btn_remove_selected.hide()
        row.addWidget(self.btn_remove_selected)
        self.btn_import = QPushButton("+ Nhập video")
        self.btn_import.setToolTip("Chọn một hoặc nhiều file video để thêm vào hàng đợi")
        self.btn_import.clicked.connect(self.on_import)
        row.addWidget(self.btn_import)
        self.btn_import_folder = QPushButton("+ Nhập thư mục")
        self.btn_import_folder.setToolTip(
            "Thêm toàn bộ video trong một thư mục và các thư mục con vào hàng đợi")
        self.btn_import_folder.clicked.connect(self.on_import_folder)
        row.addWidget(self.btn_import_folder)
        self.btn_rebuild = QPushButton("Cập nhật danh sách")
        self.btn_rebuild.setToolTip("Nạp lại các video đã tải vào hàng đợi")
        self.btn_rebuild.clicked.connect(self.on_rebuild_queue)
        row.addWidget(self.btn_rebuild)
        self.btn_primary = QPushButton("Bắt đầu hàng đợi")
        self.btn_primary.setObjectName("primaryQueueButton")
        self.btn_primary.clicked.connect(self._primary_action); row.addWidget(self.btn_primary)
        self.btn_stop = QPushButton("Dừng"); self.btn_stop.clicked.connect(self.on_stop); self.btn_stop.hide()
        row.addWidget(self.btn_stop)
        more = QToolButton(); more.setText("Thao tác khác ⋯"); more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        for text, fn in (("Thử lại tất cả video lỗi", self.on_retry),
                         ("Xóa video đã hoàn thành", self.on_remove_completed),
                         ("Mở thư mục video nguồn", self.on_open_source),
                         ("Mở thư mục đầu ra", self.on_open_output)):
            menu.addAction(QAction(text, menu, triggered=fn))
        menu.addSeparator()
        clear = QAction("Xóa toàn bộ hàng đợi", menu, triggered=self.on_clear); menu.addAction(clear)
        more.setMenu(menu); row.addWidget(more)
        return row

    def refresh(self) -> None:
        jobs = self.qm.jobs()
        self.queue.set_jobs(jobs); self.dash.update_stats(self.qm.dashboard()); self._apply_filter()
        self._stop_thumb()
        pairs = [(j.video_id, j.source_path) for j in jobs if j.source_path]
        if pairs:
            self._thumb = ThumbnailWorker(pairs, self._cache)
            self._thumb.ready.connect(self.queue.set_thumbnail); self._thumb.start()
        if self._current:
            job = next((j for j in jobs if j.video_id == self._current), None)
            if job:
                self.preview.set_job(job)
                self.queue.select_video(job.video_id)
            else:
                self._current = None
                self.preview.clear_job()
        if not self._current and jobs:
            # Mở thẳng video cần chú ý nhất thay vì để panel xem trước trống.
            job = next((j for j in jobs if j.status == Status.FAILED), None)
            job = job or next((j for j in jobs if j.status in Status.ACTIVE), None) or jobs[0]
            self._current = job.video_id
            self.queue.select_video(job.video_id)
            self.preview.set_job(job)

    def _apply_filter(self, *_):
        if hasattr(self, "queue"):
            self.queue.apply_filter(self.search.text(), self.filter.currentData())

    def _filter_from_badge(self, key):
        idx = self.filter.findData(key)
        if idx >= 0: self.filter.setCurrentIndex(idx)

    def _selection_changed(self):
        n = len(self.queue.selected_ids())
        self.selected_label.setText(f"Đã chọn {n} video")
        self.selected_label.setVisible(n > 1)
        self.btn_remove_selected.setText(f"Xóa đã chọn ({n})")
        self.btn_remove_selected.setEnabled(n > 0)
        self.btn_remove_selected.setVisible(n > 0)

    def _on_paths_dropped(self, paths, auto_start: bool = False):
        result = self.qm.import_paths(paths); self.refresh()
        self.data_changed.emit()
        total = int(result.get("total", 0))
        added = int(result.get("added", 0))
        waiting = len(result.get("eligible", []))
        completed = int(result.get("skipped_done", 0))
        if total == 0:
            QMessageBox.information(
                self, "Nhập video",
                "Không tìm thấy video hợp lệ trong nội dung đã chọn.")
            return
        if auto_start and waiting:
            # Dùng nguyên luồng hàng đợi cũ (EditWorker + FIFO), chỉ tự kích
            # hoạt sau khi import folder thay vì yêu cầu bấm thêm một lần.
            QTimer.singleShot(0, self.on_start)
        QMessageBox.information(
            self, "Nhập video",
            f"Đã tìm thấy {total} video.\n"
            f"• Mới thêm: {added}\n"
            f"• Có trong hàng đợi: {waiting}\n"
            f"• Đã hoàn thành, được bỏ qua: {completed}")

    def _on_job_selected(self, vid):
        self._current = vid
        job = next((j for j in self.qm.jobs() if j.video_id == vid), None)
        if job: self.preview.set_job(job)

    def _on_row_action(self, vid, action):
        self._current = vid
        self.queue.select_video(vid)
        job = next((j for j in self.qm.jobs() if j.video_id == vid), None)
        if job: self.preview.set_job(job)
        if action == "retry": self._retry_one(vid)
        elif action == "process_now": self._process_now(vid)
        elif action == "pause_current": self.on_stop()
        elif action == "play" and job:
            _open(str(existing_output_video_path(job.output_dir, job.source_path, job.video_id, "full")))

    def _retry_one(self, vid):
        self.db.set_edit_status(vid, "pending")
        self.db.update_job(vid, job_status=Status.WAITING, stage="", progress=0, error=None)
        self.refresh()
        self.data_changed.emit()
        if self.cfg.editor.auto_edit_after_download:
            self.on_start()

    def _primary_action(self):
        if not self._worker or not self._worker.isRunning(): self.on_start()
        elif self._worker.is_paused(): self.on_resume()
        else: self.on_pause()

    def on_start(self, preferred_id=None):
        if self._worker and self._worker.isRunning(): return
        if not self.qm.next_pending(preferred_id):
            self.refresh()
            return
        self._worker = EditWorker(
            self.cfg, self.db, device=self.device, preferred_id=preferred_id); w = self._worker
        w.status_changed.connect(self._on_status); w.stage_changed.connect(self._on_stage)
        w.progress_changed.connect(self._on_progress); w.job_completed.connect(self._on_completed)
        w.job_failed.connect(self._on_failed); w.job_cancelled.connect(self._on_cancelled)
        w.log.connect(self._on_log)
        w.queue_finished.connect(self._on_finished)
        self.btn_primary.setText("Tạm dừng sau video này")
        self.btn_stop.setText("Tạm dừng video")
        self.btn_stop.show(); self.dash.set_running(True); w.start()

    def on_pause(self):
        if self._worker:
            self._worker.pause(); self.btn_primary.setText("Tiếp tục"); self.dash.set_running(True, True)

    def on_resume(self):
        if self._worker:
            self._worker.resume()
            self.btn_primary.setText("Tạm dừng sau video này")
            self.dash.set_running(True)

    def _process_now(self, vid):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(
                self, "Video khác đang xử lý",
                "Hãy tạm dừng video hiện tại trước khi chạy video đã chọn.")
            return
        if self.qm.prepare_for_run(vid):
            self.refresh()
            self.data_changed.emit()
            self.on_start(preferred_id=vid)

    def on_stop(self):
        if not (self._worker and self._worker.isRunning()):
            return
        selected = self.queue.selected_id()
        # Nếu đang chọn một video KHÁC video đang chạy -> ưu tiên xử lý video đó tiếp theo.
        preferred = selected if selected and selected != self._current else None
        detail = ("Dừng ngay video đang xử lý và chuyển sang trạng thái 'Tạm dừng' "
                  "(không tự chạy lại; bấm 'Xử lý ngay' để tiếp tục sau).\n\n"
                  + ("Tiếp tục với video bạn đang chọn." if preferred
                     else "Nếu còn video khác đang chờ sẽ xử lý tiếp; hết thì dừng."))
        if QMessageBox.question(self, "Tạm dừng video hiện tại", detail,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._worker.skip_current(preferred_next=preferred)
            self.dash.state.setText("● Đang chuyển sang video khác…")
            self.dash.state.setStyleSheet("color:#b45309;")

    def on_retry(self):
        count = len(self.qm.retry_failed()); self.refresh()
        self.data_changed.emit()
        QMessageBox.information(self, "Thử lại", f"Đã đưa {count} video lỗi về trạng thái chờ.")
        if count and self.cfg.editor.auto_edit_after_download:
            self.on_start()

    def on_import(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Chọn video",
            "",
            "Video (*.mp4 *.mkv *.mov *.webm *.avi *.m4v)",
        )
        if files:
            self._on_paths_dropped(files)

    def on_import_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Chọn thư mục chứa danh sách video",
            "",
            QFileDialog.ShowDirsOnly,
        )
        if folder:
            self._on_paths_dropped([folder], auto_start=True)

    def on_remove_selected(self):
        ids = self.queue.selected_ids()
        jobs = {j.video_id: j for j in self.qm.jobs()}
        removable = [vid for vid in ids if vid in jobs and jobs[vid].status not in Status.ACTIVE]
        active = len(ids) - len(removable)
        if not removable:
            if active:
                QMessageBox.information(
                    self, "Không thể xóa",
                    "Video đang xử lý không thể xóa. Hãy tạm dừng video trước.")
            return
        detail = f"Xóa {len(removable)} video đang chọn khỏi hàng đợi?"
        if active:
            detail += f"\n\nBỏ qua {active} video đang được xử lý."
        if QMessageBox.question(
                self, "Xóa khỏi hàng đợi", detail,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.qm.remove_from_queue(removable)
            self._current = None
            self.preview.clear_job()
            self.refresh()
            self.data_changed.emit()

    def on_remove_completed(self):
        ids = [j.video_id for j in self.qm.jobs() if j.status == Status.COMPLETED]
        if ids and QMessageBox.question(self, "Xóa video hoàn thành", f"Xóa {len(ids)} video đã hoàn thành khỏi hàng đợi?") == QMessageBox.Yes:
            self.qm.remove_from_queue(ids); self.refresh()

    def on_clear(self):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "Hàng đợi đang chạy",
                                    "Hãy dừng video đang xử lý trước khi xóa hàng đợi.")
            return
        jobs = self.qm.jobs()
        if not jobs:
            return
        if QMessageBox.question(
                self, "Xóa toàn bộ hàng đợi",
                f"Xóa {len(jobs)} video khỏi danh sách hàng đợi?\n\n"
                "Video đã tải, kết quả đầu ra và lịch sử vẫn được giữ nguyên. "
                "Bạn có thể dùng ‘Cập nhật danh sách’ để nạp lại.") == QMessageBox.Yes:
            self.qm.remove_from_queue([j.video_id for j in jobs])
            self._current = None; self.preview.clear_job(); self.refresh()

    def on_rebuild_queue(self):
        count = self.qm.rebuild_queue()
        self.refresh()
        if count:
            QMessageBox.information(self, "Cập nhật danh sách",
                                    f"Đã nạp lại {count} video đã tải vào hàng đợi.")

    def on_open_source(self):
        job = next((j for j in self.qm.jobs() if j.video_id == self.queue.selected_id()), None)
        if job and job.source_path: _open(str(Path(job.source_path).parent))

    def on_open_output(self):
        _open(self.cfg.editor.output_dir)

    def _on_status(self, vid, status):
        import time
        self.queue.set_status(vid, status)
        if status == Status.PREPARING and vid not in self._starts: self._starts[vid] = time.time()
        self.dash.update_stats(self.qm.dashboard())
        if vid == self._current: self.preview.set_status(status)
        self.data_changed.emit()

    def _on_stage(self, vid, stage):
        self.queue.set_stage(vid, stage)
        if vid == self._current: self.preview.set_stage(stage)

    def _on_progress(self, vid, pct, eta):
        import time
        self.queue.set_progress(vid, pct, eta)
        self.edit_progress.emit(vid, int(pct), eta or "")
        if vid == self._current:
            self.preview.set_progress(pct, eta, fmt_eta(time.time() - self._starts.get(vid, time.time())))

    def _on_completed(self, vid):
        self.dash.update_stats(self.qm.dashboard())
        self.data_changed.emit()
        if vid == self._current:
            job = next((j for j in self.qm.jobs() if j.video_id == vid), None)
            if job: self.preview.set_job(job)

    def _on_failed(self, vid, err):
        self.dash.update_stats(self.qm.dashboard())
        self.data_changed.emit()
        if vid == self._current:
            self.preview.error.setText("Lỗi: " + err); self.preview.error.show()
            self.preview.append_log(f"[LỖI] {err}")

    def _on_cancelled(self, vid):
        if vid == self._current:
            self.preview.error.hide()
        self.refresh()
        self.data_changed.emit()

    def _on_log(self, vid, msg):
        if vid == self._current: self.preview.append_log(msg)

    def _on_finished(self):
        self.btn_primary.setText("Bắt đầu hàng đợi")
        self.btn_stop.setText("Dừng"); self.btn_stop.hide(); self.btn_stop.setEnabled(True)
        self.dash.set_running(False); self.refresh()
        self.data_changed.emit()

    def _stop_thumb(self) -> None:
        """Dừng và CHỜ luồng thumbnail kết thúc trước khi bỏ tham chiếu.
        Thiếu wait() -> QThread bị hủy khi còn chạy -> Qt abort cả ứng dụng."""
        t = self._thumb
        self._thumb = None
        if t is not None:
            t.stop()
            if not t.wait(5000):
                t.terminate(); t.wait(1000)

    def shutdown(self):
        w = self._worker
        if w and w.isRunning():
            w.request_stop()
            # ffmpeg bị kill trong ~0.1s; chờ pipeline nhả. Nếu còn kẹt
            # (vd. đang nạp model Whisper) mới buộc dừng để tránh crash lúc thoát.
            if not w.wait(10000):
                w.terminate(); w.wait(2000)
        self._stop_thumb()
