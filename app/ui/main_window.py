"""Cửa sổ chính PySide6.

Hai tab:
  - Tải: tổng quan, cấu hình nguồn/thư mục, quét và bảng trạng thái video.
  - Biên tập: tóm tắt preset, nút "Edit video mới", nút "Chọn vùng crop", log.

Nguyên tắc: mọi việc nặng chạy trên QThread (workers/jobs.py). UI chỉ nhận signal
cập nhật. Scheduler tự động 30 phút chạy nền; callback của nó KHÔNG chạm widget trực
tiếp mà đi qua _SignalBridge (queued signal) để an toàn đa luồng Qt.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QRect, QSize, QPoint, QEvent
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QPlainTextEdit, QLabel, QTabWidget,
    QHeaderView, QMessageBox, QDialog, QCheckBox, QFileDialog, QComboBox,
    QGroupBox, QFormLayout, QSpinBox, QDoubleSpinBox, QLineEdit, QScrollArea,
    QListWidget, QListWidgetItem, QLayout, QSizePolicy, QStackedWidget, QAbstractSpinBox,
    QAbstractItemView,
)

from ..config import AppConfig, ChannelCfg, EditorCfg, save_config
from ..store import ExcelStore
from ..logging_setup import get_logger
from ..downloader.scheduler import ScanService
from ..downloader.downloader import quality_format
from ..editor import smart_crop, video_ops, preview
from ..editor.local_source import ingest_folder
from .queue_tab import QueueTab
from ..workers.jobs import ScanWorker, CheckChannelWorker, ManualDownloadWorker
from ..presets import PLATFORM_PRESETS, apply_platform_preset
from .. import report

_COLS = ["Video ID", "Tiêu đề", "Kênh", "Tải", "Biên tập", "Thao tác"]
log = get_logger("ui")


class FlowLayout(QLayout):
    """Layout tự xếp widget theo hàng, TỰ WRAP xuống dòng theo bề rộng khả dụng.

    Dùng để các card cài đặt hiện 2 (hoặc nhiều) cột khi cửa sổ rộng, 1 cột khi hẹp.
    (Phỏng theo ví dụ FlowLayout chính thức của Qt.)
    """
    def __init__(self, parent=None, spacing: int = 10):
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        if parent is not None:
            self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            hint = it.sizeHint()
            next_x = x + hint.width() + sp
            if next_x - sp > rect.right() and line_h > 0:
                x = rect.x()
                y = y + line_h + sp
                next_x = x + hint.width() + sp
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class _SignalBridge(QObject):
    """Chuyển callback từ thread nền (scheduler, hàng đợi edit) về UI thread."""
    log = Signal(str)       # log tab Tải
    log_ed = Signal(str)    # log tab Cài đặt edit
    status = Signal(str, str, str)
    edited = Signal(str)    # (dự phòng)
    downloaded = Signal(str)  # tải xong 1 video (từ thread scheduler) -> UI thread


class _CropCanvas(QLabel):
    """Ảnh preview có thể bấm để chọn tâm vùng crop; vẽ khung giữ lại theo tỉ lệ."""
    def __init__(self, pixmap: QPixmap, aspect_ratio: float):
        super().__init__()
        self._ar = aspect_ratio
        self.fx = 0.5
        self.fy = 0.5
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())

    def mousePressEvent(self, ev) -> None:
        w, h = max(1, self.width()), max(1, self.height())
        self.fx = min(1.0, max(0.0, ev.position().x() / w))
        self.fy = min(1.0, max(0.0, ev.position().y() / h))
        self.update()

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        w, h = self.width(), self.height()
        src_ar = w / h
        if src_ar >= self._ar:
            ch = float(h); cw = ch * self._ar
        else:
            cw = float(w); ch = cw / self._ar
        cx, cy = self.fx * w, self.fy * h
        x = min(max(0.0, cx - cw / 2), w - cw)
        y = min(max(0.0, cy - ch / 2), h - ch)
        p = QPainter(self)
        p.setPen(QPen(QColor(0, 220, 120), 3))
        p.drawRect(int(x), int(y), int(cw), int(ch))
        p.setPen(QPen(QColor(255, 60, 60), 2))
        p.drawEllipse(int(cx) - 4, int(cy) - 4, 8, 8)
        p.end()


class CropSelectDialog(QDialog):
    """Dialog chọn vùng crop thủ công: bấm vào tâm vùng action chính."""
    def __init__(self, image_path: str, aspect_ratio: float,
                 init_focus: tuple[float, float] = (0.5, 0.5), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn vùng crop — bấm vào tâm vùng action chính")
        pix = QPixmap(image_path)
        if pix.width() > 900 or pix.height() > 900:
            pix = pix.scaled(900, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.canvas = _CropCanvas(pix, aspect_ratio)
        self.canvas.fx, self.canvas.fy = init_focus

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Khung xanh = vùng sẽ GIỮ LẠI (đúng tỉ lệ đầu ra). "
                             "Bấm vào ảnh để chọn tâm."))
        lay.addWidget(self.canvas)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Huỷ"); cancel.clicked.connect(self.reject)
        ok = QPushButton("Lưu vùng"); ok.clicked.connect(self.accept)
        btns.addWidget(cancel); btns.addWidget(ok)
        lay.addLayout(btns)

    def focus(self) -> tuple[float, float]:
        return self.canvas.fx, self.canvas.fy


class _PosCanvas(QLabel):
    """Ảnh preview; bấm vào -> phát (fx, fy) chuẩn hoá 0..1."""
    clicked = Signal(float, float)

    def __init__(self, pixmap: QPixmap):
        super().__init__()
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())

    def mousePressEvent(self, ev) -> None:
        w, h = max(1, self.width()), max(1, self.height())
        self.clicked.emit(min(1.0, max(0.0, ev.position().x() / w)),
                          min(1.0, max(0.0, ev.position().y() / h)))


class _InteractivePreview(QLabel):
    """Preview co giãn, click trực tiếp để đặt vị trí logo/hook/CTA."""
    clicked = Signal(float, float)

    def __init__(self, text: str = ""):
        super().__init__(text)
        self._source = QPixmap()
        self._markers: dict[str, tuple[float, float]] = {}
        self._active = "logo"
        self.setAlignment(Qt.AlignCenter)

    def set_source(self, pixmap: QPixmap) -> None:
        self._source = pixmap
        self._refresh_pixmap()

    def set_marker(self, key: str, fx: float, fy: float) -> None:
        self._markers[key] = (max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy)))
        self.update()

    def set_active(self, key: str) -> None:
        self._active = key
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if not self._source.isNull():
            self.setPixmap(self._source.scaled(
                max(1, self.width() - 12), max(1, self.height() - 12),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def mousePressEvent(self, event) -> None:
        pix = self.pixmap()
        if pix is None or pix.isNull():
            return
        x0 = (self.width() - pix.width()) / 2
        y0 = (self.height() - pix.height()) / 2
        fx = (event.position().x() - x0) / max(1, pix.width())
        fy = (event.position().y() - y0) / max(1, pix.height())
        if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
            self.clicked.emit(fx, fy)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._markers or self.pixmap() is None or self.pixmap().isNull():
            return
        pix = self.pixmap()
        x0 = (self.width() - pix.width()) / 2
        y0 = (self.height() - pix.height()) / 2
        painter = QPainter(self)
        colors = {"logo": "#22c55e", "hook": "#38bdf8", "cta": "#fb923c"}
        letters = {"logo": "L", "hook": "H", "cta": "C"}
        for key, (fx, fy) in self._markers.items():
            x = x0 + fx * pix.width(); y = y0 + fy * pix.height()
            width = 4 if key == self._active else 2
            painter.setPen(QPen(QColor(colors.get(key, "#ffffff")), width))
            painter.drawEllipse(int(x) - 10, int(y) - 10, 20, 20)
            painter.drawText(int(x) - 4, int(y) + 5, letters.get(key, "•"))
        painter.end()


class PositionPickerDialog(QDialog):
    """Bấm lên khung xem trước để đặt vị trí Logo / Hook / CTA (theo vùng)."""
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Đặt vị trí trực quan — chọn phần rồi bấm lên ảnh")
        self.positions: dict[str, str] = {}
        pix = QPixmap(image_path)
        if pix.height() > 820:
            pix = pix.scaledToHeight(820, Qt.SmoothTransformation)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Đang đặt cho:"))
        self.cmb_target = QComboBox()
        for val, lbl in [("logo", "Logo"), ("hook", "Hook"), ("cta", "CTA")]:
            self.cmb_target.addItem(lbl, val)
        top.addWidget(self.cmb_target)
        self.lbl_info = QLabel("(chưa đặt)")
        top.addWidget(self.lbl_info, 1)
        lay.addLayout(top)
        self.canvas = _PosCanvas(pix)
        self.canvas.clicked.connect(self._on_click)
        lay.addWidget(self.canvas)
        btns = QHBoxLayout(); btns.addStretch(1)
        cancel = QPushButton("Huỷ"); cancel.clicked.connect(self.reject)
        ok = QPushButton("Lưu vị trí"); ok.clicked.connect(self.accept)
        btns.addWidget(cancel); btns.addWidget(ok); lay.addLayout(btns)

    def _on_click(self, fx: float, fy: float) -> None:
        target = self.cmb_target.currentData()
        if target == "logo":   # 4 góc
            pos = ("top" if fy < 0.5 else "bottom") + "-" + ("left" if fx < 0.5 else "right")
        else:                  # hook/CTA: top/middle/bottom
            pos = "top" if fy < 0.34 else ("middle" if fy < 0.67 else "bottom")
        self.positions[target] = pos
        self.lbl_info.setText(" | ".join(f"{k}={v}" for k, v in self.positions.items()))


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig, db: ExcelStore, device: str = "auto",
                 config_path: str | Path | None = None):
        super().__init__()
        self.cfg = cfg
        self.db = db
        self.device = device
        self.config_path = config_path
        self._scan_worker: ScanWorker | None = None
        self._check_worker: CheckChannelWorker | None = None
        self._manual_worker: ManualDownloadWorker | None = None
        self._audio_labels: dict = {}   # attr -> QLabel (nhạc thay / voiceover)
        self._pos_combos: dict = {}     # 'hook'/'cta' -> combobox vị trí (đồng bộ khi đặt trực quan)

        self.setWindowTitle("Video Repurpose Studio")
        self.resize(1000, 640)
        db.resume_queue()   # job đang chạy dở lần trước -> Interrupted để tiếp tục
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_download_tab(), "Tải")
        self.tabs.addTab(self._build_edit_tab(), "Cài đặt edit")
        # Queue Manager là nơi điều khiển + theo dõi tiến trình edit (driver duy nhất).
        self._queue_tab = QueueTab(cfg, db, device=device)
        self._queue_idx = self.tabs.addTab(self._queue_tab, "Hàng đợi")
        self._history_idx = self.tabs.addTab(self._build_history_tab(), "Lịch sử / Xuất bản")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self._install_wheel_guards(self)

        self._reload_table()
        self._reload_history()

        # Bridge: callback từ thread nền (scheduler) -> UI thread.
        self._bridge = _SignalBridge()
        self._bridge.log.connect(self._log_dl)
        self._bridge.log_ed.connect(self._log_ed)
        self._bridge.status.connect(self._on_auto_status)
        self._bridge.downloaded.connect(self._on_downloaded)   # scheduler-thread -> UI thread

        # Scheduler tự động 30': tải xong -> báo (qua bridge) để cập nhật Hàng đợi.
        self._auto = ScanService(cfg, db,
                                 on_log=self._bridge.log.emit,
                                 on_status=lambda a, b, c: self._bridge.status.emit(a, b, c),
                                 on_downloaded=lambda vid: self._bridge.downloaded.emit(vid))
        self._auto.start_scheduler()

    # ---------------- Tab Tải ----------------
    def _build_download_tab(self) -> QWidget:
        w = QWidget()
        w.setObjectName("downloadTab")
        # Khai báo đồng thời nền và màu chữ cho mọi trạng thái để không phụ thuộc
        # highlight xanh đậm mặc định của Windows.
        w.setStyleSheet("""
            #downloadTab QTableWidget {
                background:#ffffff; alternate-background-color:#f8fafc;
                color:#0f172a; gridline-color:#e2e8f0;
                selection-background-color:#dbeafe; selection-color:#0f172a;
            }
            #downloadTab QTableWidget::item:hover {
                background:#eff6ff; color:#0f172a;
            }
            #downloadTab QTableWidget::item:selected {
                background:#dbeafe; color:#0f172a;
            }
            #downloadTab QPushButton {
                background:#ffffff; color:#1f2937; border:1px solid #cbd5e1;
                border-radius:5px; padding:4px 11px;
            }
            #downloadTab QPushButton:hover {
                background:#e8f1ff; color:#1e3a8a; border-color:#93b4df;
            }
            #downloadTab QPushButton:pressed {
                background:#cfe2ff; color:#172554; border-color:#6b9bd2;
            }
            #downloadTab QPushButton:disabled {
                background:#f1f5f9; color:#94a3b8; border-color:#e2e8f0;
            }
            #downloadTab QTableWidget QPushButton {
                background:#ffffff; color:#334155; padding:2px 8px;
            }
            #downloadTab QTableWidget QPushButton:hover {
                background:#dbeafe; color:#1e3a8a; border-color:#93b4df;
            }
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # Hàng 1/2: trạng thái, tổng số, lịch tự động và hành động chính.
        top = QHBoxLayout(); top.setSpacing(8)
        top.addWidget(QLabel("<b>Tải video</b>"))
        self.lbl_download_activity = QLabel("● Sẵn sàng")
        self.lbl_download_activity.setStyleSheet("color: #15803d;")
        top.addWidget(self.lbl_download_activity)
        self.lbl_download_summary = QLabel("Chờ 0 · Đang tải 0 · Đã tải 0 · Lỗi 0")
        self.lbl_download_summary.setStyleSheet("color:#475569;")
        top.addWidget(self.lbl_download_summary)
        top.addStretch(1)
        self.chk_auto = QCheckBox(
            f"Tự động quét mỗi {self.cfg.download.scan_interval_minutes} phút")
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(self._toggle_auto)
        top.addWidget(self.chk_auto)
        self.btn_scan = QPushButton("Quét ngay")
        self.btn_scan.setMinimumWidth(120)
        self.btn_scan.setStyleSheet(
            "QPushButton {background:#2563eb;color:white;font-weight:600;padding:7px 16px;"
            "border:0;border-radius:5px;} "
            "QPushButton:hover {background:#1d4ed8;color:#ffffff;} "
            "QPushButton:pressed {background:#1e40af;color:#ffffff;} "
            "QPushButton:disabled {background:#94a3b8;color:#f8fafc;}")
        self.btn_scan.clicked.connect(self.on_scan_clicked)
        top.addWidget(self.btn_scan)
        self.btn_stop_downloads = QPushButton("Dừng tất cả")
        self.btn_stop_downloads.setStyleSheet("color:#b91c1c;border-color:#fecaca;")
        self.btn_stop_downloads.clicked.connect(self.on_stop_all_downloads)
        self.btn_stop_downloads.hide()
        top.addWidget(self.btn_stop_downloads)
        self.btn_resume_downloads = QPushButton("Tiếp tục tải")
        self.btn_resume_downloads.clicked.connect(self.on_resume_all_downloads)
        self.btn_resume_downloads.hide()
        top.addWidget(self.btn_resume_downloads)
        lay.addLayout(top)

        # Hàng 2/2: toàn bộ cấu hình thường dùng + tải URL.
        tools = QHBoxLayout(); tools.setSpacing(6)
        tools.addWidget(QLabel("Thư mục:"))
        self.lbl_root = QLabel(self.cfg.download.root_dir)
        self.lbl_root.setMaximumWidth(260)
        self.lbl_root.setToolTip(self.cfg.download.root_dir)
        tools.addWidget(self.lbl_root)
        btn_root = QPushButton("Đổi")
        btn_root.clicked.connect(self.on_pick_root_dir)
        tools.addWidget(btn_root)
        tools.addWidget(QLabel("Cookie:"))
        self.cmb_cookies = QComboBox()
        self.cmb_cookies.setMaximumWidth(110)
        self.cmb_cookies.addItems(self._BROWSERS)
        cur = (self.cfg.download.cookies_from_browser or "").strip().lower()
        if cur and cur not in self._BROWSERS:
            self.cmb_cookies.addItem(cur)
        self.cmb_cookies.setCurrentText(cur if cur else self._BROWSERS[0])
        self.cmb_cookies.currentTextChanged.connect(self._on_cookies_changed)
        tools.addWidget(self.cmb_cookies)
        tools.addWidget(QLabel("Chất lượng:"))
        self.cmb_quality = QComboBox(); self.cmb_quality.setMaximumWidth(105)
        for label, height in (("Tốt nhất", 0), ("4K (2160p)", 2160), ("2K (1440p)", 1440),
                              ("Full HD", 1080), ("HD 720p", 720),
                              ("SD 480p", 480), ("360p", 360)):
            self.cmb_quality.addItem(label, height)
        selected_quality = int(getattr(self.cfg.download, "quality_height", 1080))
        qidx = self.cmb_quality.findData(selected_quality)
        self.cmb_quality.setCurrentIndex(qidx if qidx >= 0 else self.cmb_quality.findData(1080))
        self.cmb_quality.setToolTip(
            "Nếu không có đúng chất lượng, ứng dụng tự lấy mức cao nhất thấp hơn và gần nhất.")
        self.cmb_quality.currentIndexChanged.connect(self._on_quality_changed)
        tools.addWidget(self.cmb_quality)
        self.lbl_channels = QLabel(self._channels_summary())
        self.lbl_channels.hide()  # giữ tương thích callback cũ; nút dưới là UI chính.
        self.btn_sources = QPushButton(f"Nguồn ({len(self.cfg.download.channels)})")
        self.btn_sources.clicked.connect(self._show_channels_dialog)
        tools.addWidget(self.btn_sources)
        self.btn_update_range = QPushButton(self._update_range_summary())
        self.btn_update_range.setToolTip("Chọn khoảng ngày video và chu kỳ tự động cập nhật")
        self.btn_update_range.clicked.connect(self._show_update_range_dialog)
        tools.addWidget(self.btn_update_range)
        tools.addWidget(QLabel("URL:"))
        self.le_manual_url = QLineEdit()
        self.le_manual_url.setPlaceholderText("Dán URL video YouTube (watch?v=… / youtu.be/… / shorts/…)")
        self.le_manual_url.returnPressed.connect(self.on_manual_download)
        tools.addWidget(self.le_manual_url, 1)
        self.btn_manual = QPushButton("Tải video")
        self.btn_manual.clicked.connect(self.on_manual_download)
        tools.addWidget(self.btn_manual)
        lay.addLayout(tools)

        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)
        return w

    def _update_range_summary(self) -> str:
        days = int(getattr(self.cfg.download, "lookback_days", 1))
        days = days if days in {1, 7, 30, 60} else 1
        return f"Thời gian: {days} ngày gần đây"

    def _show_update_range_dialog(self) -> None:
        dlg = QDialog(self); dlg.setWindowTitle("Phạm vi cập nhật video"); dlg.resize(460, 215)
        lay = QVBoxLayout(dlg)
        hint = QLabel("Chọn khoảng thời gian video gần đây cần kiểm tra trên mỗi kênh.")
        hint.setWordWrap(True); lay.addWidget(hint)
        form = QFormLayout()
        mode = QComboBox()
        for label, value in (("1 ngày gần đây", 1), ("7 ngày gần đây", 7),
                             ("30 ngày gần đây", 30), ("60 ngày gần đây", 60)):
            mode.addItem(label, value)
        current = int(getattr(self.cfg.download, "lookback_days", 1))
        current = current if current in {1, 7, 30, 60} else 1
        mode.setCurrentIndex(max(0, mode.findData(current))); form.addRow("Lấy video trong", mode)
        interval = QSpinBox(); interval.setRange(5, 1440); interval.setSuffix(" phút")
        interval.setValue(self.cfg.download.scan_interval_minutes); form.addRow("Tự động kiểm tra mỗi", interval)
        limit = QSpinBox(); limit.setRange(15, 5000); limit.setSingleStep(50)
        limit.setValue(getattr(self.cfg.download, "history_limit", 500)); form.addRow("Tối đa mỗi kênh", limit)
        lay.addLayout(form)
        note = QLabel("Khoảng 30–60 ngày có thể quét chậm hơn vì cần đọc lịch sử kênh.")
        note.setStyleSheet("color:#64748b;"); note.setWordWrap(True); lay.addWidget(note)
        buttons = QHBoxLayout(); buttons.addStretch(1)
        cancel = QPushButton("Hủy"); save = QPushButton("Lưu và áp dụng")
        cancel.clicked.connect(dlg.reject); save.clicked.connect(dlg.accept)
        buttons.addWidget(cancel); buttons.addWidget(save); lay.addLayout(buttons)
        if not dlg.exec():
            return
        dcfg = self.cfg.download
        dcfg.lookback_days = int(mode.currentData())
        dcfg.history_scan = dcfg.lookback_days > 1
        dcfg.since = ""; dcfg.until = ""
        dcfg.scan_interval_minutes = interval.value(); dcfg.history_limit = limit.value()
        if self.chk_auto.isChecked(): self._auto.stop_scheduler()
        self._save_cfg()
        if self.chk_auto.isChecked(): self._auto.start_scheduler()
        self.chk_auto.setText(f"Tự động quét mỗi {dcfg.scan_interval_minutes} phút")
        self.btn_update_range.setText(self._update_range_summary())
        self._log_dl(f"Đã cập nhật phạm vi video: {self.btn_update_range.text()}.")

    def _show_channels_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Quản lý nguồn theo dõi")
        # Đây là màn hình quản lý danh sách: ưu tiên chiều cao cho bảng, không co cửa sổ
        # theo số dòng hiện có vì sẽ khiến người dùng chỉ nhìn được 4–5 nguồn.
        available_w = max(820, self.width() - 120)
        available_h = max(560, self.height() - 90)
        dlg.resize(min(1050, available_w), min(680, available_h))
        dlg.setMinimumSize(780, 520)
        box = QVBoxLayout(dlg)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(10)
        title = QLabel("Nguồn YouTube được quét tự động")
        title.setStyleSheet("font-size:15px;font-weight:600;")
        hint = QLabel(
            "Thêm URL kênh, @handle hoặc channel ID (UC…). Chọn một dòng để kiểm tra, "
            "sửa hoặc xóa nguồn.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#64748b;")
        box.addWidget(title); box.addWidget(hint)
        box.addWidget(self._build_channels_group(), 1)
        close = QPushButton("Đóng")
        close.clicked.connect(dlg.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(close)
        box.addLayout(row)
        self._install_wheel_guards(dlg)
        dlg.exec()

    def _install_wheel_guards(self, root: QWidget) -> None:
        """Wheel cuộn trang, không vô tình đổi combo/spinbox dưới con trỏ."""
        for cls in (QComboBox, QAbstractSpinBox):
            for widget in root.findChildren(cls):
                widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Wheel and
                isinstance(watched, (QComboBox, QAbstractSpinBox))):
            parent = watched.parentWidget()
            while parent is not None and not isinstance(parent, QScrollArea):
                parent = parent.parentWidget()
            if isinstance(parent, QScrollArea):
                bar = parent.verticalScrollBar()
                bar.setValue(bar.value() - event.angleDelta().y())
            return True
        return super().eventFilter(watched, event)

    def _channels_summary(self) -> str:
        count = len(self.cfg.download.channels)
        ck = self.cfg.download.cookies_from_browser or "không dùng cookie"
        return f"{count} nguồn theo dõi · Xác thực: {ck}"

    def _on_quality_changed(self, _index: int = 0) -> None:
        height = int(self.cmb_quality.currentData() or 0)
        self.cfg.download.quality_height = height
        self.cfg.download.format = quality_format(height)
        self._save_cfg()
        label = self.cmb_quality.currentText()
        self._log_dl(f"Chất lượng tải: {label}; tự hạ xuống mức gần nhất nếu không có.")

    def _build_channels_group(self) -> QWidget:
        g = QGroupBox(f"Danh sách nguồn ({len(self.cfg.download.channels)})")
        self.grp_channels = g
        lay = QVBoxLayout(g)
        lay.setContentsMargins(10, 12, 10, 10)
        lay.setSpacing(9)
        self.tbl_channels = QTableWidget(0, 3)
        self.tbl_channels.setHorizontalHeaderLabels(["Tên hiển thị", "Địa chỉ kênh", "Trạng thái"])
        self.tbl_channels.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_channels.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_channels.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_channels.setAlternatingRowColors(True)
        self.tbl_channels.verticalHeader().setVisible(False)
        self.tbl_channels.verticalHeader().setDefaultSectionSize(27)
        header = self.tbl_channels.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        # Khoảng 10 dòng vẫn nhìn thấy ngay cả khi form nhập ở dưới đang hiển thị.
        self.tbl_channels.setMinimumHeight(285)
        self.tbl_channels.itemSelectionChanged.connect(self._on_channel_selected)
        lay.addWidget(self.tbl_channels, 1)

        form = QFormLayout()
        form.setContentsMargins(0, 2, 0, 0)
        form.setHorizontalSpacing(12); form.setVerticalSpacing(7)
        self.le_ch_name = QLineEdit(); self.le_ch_name.setPlaceholderText("Tên kênh (hiển thị)")
        self.le_ch_url = QLineEdit()
        self.le_ch_url.setPlaceholderText("https://youtube.com/@kenh, @handle hoặc UC...")
        form.addRow("Tên nguồn", self.le_ch_name)
        form.addRow("Địa chỉ YouTube", self.le_ch_url)
        lay.addLayout(form)

        row = QHBoxLayout(); row.setSpacing(8)
        btn_add = QPushButton("+ Thêm nguồn"); btn_add.clicked.connect(self.on_add_channel)
        self.btn_update_channel = QPushButton("Lưu thay đổi")
        self.btn_update_channel.clicked.connect(self.on_update_channel)
        self.btn_check = QPushButton("Kiểm tra kênh")
        self.btn_check.clicked.connect(self.on_check_channel)
        self.btn_delete_channel = QPushButton("Xóa nguồn")
        self.btn_delete_channel.clicked.connect(self.on_remove_channel)
        row.addWidget(btn_add); row.addWidget(self.btn_update_channel)
        row.addWidget(self.btn_check); row.addStretch(1); row.addWidget(self.btn_delete_channel)
        lay.addLayout(row)
        self._reload_channel_list()
        self._update_channel_actions()
        return g

    def _reload_channel_list(self) -> None:
        self.tbl_channels.setRowCount(0)
        for row, c in enumerate(self.cfg.download.channels):
            ref = c.channel_id or c.url or "(chưa có URL/ID)"
            self.tbl_channels.insertRow(row)
            self.tbl_channels.setItem(row, 0, QTableWidgetItem(c.name or "(không tên)"))
            self.tbl_channels.setItem(row, 1, QTableWidgetItem(ref))
            status = "Chưa kiểm tra" if ref != "(chưa có URL/ID)" else "Thiếu địa chỉ"
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor("#64748b" if status == "Chưa kiểm tra" else "#dc2626"))
            self.tbl_channels.setItem(row, 2, status_item)
        self.tbl_channels.clearSelection()
        if hasattr(self, "grp_channels"):
            self.grp_channels.setTitle(f"Danh sách nguồn ({len(self.cfg.download.channels)})")
        self._update_channel_actions()

    def _selected_channel_index(self) -> int:
        rows = self.tbl_channels.selectionModel().selectedRows() if self.tbl_channels else []
        return rows[0].row() if rows else -1

    def _update_channel_actions(self) -> None:
        selected = hasattr(self, "tbl_channels") and self._selected_channel_index() >= 0
        if hasattr(self, "btn_update_channel"):
            self.btn_update_channel.setEnabled(selected)
            self.btn_delete_channel.setEnabled(selected)

    def _on_channel_selected(self) -> None:
        idx = self._selected_channel_index()
        if 0 <= idx < len(self.cfg.download.channels):
            ch = self.cfg.download.channels[idx]
            self.le_ch_name.setText(ch.name)
            self.le_ch_url.setText(ch.channel_id or ch.url)
        self._update_channel_actions()

    @staticmethod
    def _valid_channel_ref(ref: str) -> bool:
        low = ref.lower().strip()
        return ((ref.startswith("UC") and len(ref) == 24 and " " not in ref) or
                (ref.startswith("@") and len(ref) > 1 and " " not in ref) or
                (low.startswith(("http://", "https://")) and
                 ("youtube.com/" in low or "youtu.be/" in low)))

    def _channel_from_form(self) -> tuple[str, str] | None:
        name = self.le_ch_name.text().strip()
        ref = self.le_ch_url.text().strip()
        if not ref:
            QMessageBox.information(self, "Thiếu địa chỉ", "Nhập URL, @handle hoặc channel ID của kênh.")
            return None
        if not self._valid_channel_ref(ref):
            QMessageBox.warning(self, "Địa chỉ chưa hợp lệ",
                                "Địa chỉ phải là URL YouTube, @handle hoặc channel ID bắt đầu bằng UC.")
            return None
        return name or ref, ref

    def on_add_channel(self) -> None:
        values = self._channel_from_form()
        if not values: return
        name, ref = values
        refs = {c.channel_id or c.url for c in self.cfg.download.channels}
        if ref in refs:
            QMessageBox.information(self, "Nguồn đã tồn tại", "Địa chỉ này đã có trong danh sách theo dõi.")
            return
        # channel_id nếu người dùng dán thẳng UC...; ngược lại coi là url/handle để resolve khi quét
        is_id = ref.startswith("UC") and len(ref) == 24 and " " not in ref
        ch = ChannelCfg(name=name or ref, url="" if is_id else ref,
                        channel_id=ref if is_id else "")
        self.cfg.download.channels.append(ch)
        self.le_ch_name.clear(); self.le_ch_url.clear()
        self._reload_channel_list()
        self.lbl_channels.setText(self._channels_summary())
        self.btn_sources.setText(f"Nguồn ({len(self.cfg.download.channels)})")
        self._save_cfg()
        self._log_dl(f"Đã thêm kênh: {ch.name} ({ref}). Bấm 'Quét ngay' để tải video mới.")

    def on_update_channel(self) -> None:
        idx = self._selected_channel_index()
        if not (0 <= idx < len(self.cfg.download.channels)): return
        values = self._channel_from_form()
        if not values: return
        name, ref = values
        duplicate = any(i != idx and (c.channel_id or c.url) == ref
                        for i, c in enumerate(self.cfg.download.channels))
        if duplicate:
            QMessageBox.information(self, "Nguồn đã tồn tại", "Địa chỉ này đã có trong danh sách theo dõi.")
            return
        is_id = ref.startswith("UC") and len(ref) == 24 and " " not in ref
        self.cfg.download.channels[idx] = ChannelCfg(
            name=name, url="" if is_id else ref, channel_id=ref if is_id else "")
        self._save_cfg(); self._reload_channel_list()
        self._log_dl(f"Đã cập nhật nguồn: {name} ({ref}).")

    def on_remove_channel(self) -> None:
        idx = self._selected_channel_index()
        chans = self.cfg.download.channels
        if idx < 0 or idx >= len(chans):
            return
        if QMessageBox.question(self, "Xóa nguồn theo dõi",
                                f"Xóa ‘{chans[idx].name}’ khỏi danh sách theo dõi?") != QMessageBox.Yes:
            return
        removed = chans.pop(idx)
        self._reload_channel_list()
        self.lbl_channels.setText(self._channels_summary())
        self.btn_sources.setText(f"Nguồn ({len(self.cfg.download.channels)})")
        self._save_cfg()
        self._log_dl(f"Đã xóa kênh: {removed.name}")

    def on_check_channel(self) -> None:
        """Resolve channel_id + đọc RSS đếm video để xác nhận kênh hợp lệ trước khi quét."""
        ref = self.le_ch_url.text().strip()
        if not ref:   # không nhập -> dùng kênh đang chọn trong danh sách
            idx = self._selected_channel_index()
            chans = self.cfg.download.channels
            if 0 <= idx < len(chans):
                ref = chans[idx].channel_id or chans[idx].url
        if not ref:
            QMessageBox.information(self, "Kiểm tra kênh",
                                    "Nhập URL/@handle/ID vào ô, hoặc chọn 1 kênh trong danh sách.")
            return
        self._checking_channel_ref = ref
        self.btn_check.setEnabled(False)
        idx = self._selected_channel_index()
        if 0 <= idx < self.tbl_channels.rowCount():
            self.tbl_channels.item(idx, 2).setText("Đang kiểm tra…")
            self.tbl_channels.item(idx, 2).setForeground(QColor("#2563eb"))
        self._log_dl(f"Đang kiểm tra kênh: {ref}…")
        self._check_worker = CheckChannelWorker(ref)
        self._check_worker.done.connect(self._on_check_done)
        self._check_worker.start()

    def _on_check_done(self, r: dict) -> None:
        self.btn_check.setEnabled(True)
        checked_ref = getattr(self, "_checking_channel_ref", r.get("ref", ""))
        matched_row = -1
        for row, ch in enumerate(self.cfg.download.channels):
            if checked_ref in (ch.url, ch.channel_id):
                matched_row = row
                break
        if r.get("ok"):
            if matched_row >= 0:
                ch = self.cfg.download.channels[matched_row]
                ch.channel_id = r["channel_id"]
                self._save_cfg()
                item = self.tbl_channels.item(matched_row, 2)
                item.setText(f"Hợp lệ · {r['count']} video")
                item.setForeground(QColor("#15803d"))
            msg = (f"✔ Kênh hợp lệ.\nchannel_id: {r['channel_id']}\n"
                   f"RSS có {r['count']} video gần đây.")
            if r.get("title"):
                msg += f"\nMới nhất: {r['title']}"
            self._log_dl(f"✔ {r['ref']} -> {r['channel_id']} ({r['count']} video RSS)")
            QMessageBox.information(self, "Kiểm tra kênh", msg)
        else:
            if matched_row >= 0:
                item = self.tbl_channels.item(matched_row, 2)
                item.setText("Không hợp lệ")
                item.setForeground(QColor("#dc2626"))
            self._log_dl(f"✘ Kênh lỗi ({r.get('ref')}): {r.get('error')}")
            QMessageBox.warning(self, "Kiểm tra kênh",
                                f"Không kiểm tra được kênh:\n{r.get('error')}")

    # ---------------- Tab Biên tập ----------------
    def _edit_summary(self) -> str:
        e = self.cfg.editor
        focus = ""
        if e.crop_mode == "manual":
            focus = f" (focus {e.manual_focus_x:.2f},{e.manual_focus_y:.2f})"
        return (
            f"Khung: {e.target_aspect} | crop: {e.crop_mode}{focus} | lấp thiếu: {e.fill_missing} "
            f"| zoom {e.zoom_fill_percent}%\n"
            f"Flip: {e.flip_horizontal} | Mirror: {e.mirror_crop} | Speed: {e.speed}\n"
            f"Tách giọng: {e.audio.separate_speech} | Thay nhạc: {bool(e.audio.replace_music)} "
            f"| Voiceover: {bool(e.audio.voiceover)} | Pitch: {e.audio.pitch_shift_semitones} "
            f"| Audio speed: {e.audio.audio_speed}\n"
            f"Xuất: full={e.export.make_full}, short={e.export.short_seconds}s={e.export.make_short}, "
            f"txt={e.export.make_content_txt} | codec={e.export.video_codec}\n"
            f"Phụ đề: {e.subtitle.enabled}"
            + (f" (dịch {e.subtitle.translate_to or 'gốc'}, {e.subtitle.position}, cỡ {e.subtitle.font_size})"
               if e.subtitle.enabled else "")
            + f"\nThư mục xuất: {e.output_dir}"
        )

    def _build_edit_tab(self) -> QWidget:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        # Giữ label ẩn để các callback cũ tiếp tục tương thích.
        self.lbl_edit_summary = QLabel(self._edit_summary()); self.lbl_edit_summary.hide()

        # Header cố định: preset, output và preview luôn nhìn thấy.
        header = QHBoxLayout(); header.setSpacing(6)
        header.addWidget(QLabel("<b>Cài đặt biên tập</b>"))
        header.addWidget(QLabel("Preset:"))
        self.cmb_preset = QComboBox()
        for name, p in PLATFORM_PRESETS.items():
            self.cmb_preset.addItem(p["label"], name)
        header.addWidget(self.cmb_preset)
        btn_preset = QPushButton("Áp dụng")
        btn_preset.clicked.connect(self.on_apply_preset)
        header.addWidget(btn_preset)
        header.addWidget(QLabel("Xuất:"))
        self.lbl_output = QLabel(self.cfg.editor.output_dir)
        self.lbl_output.setMaximumWidth(260)
        self.lbl_output.setToolTip(self.cfg.editor.output_dir)
        header.addWidget(self.lbl_output)
        btn_out = QPushButton("Đổi")
        btn_out.clicked.connect(self.on_pick_output_dir)
        header.addWidget(btn_out)
        header.addStretch(1)
        self.lbl_edit_save_status = QLabel("✓ Đã lưu")
        self.lbl_edit_save_status.setStyleSheet("color:#15803d;")
        header.addWidget(self.lbl_edit_save_status)
        btn_prev = QPushButton("Xem trước")
        btn_prev.clicked.connect(lambda: self.on_preview(batch=False))
        header.addWidget(btn_prev)
        lay.addLayout(header)

        # Trang Cơ bản.
        basic = QGroupBox("Thiết lập đầu ra")
        bform = QFormLayout(basic)
        self.cmb_aspect = QComboBox(); self.cmb_aspect.addItems(self._ASPECTS)
        if self.cfg.editor.target_aspect in self._ASPECTS:
            self.cmb_aspect.setCurrentText(self.cfg.editor.target_aspect)
        self.cmb_aspect.currentTextChanged.connect(self._on_aspect_changed)
        bform.addRow("Tỉ lệ khung hình", self.cmb_aspect)
        self.cmb_codec = QComboBox()
        for val, label in self._CODECS:
            self.cmb_codec.addItem(label, val)
        cur = (self.cfg.editor.export.video_codec or "").strip()
        found = next((i for i, (v, _) in enumerate(self._CODECS) if v == cur), None)
        if found is None and cur:
            self.cmb_codec.addItem(f"{cur} (tùy chỉnh)", cur)
            self.cmb_codec.setCurrentIndex(self.cmb_codec.count() - 1)
        else:
            self.cmb_codec.setCurrentIndex(found or 0)
        self.cmb_codec.currentIndexChanged.connect(self._on_codec_changed)
        bform.addRow("Codec video", self.cmb_codec)
        self.sp_encode_quality = self._spin(
            QSpinBox, 14, 30, int(getattr(self.cfg.editor.export, "crf_or_cq", 20)))
        self.sp_encode_quality.setToolTip(
            "Số càng thấp càng nét và file càng lớn. Khuyên dùng 18–20.")
        self._bind_int(self.sp_encode_quality, self.cfg.editor.export, "crf_or_cq", "Chất lượng mã hóa")
        bform.addRow("Chất lượng CRF/CQ", self.sp_encode_quality)
        self.cmb_encode_preset = self._data_combo([
            ("medium", "Medium — nhanh"), ("slow", "Slow — chất lượng/dung lượng tốt"),
            ("slower", "Slower — rất chậm"),
        ], getattr(self.cfg.editor.export, "encoder_preset", "slow"))
        self._bind_combo(self.cmb_encode_preset, self.cfg.editor.export,
                         "encoder_preset", "Preset mã hóa")
        bform.addRow("Preset mã hóa", self.cmb_encode_preset)
        self.cmb_output_resolution = self._data_combo([
            (0, "Tự động theo nguồn (khuyên dùng)"),
            (720, "HD — cạnh ngắn 720"), (1080, "Full HD — cạnh ngắn 1080"),
            (1440, "2K — cạnh ngắn 1440"), (2160, "4K — cạnh ngắn 2160"),
        ], int(getattr(self.cfg.editor.export, "output_short_edge", 0)))
        self._bind_combo(self.cmb_output_resolution, self.cfg.editor.export,
                         "output_short_edge", "Độ phân giải xuất")
        bform.addRow("Độ phân giải", self.cmb_output_resolution)
        self.cmb_ai_device = self._data_combo([
            ("auto", "Tự động — ưu tiên GPU, lỗi thì dùng CPU"),
            ("cuda", "GPU NVIDIA (CUDA)"),
            ("cpu", "CPU — tương thích mọi máy"),
        ], getattr(self.cfg.editor, "processing_device", "auto"))
        self.cmb_ai_device.setToolTip(
            "Dùng cho nhận diện lời nói và Demucs; độc lập với codec video.")
        self.cmb_ai_device.currentIndexChanged.connect(self._on_ai_device_changed)
        bform.addRow("Thiết bị xử lý AI", self.cmb_ai_device)
        hint = QLabel("Preset thiết lập nhanh tỉ lệ, thời lượng short và vùng an toàn phụ đề.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#64748b;")
        bform.addRow("", hint)

        nav = QListWidget(); nav.setMaximumWidth(175)
        nav.addItems(["Cơ bản", "Hình ảnh", "Âm thanh", "Phụ đề & dịch", "Thương hiệu"])
        nav.setCurrentRow(0)
        stack = QStackedWidget()

        def add_page(widget):
            holder = QWidget(); vl = QVBoxLayout(holder); vl.setContentsMargins(8, 4, 8, 4)
            vl.addWidget(widget); vl.addStretch(1)
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(holder)
            scroll.setFrameShape(QScrollArea.NoFrame)
            stack.addWidget(scroll)

        add_page(basic)
        add_page(self._build_video_settings_group())
        add_page(self._build_audio_settings_group())
        add_page(self._build_subtitle_group())
        add_page(self._build_hook_cta_logo_group())
        nav.currentRowChanged.connect(stack.setCurrentIndex)
        preview_box = QGroupBox("Xem trước")
        pv = QVBoxLayout(preview_box)
        self.lbl_live_preview = _InteractivePreview(
            "Chọn “Xem trước” để tạo khung hình\n\nSau đó chọn Logo/Hook/CTA và click\ntrực tiếp lên ảnh để đặt vị trí")
        self.lbl_live_preview.clicked.connect(self._on_inline_position_click)
        self.lbl_live_preview.setMinimumSize(340, 390)
        self.lbl_live_preview.setStyleSheet(
            "background:#0f172a;color:#cbd5e1;border-radius:6px;padding:12px;")
        pv.addWidget(self.lbl_live_preview, 1)
        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("Đặt vị trí:"))
        self.cmb_preview_target = QComboBox()
        self.cmb_preview_target.addItem("Logo", "logo")
        self.cmb_preview_target.addItem("Hook", "hook")
        self.cmb_preview_target.addItem("CTA", "cta")
        self.cmb_preview_target.currentIndexChanged.connect(self._on_preview_target_changed)
        position_row.addWidget(self.cmb_preview_target)
        self.lbl_preview_position = QLabel("Click lên ảnh")
        self.lbl_preview_position.setStyleSheet("color:#64748b;")
        position_row.addWidget(self.lbl_preview_position, 1)
        pv.addLayout(position_row)
        ebrand = self.cfg.editor
        for key, pos in (("logo", ebrand.overlay.position),
                         ("hook", ebrand.intro_hook.position),
                         ("cta", ebrand.outro_cta.position)):
            fx, fy = self._position_point(key, pos)
            self.lbl_live_preview.set_marker(key, fx, fy)
        self.lbl_live_preview.set_active("logo")
        self._on_preview_target_changed()
        pv_actions = QHBoxLayout()
        btn_prevb = QPushButton("Xem nhiều video")
        btn_prevb.clicked.connect(lambda: self.on_preview(batch=True))
        btn_refresh = QPushButton("Cập nhật preview")
        btn_refresh.clicked.connect(lambda: self.on_preview(batch=False))
        pv_actions.addWidget(btn_prevb); pv_actions.addWidget(btn_refresh)
        pv.addLayout(pv_actions)
        preview_box.setMinimumWidth(380)
        preview_box.setMaximumWidth(460)
        content = QHBoxLayout()
        content.addWidget(nav); content.addWidget(stack, 1); content.addWidget(preview_box)
        lay.addLayout(content, 1)

        # Footer cố định: nguồn đầu vào và hành động chạy.
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Video đầu vào:"))
        self.lbl_input = QLabel(self.cfg.editor.input_folder or "(chưa chọn)")
        self.lbl_input.setWordWrap(True)
        frow.addWidget(self.lbl_input, 1)
        btn_pick = QPushButton("Chọn folder…")
        btn_pick.clicked.connect(self.on_pick_input_folder)
        frow.addWidget(btn_pick)
        self.btn_edit_folder = QPushButton("Nhập folder vào hàng đợi")
        self.btn_edit_folder.clicked.connect(self.on_edit_folder)
        frow.addWidget(self.btn_edit_folder)
        lay.addLayout(frow)

        self.btn_edit = QPushButton("Thêm video đã tải vào hàng đợi")
        self.btn_edit.clicked.connect(self.on_edit_clicked)
        self.btn_edit.setStyleSheet("font-weight:600;")
        frow.addWidget(self.btn_edit)
        lay.addLayout(frow)
        return root

    # ---------------- Tab Lịch sử / Xuất bản ----------------
    _EXPORT_HDR = ["Video", "Kênh", "Video dài", "Video ngắn", "Nội dung", "Phụ đề", "Thư mục", "Thời gian"]
    _EVENT_HDR = ["Thời gian", "Mức độ", "Nguồn", "Nội dung"]

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("""
            QTableWidget { border:1px solid #e2e8f0; border-radius:6px; background:#fff;
                alternate-background-color:#fafbfc; selection-background-color:#e8f1ff;
                selection-color:#0f172a; }
            QTableWidget::item { border-bottom:1px solid #eef2f7; padding:5px; }
            QTableWidget::item:hover { background:#f1f5f9; color:#0f172a; }
            QHeaderView::section { background:#f8fafc; color:#334155; border:0;
                border-right:1px solid #e2e8f0; border-bottom:1px solid #dbe3ee;
                padding:7px; font-weight:600; }
            QPushButton { min-height:25px; padding:2px 11px; background:#fff; color:#334155;
                border:1px solid #d8e0ea; border-radius:5px; }
            QPushButton:hover { background:#f1f5f9; color:#1e40af; border-color:#a9bdd8; }
            QLineEdit, QComboBox { min-height:25px; border:1px solid #d8e0ea;
                border-radius:5px; padding:1px 7px; background:#fff; }
        """)
        lay = QVBoxLayout(w); lay.setContentsMargins(12, 10, 12, 10); lay.setSpacing(8)
        bar = QHBoxLayout()
        heading = QLabel("Lịch sử và kết quả")
        heading.setStyleSheet("font-size:16px;font-weight:600;color:#0f172a;")
        bar.addWidget(heading)
        self.lbl_history_summary = QLabel()
        self.lbl_history_summary.setStyleSheet("color:#64748b;")
        bar.addWidget(self.lbl_history_summary)
        bar.addStretch(1)
        btn = QPushButton("Làm mới")
        btn.clicked.connect(self._reload_history)
        bar.addWidget(btn)
        btn_report = QPushButton("Xuất báo cáo")
        btn_report.clicked.connect(self.on_export_report)
        bar.addWidget(btn_report)
        btn_clear = QPushButton("Xóa lịch sử")
        btn_clear.setStyleSheet("color:#b91c1c;")
        btn_clear.clicked.connect(self._clear_history)
        bar.addWidget(btn_clear)
        lay.addLayout(bar)

        self.history_pages = QTabWidget()
        exports_page = QWidget(); exports_lay = QVBoxLayout(exports_page)
        exports_lay.setContentsMargins(0, 8, 0, 0)
        exports_hint = QLabel("Các video đã xuất thành công. Bấm đúp một dòng để mở thư mục kết quả.")
        exports_hint.setStyleSheet("color:#64748b;")
        exports_lay.addWidget(exports_hint)
        self.tbl_exports = QTableWidget(0, len(self._EXPORT_HDR))
        self.tbl_exports.setHorizontalHeaderLabels(self._EXPORT_HDR)
        self.tbl_exports.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_exports.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_exports.setAlternatingRowColors(True); self.tbl_exports.setShowGrid(False)
        self.tbl_exports.cellDoubleClicked.connect(self._open_export_dir)
        self.tbl_exports.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_exports.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.lbl_exports_empty = QLabel("Chưa có video nào được xuất bản\nKết quả sẽ xuất hiện tại đây sau khi hoàn thành biên tập.")
        self.lbl_exports_empty.setAlignment(Qt.AlignCenter)
        self.lbl_exports_empty.setStyleSheet("color:#64748b;background:#f8fafc;border:1px dashed #cbd5e1;border-radius:7px;padding:30px;")
        exports_lay.addWidget(self.lbl_exports_empty, 1)
        exports_lay.addWidget(self.tbl_exports, 1)
        self.history_pages.addTab(exports_page, "Kết quả xuất bản")

        events_page = QWidget(); events_lay = QVBoxLayout(events_page)
        events_lay.setContentsMargins(0, 8, 0, 0)
        filters = QHBoxLayout()
        self.history_search = QLineEdit(); self.history_search.setPlaceholderText("Tìm trong nội dung…")
        self.history_search.setClearButtonEnabled(True); self.history_search.setMaximumWidth(330)
        self.history_search.textChanged.connect(self._apply_event_filters); filters.addWidget(self.history_search)
        self.history_level = QComboBox()
        self.history_level.addItem("Tất cả mức độ", "all")
        self.history_level.addItem("Chỉ lỗi", "ERROR"); self.history_level.addItem("Thông tin", "INFO")
        self.history_level.currentIndexChanged.connect(self._apply_event_filters); filters.addWidget(self.history_level)
        self.history_source = QComboBox(); self.history_source.addItem("Tất cả nguồn", "all")
        self.history_source.currentIndexChanged.connect(self._apply_event_filters); filters.addWidget(self.history_source)
        filters.addStretch(1)
        self.lbl_event_count = QLabel(); self.lbl_event_count.setStyleSheet("color:#64748b;")
        filters.addWidget(self.lbl_event_count); events_lay.addLayout(filters)
        self.tbl_events = QTableWidget(0, len(self._EVENT_HDR))
        self.tbl_events.setHorizontalHeaderLabels(self._EVENT_HDR)
        self.tbl_events.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_events.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_events.setAlternatingRowColors(True); self.tbl_events.setShowGrid(False)
        self.tbl_events.verticalHeader().hide()
        self.tbl_events.cellDoubleClicked.connect(self._show_event_detail)
        self.tbl_events.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_events.horizontalHeader().resizeSection(0, 155)
        self.tbl_events.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.tbl_events.horizontalHeader().resizeSection(1, 90)
        self.tbl_events.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_events.horizontalHeader().resizeSection(2, 115)
        self.tbl_events.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        events_lay.addWidget(self.tbl_events, 1)
        self.history_pages.addTab(events_page, "Nhật ký hoạt động")
        lay.addWidget(self.history_pages, 1)
        self._history_events = []
        return w

    @staticmethod
    def _fill_table(tbl: QTableWidget, rows: list[dict], keys: list[str]) -> None:
        tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, k in enumerate(keys):
                v = r.get(k)
                tbl.setItem(i, j, QTableWidgetItem("" if v is None else str(v)))

    def _reload_history(self) -> None:
        exports = list(reversed(self.db.recent_exports()))
        self._fill_exports(exports)
        self._history_events = list(reversed(self.db.recent_events()))
        current_source = self.history_source.currentData()
        sources = sorted({str(e.get("source") or "khác") for e in self._history_events})
        self.history_source.blockSignals(True); self.history_source.clear()
        self.history_source.addItem("Tất cả nguồn", "all")
        for source in sources: self.history_source.addItem(source.capitalize(), source)
        idx = self.history_source.findData(current_source)
        self.history_source.setCurrentIndex(max(0, idx)); self.history_source.blockSignals(False)
        self._apply_event_filters()
        errors = sum(str(e.get("level", "")).upper() == "ERROR" for e in self._history_events)
        self.lbl_history_summary.setText(f"{len(exports)} kết quả · {errors} lỗi")
        if not exports and self._history_events and self.history_pages.currentIndex() == 0:
            self.history_pages.setCurrentIndex(1)

    def _fill_exports(self, rows: list[dict]) -> None:
        self.tbl_exports.setRowCount(len(rows))
        keys = ["video_id", "channel_name", "full_path", "short_path", "content_txt",
                "srt_path", "output_dir", "exported_at"]
        for row, data in enumerate(rows):
            for col, key in enumerate(keys):
                raw = str(data.get(key) or "")
                shown = ("Có" if raw else "—") if key in {"full_path", "short_path", "content_txt", "srt_path"} else raw
                item = QTableWidgetItem(shown); item.setToolTip(raw)
                if key == "output_dir": item.setText("Mở thư mục")
                self.tbl_exports.setItem(row, col, item)
        self.lbl_exports_empty.setVisible(not rows)
        self.tbl_exports.setVisible(bool(rows))

    def _apply_event_filters(self, *_):
        if not hasattr(self, "_history_events"): return
        query = self.history_search.text().strip().lower()
        level = self.history_level.currentData(); source = self.history_source.currentData()
        rows = [e for e in self._history_events
                if (level == "all" or str(e.get("level", "")).upper() == level)
                and (source == "all" or str(e.get("source") or "khác") == source)
                and (not query or query in str(e.get("message", "")).lower())]
        self._fill_table(self.tbl_events, rows, ["time", "level", "source", "message"])
        for row, event in enumerate(rows):
            is_error = str(event.get("level", "")).upper() == "ERROR"
            self.tbl_events.item(row, 1).setText("Lỗi" if is_error else "Thông tin")
            if is_error:
                for col in range(self.tbl_events.columnCount()):
                    self.tbl_events.item(row, col).setBackground(QColor("#fef2f2"))
                    self.tbl_events.item(row, col).setForeground(QColor("#991b1b"))
            for col in range(self.tbl_events.columnCount()):
                self.tbl_events.item(row, col).setToolTip(str(event.get("message", "")))
        self.lbl_event_count.setText(f"Hiển thị {len(rows)}/{len(self._history_events)} sự kiện")

    def _show_event_detail(self, row: int, _col: int) -> None:
        level = self.tbl_events.item(row, 1).text() if self.tbl_events.item(row, 1) else ""
        source = self.tbl_events.item(row, 2).text() if self.tbl_events.item(row, 2) else ""
        message = self.tbl_events.item(row, 3).text() if self.tbl_events.item(row, 3) else ""
        QMessageBox.information(self, f"{level} · {source}", message)

    def _on_tab_changed(self, idx: int) -> None:
        if idx == self._history_idx:
            self._reload_history()

    def _clear_history(self) -> None:
        r = QMessageBox.question(
            self, "Xóa dữ liệu lịch sử",
            "Xóa toàn bộ kết quả xuất bản và nhật ký hoạt động khỏi danh sách?\n\n"
            "Các file video đã tải và đã xuất vẫn được giữ nguyên.",
        )
        if r == QMessageBox.Yes:
            self.db.clear_history()
            self._reload_history()
            self._log_ed("Đã xoá lịch sử log.")

    def _open_export_dir(self, row: int, col: int) -> None:
        """Bấm đúp 1 dòng -> mở thư mục xuất bản (<kênh>/<id>) trong Explorer."""
        item = self.tbl_exports.item(row, 6)  # cột "Thư mục"
        path = (item.toolTip() or item.text()) if item else ""
        if path and os.path.isdir(path):
            try:
                os.startfile(path)  # Windows
            except Exception as e:
                QMessageBox.warning(self, "Không mở được thư mục", str(e))
        else:
            QMessageBox.information(self, "Không thấy thư mục", path or "(trống)")

    # ---------------- Hành động ----------------
    def _download_space_ok(self) -> bool:
        target = Path(self.cfg.download.root_dir or ".")
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            return True
        if free >= 5 * 1024 ** 3:
            return True
        free_gb = free / 1024 ** 3
        answer = QMessageBox.warning(
            self, "Dung lượng sắp hết",
            f"Ổ đĩa lưu video chỉ còn {free_gb:.1f} GB. Tải hàng loạt có thể thất bại "
            "hoặc làm hỏng file đang xử lý.\n\nBạn vẫn muốn tiếp tục?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def on_scan_clicked(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            self._log_dl("Đang quét, chờ hoàn tất…")
            return
        if not self._download_space_ok():
            return
        self.btn_scan.setEnabled(False)
        self.btn_stop_downloads.show()
        self.lbl_download_activity.setText("● Đang quét và tải video…")
        self.lbl_download_activity.setStyleSheet("color:#2563eb;")
        self._scan_worker = ScanWorker(self.cfg, self.db)
        self._scan_worker.log.connect(self._log_dl)
        self._scan_worker.status.connect(self._on_status)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, stats: dict) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("Quét ngay")
        self.btn_stop_downloads.hide(); self.btn_stop_downloads.setEnabled(True)
        failed = stats.get("failed", 0)
        cancelled = stats.get("cancelled", 0)
        stopped = stats.get("stopped", False)
        self.lbl_download_activity.setText(
            "● Đã dừng tải" if stopped else ("● Hoàn tất, có lỗi" if failed else "● Quét hoàn tất"))
        self.lbl_download_activity.setStyleSheet(
            "color:#b45309;" if stopped else ("color:#dc2626;" if failed else "color:#15803d;"))
        self._reload_table()
        if not stats.get("skipped"):
            self._log_dl(f"Xong: mới {stats.get('discovered',0)}, "
                         f"tải {stats.get('downloaded',0)}, lỗi {stats.get('failed',0)}.")

    def on_manual_download(self) -> None:
        """Tải thủ công 1 video theo URL (chạy nền, không đơ UI)."""
        url = self.le_manual_url.text().strip()
        if not url:
            QMessageBox.information(self, "Tải thủ công", "Dán URL video YouTube vào ô.")
            return
        if self._manual_worker and self._manual_worker.isRunning():
            self._manual_worker.cancel()
            self.btn_manual.setText("Đang dừng…"); self.btn_manual.setEnabled(False)
            return
        if not self._download_space_ok():
            return
        self.btn_manual.setEnabled(True); self.btn_manual.setText("Dừng tải")
        self.btn_stop_downloads.show()
        self.lbl_download_activity.setText("● Đang tải video thủ công…")
        self.lbl_download_activity.setStyleSheet("color:#2563eb;")
        self._log_dl(f"Tải thủ công: {url}")
        self._manual_worker = ManualDownloadWorker(self.cfg, self.db, url)
        self._manual_worker.log.connect(self._log_dl)
        self._manual_worker.status.connect(self._on_status)
        self._manual_worker.done.connect(self._on_manual_done)
        self._manual_worker.start()

    def _on_manual_done(self, r: dict) -> None:
        self.btn_manual.setEnabled(True); self.btn_manual.setText("Tải video")
        if not (self._scan_worker and self._scan_worker.isRunning()): self.btn_stop_downloads.hide()
        self.lbl_download_activity.setText(
            "● Đã dừng tải" if r.get("cancelled") else ("● Tải hoàn tất" if r.get("ok") else "● Tải thất bại"))
        self.lbl_download_activity.setStyleSheet(
            "color:#b45309;" if r.get("cancelled") else ("color:#15803d;" if r.get("ok") else "color:#dc2626;"))
        self._reload_table()
        if r.get("ok"):
            self.le_manual_url.clear()
            self._log_dl(f"✔ Tải xong: {r['video_id']} -> {r.get('filepath')}")
            self._queue_tab.refresh()
            if self.cfg.editor.auto_edit_after_download:   # tải xong -> tự chạy hàng đợi
                self._queue_tab.on_start()
        elif r.get("cancelled"):
            self._log_dl(f"‖ Đã dừng tải {r.get('video_id')}; có thể tiếp tục từ dữ liệu tải dở.")
        else:
            self._log_dl(f"✘ Tải lỗi: {r.get('error')}")
            QMessageBox.warning(self, "Tải thủ công", f"Không tải được:\n{r.get('error')}")

    def _download_from_table(self, url: str) -> None:
        self.le_manual_url.setText(url)
        self.on_manual_download()

    def _cancel_download(self, video_id: str) -> None:
        cancelled = False
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.cancel_video(video_id); cancelled = True
        if self._manual_worker and self._manual_worker.isRunning():
            cancelled = self._manual_worker.cancel(video_id) or cancelled
        self._auto.cancel_video(video_id)
        self.db.transition_download_status(video_id, "downloading", "cancelling")
        self._reload_table()
        self._log_dl(f"Đang dừng tải {video_id}…")

    def on_stop_all_downloads(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning(): self._scan_worker.cancel_all()
        if self._manual_worker and self._manual_worker.isRunning(): self._manual_worker.cancel()
        self._auto.cancel_all_downloads()
        self.btn_stop_downloads.setEnabled(False)
        self.lbl_download_activity.setText("● Đang dừng các video…")
        self.lbl_download_activity.setStyleSheet("color:#b45309;")

    def _resume_one_download(self, video_id: str, url: str) -> None:
        if self.db.resume_paused_downloads([video_id]):
            self.le_manual_url.setText(url)
            self.on_manual_download()

    def on_resume_all_downloads(self) -> None:
        count = self.db.resume_paused_downloads()
        if count:
            self._log_dl(f"Tiếp tục {count} video đã tạm dừng.")
            self.on_scan_clicked()

    def _open_download_path(self, path: str) -> None:
        target = str(Path(path).parent) if path else ""
        if target and Path(target).is_dir():
            try:
                os.startfile(target)
            except Exception as e:
                QMessageBox.warning(self, "Không mở được thư mục", str(e))
        else:
            QMessageBox.information(self, "Không thấy thư mục", target or "Đường dẫn trống.")

    def on_reset_stuck(self) -> None:
        """Đưa video kẹt 'downloading'/'processing' về 'pending' để tải/edit lại."""
        self.db.reset_stuck()
        self._reload_table()
        self._log_dl("Đã đặt lại các video kẹt về 'pending' (bấm 'Quét ngay' hoặc tải thủ công).")

    def on_edit_clicked(self) -> None:
        """Chuyển sang tab Hàng đợi và bắt đầu xử lý (Queue Manager là driver edit)."""
        self.tabs.setCurrentIndex(self._queue_idx)
        self._queue_tab.refresh()
        self._queue_tab.on_start()
        self._log_ed("Đã chuyển sang tab 'Hàng đợi' để theo dõi tiến trình.")

    def _on_downloaded(self, video_id: str) -> None:
        """Scheduler nền báo tải xong: cập nhật Hàng đợi; tự Start nếu bật auto."""
        self._queue_tab.refresh()
        if self.cfg.editor.auto_edit_after_download:
            self._queue_tab.on_start()

    # Trình duyệt yt-dlp đọc cookies được; item đầu = không dùng.
    _BROWSERS = ["(không dùng)", "chrome", "edge", "firefox", "brave", "opera",
                 "vivaldi", "chromium"]
    # (giá trị codec ffmpeg, nhãn hiển thị)
    _CODECS = [
        ("h264_nvenc", "h264_nvenc — GPU NVIDIA (H.264)"),
        ("hevc_nvenc", "hevc_nvenc — GPU NVIDIA (H.265/HEVC)"),
        ("libx264", "libx264 — CPU (H.264)"),
        ("libx265", "libx265 — CPU (H.265)"),
    ]
    _ASPECTS = ["9:16", "1:1", "16:9"]

    def _on_aspect_changed(self, text: str) -> None:
        if text not in self._ASPECTS:
            return
        self.cfg.editor.target_aspect = text
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        self._log_ed(f"Khung hình đầu ra: {text} — đã lưu, giữ tới khi đổi lại.")

    def _on_sep_changed(self, on: bool) -> None:
        self.cfg.editor.audio.separate_speech = bool(on)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        msg = "BẬT (cần requirements-ml.txt + GPU để chạy thực tế)" if on else "TẮT"
        self._log_ed(f"Tách giọng: {msg} — đã lưu.")

    def _on_cookies_changed(self, text: str) -> None:
        val = "" if text.startswith("(") else text.strip()
        self.cfg.download.cookies_from_browser = val
        self.lbl_channels.setText(self._channels_summary())
        self._save_cfg()
        self._log_dl(f"Cookies trình duyệt: {val or '(không dùng)'} — đã lưu, giữ tới khi đổi lại.")

    def _on_codec_changed(self, idx: int) -> None:
        val = self.cmb_codec.itemData(idx)
        if not val:
            return
        self.cfg.editor.export.video_codec = val
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        self._log_ed(f"Codec xuất: {val} — đã lưu, giữ tới khi đổi lại.")

    def _on_ai_device_changed(self, idx: int) -> None:
        val = self.cmb_ai_device.itemData(idx) or "auto"
        self.cfg.editor.processing_device = val
        self.device = val
        self._queue_tab.device = val
        # Worker tạo pipeline mới cho từng video; cập nhật này áp dụng từ video kế
        # tiếp (hoặc ngay bước kế tiếp nếu worker chưa tạo pipeline).
        if self._queue_tab._worker and self._queue_tab._worker.isRunning():
            self._queue_tab._worker.device = val
        self._save_cfg()
        labels = {"auto": "Tự động (GPU lỗi sẽ chuyển CPU)",
                  "cuda": "GPU NVIDIA (CUDA)", "cpu": "CPU"}
        self._log_ed(f"Thiết bị xử lý AI: {labels.get(val, val)} — đã lưu.")

    # ---------------- Cài đặt nâng cao ----------------
    def _set_cfg(self, obj, attr: str, value, label: str | None = None) -> None:
        setattr(obj, attr, value)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        if label:
            self._log_ed(f"{label}: {value} — đã lưu, giữ tới khi đổi lại.")

    def _bind_bool(self, w, obj, attr, label=None):
        w.toggled.connect(lambda v: self._set_cfg(obj, attr, bool(v), label))

    def _bind_int(self, w, obj, attr, label=None):
        w.valueChanged.connect(lambda v: self._set_cfg(obj, attr, int(v), label))

    def _bind_float(self, w, obj, attr, label=None):
        w.valueChanged.connect(lambda v: self._set_cfg(obj, attr, float(v), label))

    @staticmethod
    def _spin(cls, lo, hi, val, step=None):
        w = cls()
        w.setRange(lo, hi)
        if step is not None:
            w.setSingleStep(step)
        w.setValue(val)
        return w

    @staticmethod
    def _data_combo(items, current):
        c = QComboBox()
        idx = 0
        for k, (val, label) in enumerate(items):
            c.addItem(label, val)
            if val == current:
                idx = k
        c.setCurrentIndex(idx)
        return c

    def _bind_combo(self, w, obj, attr, label=None):
        w.currentIndexChanged.connect(lambda i: self._set_cfg(obj, attr, w.itemData(i), label))

    _SUB_LANGS = [
        ("", "(giữ ngôn ngữ gốc)"), ("en", "English"), ("vi", "Tiếng Việt"),
        ("ja", "日本語"), ("ko", "한국어"), ("zh-CN", "中文 (giản thể)"),
        ("fr", "Français"), ("es", "Español"), ("de", "Deutsch"),
        ("th", "ไทย"), ("ru", "Русский"),
    ]

    def _build_subtitle_group(self) -> QWidget:
        s = self.cfg.editor.subtitle
        g = QGroupBox("Phụ đề & dịch (lưu ngay vào config)")
        form = QFormLayout(g)

        self.chk_sub = QCheckBox("Thêm phụ đề & xuất .srt (mặc định: ngôn ngữ gốc)")
        self.chk_sub.setChecked(s.enabled)
        self._bind_bool(self.chk_sub, s, "enabled", "Phụ đề")
        form.addRow("Bật phụ đề", self.chk_sub)

        self.cmb_tr = self._data_combo(self._SUB_LANGS, s.translate_to)
        self._bind_combo(self.cmb_tr, s, "translate_to", "Dịch sang")
        form.addRow("Dịch sang (chỉ hiển thị bản dịch)", self.cmb_tr)

        self.chk_burn = QCheckBox("Hiển thị phụ đề lên video")
        self.chk_burn.setChecked(s.burn_in)
        self._bind_bool(self.chk_burn, s, "burn_in", "Hiển thị phụ đề")
        form.addRow("Burn vào video", self.chk_burn)

        self.cmb_subpos = self._data_combo(
            [("bottom", "Dưới"), ("middle", "Giữa"), ("top", "Trên")], s.position)
        self._bind_combo(self.cmb_subpos, s, "position", "Vị trí phụ đề")
        form.addRow("Vị trí dòng phụ đề", self.cmb_subpos)

        self.sp_subsize = self._spin(QSpinBox, 12, 72, s.font_size)
        self._bind_int(self.sp_subsize, s, "font_size", "Cỡ chữ phụ đề")
        form.addRow("Cỡ chữ phụ đề", self.sp_subsize)
        children = (self.cmb_tr, self.chk_burn, self.cmb_subpos, self.sp_subsize)
        for widget in children:
            widget.setEnabled(s.enabled)
        self.chk_sub.toggled.connect(
            lambda on: [widget.setEnabled(on) for widget in children])
        return g

    def _bind_text(self, w, obj, attr, label=None):
        w.editingFinished.connect(lambda: self._set_cfg(obj, attr, w.text(), label))

    def _after_cfg_change(self) -> None:
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()

    def _path_row(self, obj, attr, title, filt):
        holder = QWidget()
        h = QHBoxLayout(holder)
        h.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(getattr(obj, attr) or "(không)")
        lbl.setWordWrap(True)
        h.addWidget(lbl, 1)

        def pick():
            f, _ = QFileDialog.getOpenFileName(self, title, "", filt)
            if f:
                setattr(obj, attr, f)
                lbl.setText(f)
                self._after_cfg_change()

        def clr():
            setattr(obj, attr, "")
            lbl.setText("(không)")
            self._after_cfg_change()

        b1 = QPushButton("Chọn…"); b1.clicked.connect(pick)
        b2 = QPushButton("Xóa"); b2.clicked.connect(clr)
        h.addWidget(b1); h.addWidget(b2)
        return holder

    def _add_text_overlay_rows(self, form, tcfg, prefix: str, key: str,
                               is_hook: bool = False) -> None:
        chk = QCheckBox(f"Bật {prefix}")
        chk.setChecked(tcfg.enabled)
        self._bind_bool(chk, tcfg, "enabled", prefix)
        form.addRow("Trạng thái", chk)
        children = []
        le = QLineEdit(tcfg.text)
        self._bind_text(le, tcfg, "text")
        form.addRow("Nội dung", le); children.append(le)
        if is_hook:
            chka = QCheckBox("Tự lấy câu mở đầu transcript nếu để trống")
            chka.setChecked(tcfg.auto)
            self._bind_bool(chka, tcfg, "auto")
            form.addRow("Tự động", chka); children.append(chka)
        cmb = self._data_combo([("top", "Trên"), ("middle", "Giữa"), ("bottom", "Dưới")],
                               tcfg.position)
        self._bind_combo(cmb, tcfg, "position")
        cmb.currentIndexChanged.connect(lambda _i, k=key, c=cmb: self._on_brand_combo_position(k, c))
        self._pos_combos[key] = cmb
        form.addRow("Vị trí", cmb); children.append(cmb)
        sp = self._spin(QDoubleSpinBox, 0.5, 60.0, tcfg.seconds, 0.5)
        self._bind_float(sp, tcfg, "seconds")
        form.addRow("Thời gian hiển thị (giây)", sp); children.append(sp)
        spf = self._spin(QSpinBox, 16, 96, tcfg.font_size)
        self._bind_int(spf, tcfg, "font_size")
        form.addRow("Cỡ chữ", spf); children.append(spf)
        chkb = QCheckBox("Nền hộp cho dễ đọc")
        chkb.setChecked(tcfg.box)
        self._bind_bool(chkb, tcfg, "box")
        form.addRow("Nền chữ", chkb); children.append(chkb)
        for widget in children:
            widget.setEnabled(tcfg.enabled)
        chk.toggled.connect(lambda on, ws=children: [w.setEnabled(on) for w in ws])

    def _build_hook_cta_logo_group(self) -> QWidget:
        e = self.cfg.editor
        ov = e.overlay
        tabs = QTabWidget()
        logo_page = QWidget(); form = QFormLayout(logo_page)

        self.chk_logo = QCheckBox("Thêm logo/watermark lên video")
        self.chk_logo.setChecked(ov.enabled)
        self._bind_bool(self.chk_logo, ov, "enabled", "Logo")
        form.addRow("Bật logo", self.chk_logo)
        logo_path = self._path_row(ov, "image_path", "Chọn ảnh logo", "Ảnh (*.png *.jpg *.webp)")
        form.addRow("Ảnh logo", logo_path)
        self.cmb_logopos = self._data_combo(
            [("top-left", "Trên-trái"), ("top-right", "Trên-phải"),
             ("bottom-left", "Dưới-trái"), ("bottom-right", "Dưới-phải")], ov.position)
        self._bind_combo(self.cmb_logopos, ov, "position", "Vị trí logo")
        self.cmb_logopos.currentIndexChanged.connect(
            lambda _i: self._on_brand_combo_position("logo", self.cmb_logopos))
        form.addRow("Vị trí logo", self.cmb_logopos)
        self.sp_logoscale = self._spin(QDoubleSpinBox, 0.0, 0.5, ov.scale, 0.01)
        self._bind_float(self.sp_logoscale, ov, "scale", "Size logo")
        form.addRow("Kích thước (0.15 = 15%)", self.sp_logoscale)
        self.sp_logoop = self._spin(QDoubleSpinBox, 0.0, 1.0, ov.opacity, 0.05)
        self._bind_float(self.sp_logoop, ov, "opacity")
        form.addRow("Độ trong suốt", self.sp_logoop)
        logo_children = (logo_path, self.cmb_logopos, self.sp_logoscale, self.sp_logoop)
        for widget in logo_children:
            widget.setEnabled(ov.enabled)
        self.chk_logo.toggled.connect(
            lambda on: [widget.setEnabled(on) for widget in logo_children])
        tabs.addTab(logo_page, "Logo")

        hook_page = QWidget(); hook_form = QFormLayout(hook_page)
        self._add_text_overlay_rows(hook_form, e.intro_hook, "Hook mở đầu", "hook", is_hook=True)
        tabs.addTab(hook_page, "Hook mở đầu")
        cta_page = QWidget(); cta_form = QFormLayout(cta_page)
        self._add_text_overlay_rows(cta_form, e.outro_cta, "CTA cuối video", "cta")
        tabs.addTab(cta_page, "CTA cuối video")
        self.brand_tabs = tabs
        tabs.currentChanged.connect(
            lambda i: self.cmb_preview_target.setCurrentIndex(i)
            if hasattr(self, "cmb_preview_target") else None)
        return tabs

    def _audio_file_row(self, attr: str):
        """Hàng chọn/xóa 1 file audio (replace_music / voiceover)."""
        a = self.cfg.editor.audio
        holder = QWidget()
        h = QHBoxLayout(holder)
        h.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(getattr(a, attr) or "(không)")
        lbl.setWordWrap(True)
        self._audio_labels[attr] = lbl
        h.addWidget(lbl, 1)
        pick = QPushButton("Chọn…")
        pick.clicked.connect(lambda: self._pick_audio_file(attr, lbl))
        clr = QPushButton("Xóa")
        clr.clicked.connect(lambda: self._clear_audio_file(attr, lbl))
        h.addWidget(pick)
        h.addWidget(clr)
        return holder

    def _pick_audio_file(self, attr: str, lbl: QLabel) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "Chọn file âm thanh", "",
            "Audio/Video (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.mp4);;Tất cả (*.*)")
        if not f:
            return
        setattr(self.cfg.editor.audio, attr, f)
        lbl.setText(f)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()

    def _clear_audio_file(self, attr: str, lbl: QLabel) -> None:
        setattr(self.cfg.editor.audio, attr, "")
        lbl.setText("(không)")
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()

    def _build_video_settings_group(self) -> QWidget:
        e, cg = self.cfg.editor, self.cfg.editor.color_grading
        g = QGroupBox("Bố cục & hình ảnh")
        form = QFormLayout(g)
        self.cmb_fill = self._data_combo([
            ("blur", "Giữ toàn bộ video + nền mờ (giống preview)"),
            ("pad_black", "Giữ toàn bộ video + viền đen"),
        ], e.fill_missing)
        self._bind_combo(self.cmb_fill, e, "fill_missing", "Bố cục đầu ra")
        form.addRow("Bố cục đầu ra", self.cmb_fill)
        crop_hint = QLabel(
            "Nguồn ngang/1:1: cắt trái-phải, phóng nội dung giữa và lấp trên-dưới bằng "
            "nền mờ. Nguồn đã là 9:16: cắt đều cả 4 cạnh rồi phóng đầy khung, không "
            "thêm nền mờ. Preview và video xuất dùng cùng một bố cục.")
        crop_hint.setWordWrap(True); crop_hint.setStyleSheet("color:#64748b;")
        form.addRow("", crop_hint)
        self.chk_flip = QCheckBox("Lật hình theo chiều ngang")
        self.chk_flip.setChecked(e.flip_horizontal)
        self._bind_bool(self.chk_flip, e, "flip_horizontal", "Lật ngang")
        form.addRow("Lật ngang", self.chk_flip)
        self.chk_mirror = QCheckBox("Phản chiếu phần rìa khung hình")
        self.chk_mirror.setChecked(e.mirror_crop)
        self._bind_bool(self.chk_mirror, e, "mirror_crop", "Phản chiếu")
        form.addRow("Phản chiếu hai bên", self.chk_mirror)
        self.sp_speed = self._spin(QDoubleSpinBox, 0.25, 4.0, e.speed, 0.05)
        self._bind_float(self.sp_speed, e, "speed", "Tốc độ video")
        form.addRow("Tốc độ video", self.sp_speed)
        # zoom_fill_percent chỉ dành cho crop-to-fill cũ. Giữ widget nội bộ để phần
        # khôi phục mặc định tương thích, nhưng không hiển thị một thiết lập vô tác dụng.
        self.sp_zoom = self._spin(QSpinBox, 0, 30, e.zoom_fill_percent)
        self._bind_int(self.sp_zoom, e, "zoom_fill_percent", "Phóng lớn")
        self.sp_sidecrop = self._spin(QSpinBox, 0, 10, int(e.side_crop_percent))
        self.sp_sidecrop.setSuffix("%")
        self.sp_sidecrop.setToolTip(
            "Ví dụ 5%: nguồn ngang/1:1 cắt 5% trái và phải; nguồn 9:16 cắt 5% ở "
            "cả trái, phải, trên và dưới. Sau đó phóng trở lại khung đầu ra.")
        self._bind_int(self.sp_sidecrop, e, "side_crop_percent", "Cắt và phóng")
        form.addRow("Cắt & phóng", self.sp_sidecrop)
        crop_detail = QLabel(
            "Giá trị lớn hơn làm nội dung giữa to hơn; 9:16 không tạo nền mờ.")
        crop_detail.setWordWrap(True); crop_detail.setStyleSheet("color:#64748b;")
        form.addRow("", crop_detail)
        self.sp_sidecrop.setEnabled(e.fill_missing == "blur")
        self.cmb_fill.currentIndexChanged.connect(
            lambda _i: self.sp_sidecrop.setEnabled(self.cmb_fill.currentData() == "blur"))
        self.chk_color = QCheckBox("Bật điều chỉnh màu sắc")
        self.chk_color.setChecked(cg.enabled)
        self._bind_bool(self.chk_color, cg, "enabled", "Điều chỉnh màu")
        form.addRow("Màu sắc", self.chk_color)
        self.sp_bright = self._spin(QDoubleSpinBox, -1.0, 1.0, cg.brightness, 0.05)
        self.sp_contrast = self._spin(QDoubleSpinBox, 0.0, 2.0, cg.contrast, 0.05)
        self.sp_sat = self._spin(QDoubleSpinBox, 0.0, 3.0, cg.saturation, 0.05)
        self._bind_float(self.sp_bright, cg, "brightness")
        self._bind_float(self.sp_contrast, cg, "contrast")
        self._bind_float(self.sp_sat, cg, "saturation")
        form.addRow("Độ sáng", self.sp_bright)
        form.addRow("Độ tương phản", self.sp_contrast)
        form.addRow("Độ bão hòa", self.sp_sat)
        color_children = (self.sp_bright, self.sp_contrast, self.sp_sat)
        for widget in color_children:
            widget.setEnabled(cg.enabled)
        self.chk_color.toggled.connect(
            lambda on: [widget.setEnabled(on) for widget in color_children])
        return g

    def _build_audio_settings_group(self) -> QWidget:
        e, a = self.cfg.editor, self.cfg.editor.audio
        g = QGroupBox("Âm thanh")
        form = QFormLayout(g)
        self.chk_mute = QCheckBox("Video đầu ra không có âm thanh")
        self.chk_mute.setChecked(a.mute_all)
        self._bind_bool(self.chk_mute, a, "mute_all", "Tắt âm thanh")
        form.addRow("Âm thanh đầu ra", self.chk_mute)
        self.chk_sep = QCheckBox("Giữ lời thoại, loại bớt nhạc nền")
        self.chk_sep.setChecked(bool(a.separate_speech))
        self.chk_sep.toggled.connect(self._on_sep_changed)
        form.addRow("Tách lời thoại", self.chk_sep)
        self.cmb_sep = self._data_combo(
            [("mdx", "MDX — nhẹ, chạy CPU"), ("demucs", "Demucs — chất lượng cao, GPU"),
             ("vr", "VR Architecture")], a.separator_backend)
        self._bind_combo(self.cmb_sep, a, "separator_backend", "Công nghệ tách giọng")
        self.cmb_sep.setEnabled(a.separate_speech)
        self.chk_sep.toggled.connect(self.cmb_sep.setEnabled)
        form.addRow("Công nghệ tách giọng", self.cmb_sep)
        self.chk_duck = QCheckBox("Tự giảm nhạc nền khi có lời thoại")
        self.chk_duck.setChecked(a.duck_music)
        self._bind_bool(self.chk_duck, a, "duck_music", "Tự giảm nhạc")
        form.addRow("Tự giảm nhạc", self.chk_duck)
        self.sp_pitch = self._spin(QSpinBox, -12, 12, a.pitch_shift_semitones)
        self._bind_int(self.sp_pitch, a, "pitch_shift_semitones", "Độ cao giọng")
        form.addRow("Độ cao giọng", self.sp_pitch)
        self.sp_aspeed = self._spin(QDoubleSpinBox, 0.25, 4.0, a.audio_speed, 0.05)
        self._bind_float(self.sp_aspeed, a, "audio_speed", "Tốc độ audio")
        form.addRow("Tốc độ âm thanh", self.sp_aspeed)
        self.sp_mvol = self._spin(QDoubleSpinBox, 0.0, 1.0, a.music_volume, 0.05)
        self._bind_float(self.sp_mvol, a, "music_volume", "Âm lượng nhạc nền")
        form.addRow("Âm lượng nhạc nền", self.sp_mvol)
        form.addRow("Nhạc nền thay thế", self._audio_file_row("replace_music"))
        form.addRow("Voiceover", self._audio_file_row("voiceover"))
        self.cmb_shortmode = self._data_combo(
            [("start", "Đoạn đầu video"), ("highlight", "Đoạn sôi động nhất")], e.export.short_mode)
        self._bind_combo(self.cmb_shortmode, e.export, "short_mode", "Cách chọn short")
        form.addRow("Cách chọn bản short", self.cmb_shortmode)
        btn_reset = QPushButton("Khôi phục mặc định")
        btn_reset.clicked.connect(self._restore_advanced_defaults)
        form.addRow("", btn_reset)
        return g

    def _build_advanced_group(self) -> QWidget:
        e = self.cfg.editor
        cg = e.color_grading
        a = e.audio
        g = QGroupBox("Cài đặt nâng cao (lưu ngay vào config)")
        form = QFormLayout(g)

        # --- Video ---
        self.chk_flip = QCheckBox()
        self.chk_flip.setChecked(e.flip_horizontal)
        self._bind_bool(self.chk_flip, e, "flip_horizontal", "Lật ngang")
        form.addRow("Lật ngang (flip)", self.chk_flip)

        self.chk_mirror = QCheckBox()
        self.chk_mirror.setChecked(e.mirror_crop)
        self._bind_bool(self.chk_mirror, e, "mirror_crop", "Mirror+crop")
        form.addRow("Mirror + crop", self.chk_mirror)

        self.sp_speed = self._spin(QDoubleSpinBox, 0.25, 4.0, e.speed, 0.05)
        self._bind_float(self.sp_speed, e, "speed", "Tốc độ video")
        form.addRow("Tốc độ video", self.sp_speed)

        self.sp_zoom = self._spin(QSpinBox, 0, 30, e.zoom_fill_percent)
        self._bind_int(self.sp_zoom, e, "zoom_fill_percent", "Zoom fill %")
        form.addRow("Zoom fill % (chế độ none)", self.sp_zoom)

        self.sp_sidecrop = self._spin(QSpinBox, 0, 10, int(e.side_crop_percent))
        self._bind_int(self.sp_sidecrop, e, "side_crop_percent", "Cắt 2 bên %")
        form.addRow("Cắt 2 bên % (chế độ blur)", self.sp_sidecrop)

        self.chk_color = QCheckBox()
        self.chk_color.setChecked(cg.enabled)
        self._bind_bool(self.chk_color, cg, "enabled", "Color grading")
        form.addRow("Color grading bật", self.chk_color)
        self.sp_bright = self._spin(QDoubleSpinBox, -1.0, 1.0, cg.brightness, 0.05)
        self._bind_float(self.sp_bright, cg, "brightness")
        form.addRow("  Brightness (-1..1)", self.sp_bright)
        self.sp_contrast = self._spin(QDoubleSpinBox, 0.0, 2.0, cg.contrast, 0.05)
        self._bind_float(self.sp_contrast, cg, "contrast")
        form.addRow("  Contrast (0..2)", self.sp_contrast)
        self.sp_sat = self._spin(QDoubleSpinBox, 0.0, 3.0, cg.saturation, 0.05)
        self._bind_float(self.sp_sat, cg, "saturation")
        form.addRow("  Saturation (0..3)", self.sp_sat)

        # --- Audio ---
        self.chk_mute = QCheckBox("Xóa HẾT mọi âm thanh (kể cả voice)")
        self.chk_mute.setChecked(a.mute_all)
        self._bind_bool(self.chk_mute, a, "mute_all", "Xóa hết âm thanh")
        form.addRow("Tắt toàn bộ audio", self.chk_mute)
        self.chk_duck = QCheckBox("Nhạc nền tự hạ khi có lời thoại (ducking)")
        self.chk_duck.setChecked(a.duck_music)
        self._bind_bool(self.chk_duck, a, "duck_music", "Ducking nhạc nền")
        form.addRow("Ducking", self.chk_duck)
        self.chk_fp = QCheckBox("Fingerprint: biến đổi nhẹ mỗi video (chống trùng)")
        self.chk_fp.setChecked(e.fingerprint_enabled)
        self._bind_bool(self.chk_fp, e, "fingerprint_enabled", "Fingerprint")
        form.addRow("Chống trùng", self.chk_fp)
        self.cmb_shortmode = self._data_combo(
            [("start", "100s đầu"), ("highlight", "Đoạn sôi động nhất")], e.export.short_mode)
        self._bind_combo(self.cmb_shortmode, e.export, "short_mode", "Kiểu chọn short")
        form.addRow("Cách lấy bản short", self.cmb_shortmode)

        self.sp_pitch = self._spin(QSpinBox, -12, 12, a.pitch_shift_semitones)
        self._bind_int(self.sp_pitch, a, "pitch_shift_semitones", "Pitch")
        form.addRow("Pitch shift (nửa cung)", self.sp_pitch)
        self.sp_aspeed = self._spin(QDoubleSpinBox, 0.25, 4.0, a.audio_speed, 0.05)
        self._bind_float(self.sp_aspeed, a, "audio_speed", "Tốc độ audio")
        form.addRow("Tốc độ audio (thêm)", self.sp_aspeed)
        self.sp_mvol = self._spin(QDoubleSpinBox, 0.0, 1.0, a.music_volume, 0.05)
        self._bind_float(self.sp_mvol, a, "music_volume", "Âm lượng nhạc nền")
        form.addRow("Âm lượng nhạc thay (0..1)", self.sp_mvol)
        form.addRow("Nhạc nền thay", self._audio_file_row("replace_music"))
        form.addRow("Voiceover", self._audio_file_row("voiceover"))

        btn_reset = QPushButton("Khôi phục mặc định")
        btn_reset.clicked.connect(self._restore_advanced_defaults)
        form.addRow("", btn_reset)
        return g

    def _restore_advanced_defaults(self) -> None:
        if QMessageBox.question(
                self, "Khôi phục mặc định",
                "Đưa các cài đặt nâng cao (video + âm thanh) về mặc định?\n"
                "Không đổi: khung hình, codec, thư mục, cookies, tách giọng.") != QMessageBox.Yes:
            return
        d = EditorCfg()               # giá trị mặc định
        e = self.cfg.editor
        e.flip_horizontal = d.flip_horizontal
        e.mirror_crop = d.mirror_crop
        e.speed = d.speed
        e.zoom_fill_percent = d.zoom_fill_percent
        e.side_crop_percent = d.side_crop_percent
        e.color_grading.enabled = d.color_grading.enabled
        e.color_grading.brightness = d.color_grading.brightness
        e.color_grading.contrast = d.color_grading.contrast
        e.color_grading.saturation = d.color_grading.saturation
        e.audio.mute_all = d.audio.mute_all
        e.audio.pitch_shift_semitones = d.audio.pitch_shift_semitones
        e.audio.audio_speed = d.audio.audio_speed
        e.audio.music_volume = d.audio.music_volume
        e.audio.replace_music = d.audio.replace_music
        e.audio.voiceover = d.audio.voiceover

        # cập nhật widget, chặn signal để KHÔNG lưu nhiều lần
        pairs = [
            (self.chk_flip, e.flip_horizontal), (self.chk_mirror, e.mirror_crop),
            (self.chk_color, e.color_grading.enabled), (self.chk_mute, e.audio.mute_all),
            (self.sp_speed, e.speed), (self.sp_zoom, e.zoom_fill_percent),
            (self.sp_sidecrop, int(e.side_crop_percent)),
            (self.sp_bright, e.color_grading.brightness),
            (self.sp_contrast, e.color_grading.contrast),
            (self.sp_sat, e.color_grading.saturation),
            (self.sp_pitch, e.audio.pitch_shift_semitones),
            (self.sp_aspeed, e.audio.audio_speed), (self.sp_mvol, e.audio.music_volume),
        ]
        for w, val in pairs:
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(val))
            else:
                w.setValue(val)
            w.blockSignals(False)
        for attr in ("replace_music", "voiceover"):
            if attr in self._audio_labels:
                self._audio_labels[attr].setText("(không)")

        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        self._log_ed("Đã khôi phục cài đặt nâng cao về mặc định.")

    def _save_cfg(self) -> bool:
        if not self.config_path:
            return False
        try:
            save_config(self.cfg, self.config_path)
            if hasattr(self, "lbl_edit_save_status"):
                self.lbl_edit_save_status.setText("✓ Đã lưu")
                self.lbl_edit_save_status.setStyleSheet("color:#15803d;")
            return True
        except Exception as ex:
            QMessageBox.warning(self, "Lỗi lưu config", str(ex))
            return False

    def on_pick_root_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu video TẢI VỀ", self.cfg.download.root_dir or "")
        if not d:
            return
        self.cfg.download.root_dir = d
        self.lbl_root.setText(d)
        self.lbl_channels.setText(self._channels_summary())
        self._save_cfg()
        self._log_dl(f"Đã đổi thư mục tải về: {d}")

    def on_pick_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục XUẤT file sau khi edit", self.cfg.editor.output_dir or "")
        if not d:
            return
        self.cfg.editor.output_dir = d
        self.lbl_output.setText(d)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        self._log_ed(f"Đã đổi thư mục xuất: {d}")

    @staticmethod
    def _set_combo_data(w, value) -> None:
        for i in range(w.count()):
            if w.itemData(i) == value:
                w.blockSignals(True); w.setCurrentIndex(i); w.blockSignals(False)
                return

    def on_apply_preset(self) -> None:
        name = self.cmb_preset.currentData()
        if not apply_platform_preset(self.cfg, name):
            return
        e = self.cfg.editor
        self.cmb_aspect.blockSignals(True)
        self.cmb_aspect.setCurrentText(e.target_aspect)
        self.cmb_aspect.blockSignals(False)
        self._set_combo_data(self.cmb_subpos, e.subtitle.position)
        self.sp_subsize.blockSignals(True); self.sp_subsize.setValue(e.subtitle.font_size)
        self.sp_subsize.blockSignals(False)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        self._log_ed(f"Đã áp preset '{name}': khung {e.target_aspect}, short {e.export.short_seconds}s.")

    def _downloaded_videos(self) -> list[str]:
        return [r.download_path for r in self.db.all_videos()
                if r.download_status == "downloaded" and r.download_path
                and Path(r.download_path).exists()]

    def on_preview(self, batch: bool = False) -> None:
        vids = self._downloaded_videos()
        if not vids:
            QMessageBox.information(self, "Chưa có video", "Cần ít nhất 1 video đã tải để xem trước.")
            return
        out_dir = str(Path(self.cfg.editor.output_dir) / "_previews")
        try:
            if batch:
                res = preview.preview_batch(self.cfg, vids, out_dir, at_seconds=1.0)
                ok = sum(r["ok"] for r in res)
                self._log_ed(f"Xem trước hàng loạt: {ok}/{len(res)} ảnh -> {out_dir}")
                if os.path.isdir(out_dir):
                    os.startfile(out_dir)
            else:
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                png = str(Path(out_dir) / "preview.png")
                preview.preview_frame(self.cfg, vids[0], png, at_seconds=1.0)
                self._log_ed(f"Xem trước: {png}")
                if hasattr(self, "lbl_live_preview"):
                    pix = QPixmap(png)
                    self.lbl_live_preview.set_source(pix)
                else:
                    os.startfile(png)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi xem trước", str(e))

    def _on_inline_position_click(self, fx: float, fy: float) -> None:
        """Đặt Logo/Hook/CTA trực tiếp trên preview, không mở modal riêng."""
        target = self.cmb_preview_target.currentData()
        if hasattr(self, "brand_tabs"):
            wanted = {"logo": 0, "hook": 1, "cta": 2}.get(target, 0)
            if self.brand_tabs.currentIndex() != wanted:
                self.brand_tabs.setCurrentIndex(wanted)
        e = self.cfg.editor
        if target == "logo":
            pos = ("top" if fy < 0.5 else "bottom") + "-" + (
                "left" if fx < 0.5 else "right")
            e.overlay.position = pos
            if hasattr(self, "cmb_logopos"):
                self._set_combo_data(self.cmb_logopos, pos)
            label = {"top-left": "Trên trái", "top-right": "Trên phải",
                     "bottom-left": "Dưới trái", "bottom-right": "Dưới phải"}[pos]
        else:
            pos = "top" if fy < 0.34 else ("middle" if fy < 0.67 else "bottom")
            tcfg = e.intro_hook if target == "hook" else e.outro_cta
            tcfg.position = pos
            combo = self._pos_combos.get(target)
            if combo is not None:
                self._set_combo_data(combo, pos)
            label = {"top": "Trên", "middle": "Giữa", "bottom": "Dưới"}[pos]
        self.lbl_live_preview.set_marker(target, fx, fy)
        self.lbl_preview_position.setText(f"{label} · đã lưu")
        self._save_cfg()
        self._log_ed(f"Đặt {target} trực tiếp trên preview: {pos}")

    @staticmethod
    def _position_point(target: str, pos: str) -> tuple[float, float]:
        if target == "logo":
            return {
                "top-left": (0.15, 0.12), "top-right": (0.85, 0.12),
                "bottom-left": (0.15, 0.88), "bottom-right": (0.85, 0.88),
            }.get(pos, (0.85, 0.12))
        return {"top": (0.5, 0.15), "middle": (0.5, 0.5),
                "bottom": (0.5, 0.85)}.get(pos, (0.5, 0.85))

    def _on_preview_target_changed(self, _index: int = 0) -> None:
        if not hasattr(self, "lbl_live_preview"):
            return
        target = self.cmb_preview_target.currentData()
        e = self.cfg.editor
        pos = (e.overlay.position if target == "logo" else
               e.intro_hook.position if target == "hook" else e.outro_cta.position)
        fx, fy = self._position_point(target, pos)
        self.lbl_live_preview.set_active(target)
        self.lbl_live_preview.set_marker(target, fx, fy)
        labels = {"top-left": "Trên trái", "top-right": "Trên phải",
                  "bottom-left": "Dưới trái", "bottom-right": "Dưới phải",
                  "top": "Trên", "middle": "Giữa", "bottom": "Dưới"}
        self.lbl_preview_position.setText(f"{labels.get(pos, pos)} · đã lưu")

    def _on_brand_combo_position(self, target: str, combo: QComboBox) -> None:
        """Đồng bộ dropdown Thương hiệu -> marker riêng trên preview."""
        if not hasattr(self, "lbl_live_preview"):
            return
        pos = combo.currentData()
        fx, fy = self._position_point(target, pos)
        self.lbl_live_preview.set_marker(target, fx, fy)
        if self.cmb_preview_target.currentData() == target:
            self._on_preview_target_changed()

    def on_pick_positions(self) -> None:
        vids = self._downloaded_videos()
        if not vids:
            QMessageBox.information(self, "Chưa có video", "Cần 1 video đã tải để xem trước vị trí.")
            return
        out_dir = Path(self.cfg.editor.output_dir) / "_previews"
        out_dir.mkdir(parents=True, exist_ok=True)
        png = str(out_dir / "pos_preview.png")
        try:
            preview.preview_frame(self.cfg, vids[0], png, at_seconds=1.0)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi tạo preview", str(e))
            return
        dlg = PositionPickerDialog(png, self)
        if not dlg.exec():
            return
        e = self.cfg.editor
        p = dlg.positions
        if "logo" in p:
            e.overlay.position = p["logo"]
            self._set_combo_data(self.cmb_logopos, p["logo"])
        for key, tcfg in (("hook", e.intro_hook), ("cta", e.outro_cta)):
            if key in p:
                tcfg.position = p[key]
                combo = self._pos_combos.get(key)
                if combo is not None:
                    self._set_combo_data(combo, p[key])
        self.lbl_edit_summary.setText(self._edit_summary())
        self._save_cfg()
        self._log_ed(f"Đã đặt vị trí trực quan: {p}")

    def on_export_report(self) -> None:
        out = str(Path(self.cfg.editor.output_dir) / "bao_cao.md")
        try:
            report.write_report(self.db, out)
            self._log_ed(f"Đã xuất báo cáo: {out}")
            os.startfile(out)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi báo cáo", str(e))

    def _toggle_auto(self, on: bool) -> None:
        """Bật/tắt tự động quét & tải định kỳ ở module cập nhật."""
        if on:
            self._auto.start_scheduler()
            self._log_dl("Đã BẬT tự động quét & tải.")
        else:
            self._auto.stop_scheduler()
            self._log_dl("Đã TẮT tự động quét & tải (bấm 'Quét ngay' để chạy tay).")

    def on_pick_input_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Chọn folder video đầu vào", self.cfg.editor.input_folder or "")
        if not folder:
            return
        self.cfg.editor.input_folder = folder
        self.lbl_input.setText(folder)
        if self.config_path:
            try:
                save_config(self.cfg, self.config_path)
            except Exception as ex:
                QMessageBox.warning(self, "Lỗi lưu config", str(ex))

    def on_edit_folder(self) -> None:
        """Import video từ folder đầu vào rồi đẩy video MỚI/CHƯA EDIT vào hàng đợi."""
        folder = self.cfg.editor.input_folder
        if not folder or not Path(folder).is_dir():
            QMessageBox.information(self, "Chưa chọn folder",
                                    "Hãy bấm 'Chọn folder…' để chọn folder chứa video đầu vào.")
            return
        res = ingest_folder(self.db, folder)
        self._log_ed(f"Folder '{res['channel']}': tổng {res['total']}, mới {res['added']}, "
                     f"bỏ qua đã xong {res['skipped_done']}. Chuyển sang tab Hàng đợi.")
        self.tabs.setCurrentIndex(self._queue_idx)
        self._queue_tab.refresh()
        self._queue_tab.on_start()

    def on_pick_crop(self) -> None:
        """Lấy 1 khung hình video đã tải -> mở dialog chọn tâm vùng crop -> lưu config."""
        src = None
        for r in self.db.all_videos():
            if r.download_status == "downloaded" and r.download_path and Path(r.download_path).exists():
                src = r.download_path
                break
        if not src:
            QMessageBox.information(self, "Chưa có video",
                                    "Cần ít nhất 1 video đã tải xong để chọn vùng crop.")
            return
        try:
            preview = str(Path(self.cfg.editor.output_dir) / "_crop_preview.png")
            smart_crop.grab_frame(src, preview, at_seconds=1.0)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi lấy khung hình", str(e))
            return

        e = self.cfg.editor
        ow, oh = video_ops.target_resolution(e.target_aspect)
        dlg = CropSelectDialog(preview, ow / oh, (e.manual_focus_x, e.manual_focus_y), self)
        if not dlg.exec():
            return
        fx, fy = dlg.focus()
        e.crop_mode = "manual"
        e.fill_missing = "none"  # chọn vùng thủ công -> crop-to-fill quanh vùng đó
        e.manual_focus_x = round(fx, 4)
        e.manual_focus_y = round(fy, 4)
        if self.config_path:
            try:
                save_config(self.cfg, self.config_path)
            except Exception as ex:
                QMessageBox.warning(self, "Lỗi lưu config", str(ex))
        self.lbl_edit_summary.setText(self._edit_summary())
        self._log_ed(f"Đã đặt vùng crop thủ công: focus=({fx:.2f},{fy:.2f}); "
                     f"crop_mode=manual, fill_missing=none.")

    # ---------------- Bảng & log ----------------
    def _reload_table(self) -> None:
        rows = self.db.all_videos()
        self.table.setRowCount(len(rows))
        download_labels = {
            "pending": ("Chờ tải", "#475569", "#f1f5f9"),
            "downloading": ("Đang tải", "#1d4ed8", "#dbeafe"),
            "cancelling": ("Đang dừng", "#b45309", "#fef3c7"),
            "paused": ("Tạm dừng", "#9a3412", "#ffedd5"),
            "downloaded": ("Đã tải", "#15803d", "#dcfce7"),
            "failed": ("Lỗi", "#b91c1c", "#fee2e2"),
        }
        edit_labels = {
            "pending": ("Chờ biên tập", "#475569", "#f1f5f9"),
            "processing": ("Đang biên tập", "#6d28d9", "#ede9fe"),
            "done": ("Hoàn thành", "#15803d", "#dcfce7"),
            "failed": ("Lỗi", "#b91c1c", "#fee2e2"),
        }
        for i, r in enumerate(rows):
            vals = [r.video_id, r.title, r.channel_name]
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))
            for col, raw, mapping in ((3, r.download_status, download_labels),
                                      (4, r.edit_status, edit_labels)):
                text, fg, bg = mapping.get(raw, (str(raw), "#334155", "#f8fafc"))
                item = QTableWidgetItem(text)
                item.setForeground(QColor(fg))
                item.setBackground(QColor(bg))
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, col, item)

            if r.download_status == "downloaded" and r.download_path:
                action = QPushButton("Mở thư mục")
                action.clicked.connect(
                    lambda _checked=False, path=r.download_path: self._open_download_path(path))
            elif r.download_status in ("pending", "failed"):
                action = QPushButton("Thử lại" if r.download_status == "failed" else "Tải ngay")
                action.clicked.connect(
                    lambda _checked=False, url=r.url: self._download_from_table(url))
            elif r.download_status == "paused":
                action = QPushButton("Tiếp tục")
                action.clicked.connect(
                    lambda _checked=False, vid=r.video_id, url=r.url: self._resume_one_download(vid, url))
            elif r.download_status == "downloading":
                action = QPushButton("Dừng")
                action.setStyleSheet("color:#b91c1c;")
                action.clicked.connect(
                    lambda _checked=False, vid=r.video_id: self._cancel_download(vid))
            else:
                action = QPushButton("Đang dừng…")
                action.setEnabled(False)
            action.setMaximumWidth(92)
            self.table.setCellWidget(i, 5, action)

        counts = {"pending": 0, "downloading": 0, "cancelling": 0,
                  "paused": 0, "downloaded": 0, "failed": 0}
        for row in rows:
            if row.download_status in counts:
                counts[row.download_status] += 1
        self.lbl_download_summary.setText(
            f"Chờ {counts['pending']} · Đang tải {counts['downloading']} · "
            f"Tạm dừng {counts['paused'] + counts['cancelling']} · "
            f"Đã tải {counts['downloaded']} · Lỗi {counts['failed']}")
        self.btn_resume_downloads.setVisible(counts["paused"] > 0)
        if counts["downloading"] or counts["cancelling"]:
            self.btn_stop_downloads.show()

    def _on_status(self, video_id: str, field: str, value: str) -> None:
        self._reload_table()

    def _on_auto_status(self, video_id: str, field: str, value: str) -> None:
        # Chạy trên UI thread (qua bridge) -> chạm widget an toàn.
        self._reload_table()
        self._log_dl(f"[auto] {video_id}: {field}={value}")

    def _on_edit_item_done(self, video_id: str) -> None:
        # 1 video edit xong (từ luồng nền) -> cập nhật bảng + lịch sử trên UI thread.
        self._reload_table()
        if hasattr(self, "tbl_exports"):
            self._reload_history()

    def _log_dl(self, msg: str) -> None:
        # Tab Tải không hiển thị log kỹ thuật; vẫn ghi file logs/app.log và giữ
        # một dòng trạng thái thân thiện ở header.
        log.info(msg)

    def _log_ed(self, msg: str) -> None:
        # Không hiển thị log trong màn hình cấu hình; thông tin kỹ thuật vẫn vào file log.
        log.info(msg)

    def closeEvent(self, event) -> None:
        for stop in (lambda: self._auto.cancel_all_downloads(),
                     lambda: self._auto.stop_scheduler(),
                     lambda: self._scan_worker.cancel_all() if self._scan_worker else None,
                     lambda: self._manual_worker.cancel() if self._manual_worker else None,
                     lambda: self._queue_tab.shutdown()):
            try:
                stop()
            except Exception:
                pass
        super().closeEvent(event)
