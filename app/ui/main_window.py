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
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import (Qt, QObject, Signal, QRect, QRectF, QSize, QPoint, QPointF, QEvent,
                            QTimer, QFileSystemWatcher, QThread)
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QPlainTextEdit, QLabel, QTabWidget,
    QHeaderView, QMessageBox, QDialog, QCheckBox, QFileDialog, QComboBox,
    QGroupBox, QFormLayout, QGridLayout, QSpinBox, QDoubleSpinBox, QLineEdit, QScrollArea,
    QListWidget, QListWidgetItem, QLayout, QSizePolicy, QStackedWidget, QAbstractSpinBox,
    QAbstractItemView, QColorDialog, QSplitter, QFrame, QInputDialog,
)

from ..config import AppConfig, EditorCfg, MaskRegionCfg, save_config
from ..source_controller import SourceController, SourceError
from ..store import ExcelStore
from ..logging_setup import get_logger
from ..downloader.scheduler import ScanService
from ..downloader.downloader import quality_format
from ..editor import smart_crop, video_ops, preview
from ..editor.local_source import ingest_folder, ingest_paths, list_videos, video_id_for
from .queue_tab import QueueTab
from .theme import make_columns_resizable
from ..workers.jobs import ScanWorker, CheckChannelWorker, ManualDownloadWorker
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
    progress = Signal(str, float, str)  # (video_id, %, note) tiến trình TẢI
    edited = Signal(str)    # (dự phòng)
    downloaded = Signal(str)  # tải xong 1 video (từ thread scheduler) -> UI thread


class _EdgeVoicesWorker(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def run(self) -> None:
        try:
            from ..editor.edge_tts_service import list_voices
            self.loaded.emit(list_voices())
        except Exception as exc:
            self.failed.emit(str(exc))


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
    """Preview co giãn, click trực tiếp để đặt phụ đề/logo/hook/CTA."""
    clicked = Signal(float, float)
    mask_changed = Signal(int, float, float, float, float)
    mask_selected = Signal(int)
    mask_edit_finished = Signal(int)

    def __init__(self, text: str = ""):
        super().__init__(text)
        self._source = QPixmap()
        self._markers: dict[str, tuple[float, float]] = {}
        self._active = "logo"
        self._safe_margins = (0.0, 0.0, 0.0, 0.0)
        self._mask_rects = []
        self._mask_visible = []
        self._mask_locked = []
        self._mask_names = []
        self._active_mask = -1
        self._drag = None
        self._mask_allowed: set[int] | None = None
        self._edit_chrome = False
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)

    def set_source(self, pixmap: QPixmap) -> None:
        self._source = pixmap
        self._refresh_pixmap()

    def set_marker(self, key: str, fx: float, fy: float) -> None:
        self._markers[key] = (max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy)))
        self.update()

    def set_active(self, key: str) -> None:
        self._active = key
        self.update()

    def set_masks(self, masks, active: int = -1) -> None:
        self._mask_rects = [(float(m.x), float(m.y), float(m.width), float(m.height))
                            for m in masks]
        self._mask_visible = [bool(getattr(m, "visible", True)) for m in masks]
        self._mask_locked = [bool(getattr(m, "locked", False)) for m in masks]
        self._mask_names = [str(getattr(m, "name", "") or f"Vùng che {i + 1}")
                            for i, m in enumerate(masks)]
        self._active_mask = active
        if active >= 0:
            self._active = "mask"
        self.update()

    def set_active_mask(self, index: int) -> None:
        self._active = "mask"
        self._active_mask = index
        self.update()

    def set_mask_context(self, indices=None) -> None:
        """Limit interactive mask overlays without changing render config."""
        self._mask_allowed = None if indices is None else set(indices)
        self.update()

    def set_edit_chrome(self, enabled: bool) -> None:
        self._edit_chrome = bool(enabled)
        if not enabled:
            self._drag = None
            self.unsetCursor()
        self.update()

    def _mask_is_available(self, index: int) -> bool:
        return (self._mask_allowed is None or index in self._mask_allowed)

    def set_safe_margins(self, left: float, right: float,
                         top: float, bottom: float) -> None:
        self._safe_margins = tuple(
            max(0.0, min(0.45, float(value) / 100.0))
            for value in (left, right, top, bottom))
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
        if not self._edit_chrome:
            return
        pix = self.pixmap()
        if pix is None or pix.isNull():
            return
        x0 = (self.width() - pix.width()) / 2
        y0 = (self.height() - pix.height()) / 2
        fx = (event.position().x() - x0) / max(1, pix.width())
        fy = (event.position().y() - y0) / max(1, pix.height())
        if self._active == "mask" and 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
            order = list(range(len(self._mask_rects) - 1, -1, -1))
            if self._active_mask in order:
                order.remove(self._active_mask); order.insert(0, self._active_mask)
            for index in order:
                if not self._mask_is_available(index):
                    continue
                if index < len(self._mask_visible) and not self._mask_visible[index]:
                    continue
                x, y, w, h = self._mask_rects[index]
                if x - .015 <= fx <= x + w + .015 and y - .015 <= fy <= y + h + .015:
                    self._active_mask = index
                    self.mask_selected.emit(index)
                    if index < len(self._mask_locked) and self._mask_locked[index]:
                        self.update()
                        return
                    # Vùng bắt cạnh đủ rộng để thao tác trên preview nhỏ nhưng
                    # vẫn ưu tiên đúng bốn góc khi hai cạnh cùng được chạm.
                    tol_x = max(.018, 8.0 / max(1, pix.width()))
                    tol_y = max(.018, 8.0 / max(1, pix.height()))
                    horizontal = "l" if abs(fx-x) < tol_x else ("r" if abs(fx-x-w) < tol_x else "")
                    vertical = "t" if abs(fy-y) < tol_y else ("b" if abs(fy-y-h) < tol_y else "")
                    self._drag = (horizontal + vertical or "move", fx, fy, x, y, w, h)
                    self._set_mask_cursor(self._drag[0], dragging=True)
                    self.update()
                    return
            return
        if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
            self.clicked.emit(fx, fy)

    def mouseMoveEvent(self, event) -> None:
        if self.pixmap() is None or self.pixmap().isNull():
            return
        if not self._drag:
            self._update_mask_hover_cursor(event.position())
            return
        if self._active_mask < 0:
            return
        pix = self.pixmap(); x0 = (self.width() - pix.width()) / 2
        y0 = (self.height() - pix.height()) / 2
        fx = min(1.0, max(0.0, (event.position().x() - x0) / max(1, pix.width())))
        fy = min(1.0, max(0.0, (event.position().y() - y0) / max(1, pix.height())))
        mode, ox, oy, x, y, w, h = self._drag; dx, dy = fx-ox, fy-oy
        if mode == "move":
            x = min(1.0-w, max(0.0, x+dx)); y = min(1.0-h, max(0.0, y+dy))
        else:
            if "l" in mode:
                nx = min(x+w-.02, max(0.0, x+dx)); w += x-nx; x = nx
            if "r" in mode: w = min(1.0-x, max(.02, w+dx))
            if "t" in mode:
                ny = min(y+h-.02, max(0.0, y+dy)); h += y-ny; y = ny
            if "b" in mode: h = min(1.0-y, max(.02, h+dy))
        self._mask_rects[self._active_mask] = (x, y, w, h)
        self.mask_changed.emit(self._active_mask, x, y, w, h)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        edited_mask = self._active_mask if self._drag else -1
        self._drag = None
        self._update_mask_hover_cursor(event.position())
        if edited_mask >= 0:
            self.mask_edit_finished.emit(edited_mask)

    def leaveEvent(self, event) -> None:
        if not self._drag:
            self.unsetCursor()
        super().leaveEvent(event)

    def _set_mask_cursor(self, mode: str, dragging: bool = False) -> None:
        cursors = {
            "lt": Qt.SizeFDiagCursor, "rb": Qt.SizeFDiagCursor,
            "rt": Qt.SizeBDiagCursor, "lb": Qt.SizeBDiagCursor,
            "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
            "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
            "move": Qt.ClosedHandCursor if dragging else Qt.SizeAllCursor,
        }
        self.setCursor(cursors.get(mode, Qt.ArrowCursor))

    def _update_mask_hover_cursor(self, position) -> None:
        """Đổi biểu tượng chuột theo cạnh/góc/vùng có thể thao tác."""
        if not self._edit_chrome or self._active != "mask" or self.pixmap() is None:
            self.unsetCursor(); return
        pix = self.pixmap()
        x0 = (self.width() - pix.width()) / 2
        y0 = (self.height() - pix.height()) / 2
        fx = (position.x() - x0) / max(1, pix.width())
        fy = (position.y() - y0) / max(1, pix.height())
        tol_x = max(.018, 8.0 / max(1, pix.width()))
        tol_y = max(.018, 8.0 / max(1, pix.height()))
        order = list(range(len(self._mask_rects) - 1, -1, -1))
        if self._active_mask in order:
            order.remove(self._active_mask); order.insert(0, self._active_mask)
        for index in order:
            if not self._mask_is_available(index):
                continue
            if index < len(self._mask_visible) and not self._mask_visible[index]:
                continue
            x, y, w, h = self._mask_rects[index]
            if not (x-tol_x <= fx <= x+w+tol_x and y-tol_y <= fy <= y+h+tol_y):
                continue
            if index < len(self._mask_locked) and self._mask_locked[index]:
                self.setCursor(Qt.ForbiddenCursor); return
            horizontal = "l" if abs(fx-x) <= tol_x else ("r" if abs(fx-x-w) <= tol_x else "")
            vertical = "t" if abs(fy-y) <= tol_y else ("b" if abs(fy-y-h) <= tol_y else "")
            self._set_mask_cursor(horizontal + vertical or "move")
            return
        self.unsetCursor()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.pixmap() is None or self.pixmap().isNull():
            return
        pix = self.pixmap()
        x0 = (self.width() - pix.width()) / 2
        y0 = (self.height() - pix.height()) / 2
        painter = QPainter(self)
        if self._edit_chrome and self._active in ("subtitle", "hook", "cta"):
            left, right, top, bottom = self._safe_margins
            safe_x = x0 + pix.width() * left
            safe_y = y0 + pix.height() * top
            safe_w = pix.width() * max(0.02, 1.0 - left - right)
            safe_h = pix.height() * max(0.02, 1.0 - top - bottom)
            pen = QPen(QColor("#38bdf8"), 2)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(
                int(safe_x), int(safe_y), int(safe_w), int(safe_h))
        colors = {
            "subtitle": "#e879f9", "logo": "#22c55e",
            "hook": "#38bdf8", "cta": "#fb923c"}
        letters = {"subtitle": "S", "logo": "L", "hook": "H", "cta": "C"}
        # Khi chỉnh vùng che, các marker Logo/Hook/CTA chỉ tạo nhiễu và dễ khiến
        # người dùng kéo nhầm đối tượng. Chúng vẫn hiện ở các chế độ đặt vị trí cũ.
        visible_markers = (
            {self._active: self._markers[self._active]}
            if self._edit_chrome and self._active != "mask"
            and self._active in self._markers else {})
        for key, (fx, fy) in visible_markers.items():
            x = x0 + fx * pix.width(); y = y0 + fy * pix.height()
            width = 4 if key == self._active else 2
            painter.setPen(QPen(QColor(colors.get(key, "#ffffff")), width))
            painter.drawEllipse(int(x) - 10, int(y) - 10, 20, 20)
            painter.drawText(int(x) - 4, int(y) + 5, letters.get(key, "•"))
        for index, (fx, fy, fw, fh) in enumerate(self._mask_rects):
            if not self._edit_chrome or not self._mask_is_available(index):
                continue
            if index < len(self._mask_visible) and not self._mask_visible[index]:
                continue
            rect = QRectF(x0 + fx * pix.width(), y0 + fy * pix.height(),
                          fw * pix.width(), fh * pix.height())
            active = self._active == "mask" and index == self._active_mask
            painter.setPen(QPen(QColor("#fb923c"), 3 if active else 1))
            painter.setBrush(QColor(251, 146, 60, 34 if active else 18))
            painter.drawRect(rect)
            if active:
                painter.setBrush(QColor("#ffffff"))
                points = (
                    rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight(),
                    QPointF(rect.center().x(), rect.top()),
                    QPointF(rect.center().x(), rect.bottom()),
                    QPointF(rect.left(), rect.center().y()),
                    QPointF(rect.right(), rect.center().y()),
                )
                for point in points:
                    painter.drawRect(QRectF(point.x() - 5, point.y() - 5, 10, 10))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect.adjusted(5, 4, -4, -4), Qt.AlignLeft | Qt.AlignTop,
                             self._mask_names[index] if index < len(self._mask_names)
                             else f"Vùng che {index + 1}")
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
        self._saved_editor = deepcopy(cfg.editor)
        self._settings_dirty = False
        self._sources = SourceController(self.cfg.download.channels)
        self._scan_worker: ScanWorker | None = None
        self._check_worker: CheckChannelWorker | None = None
        self._manual_worker: ManualDownloadWorker | None = None
        self._resume_downloads_pending = False
        self._audio_labels: dict = {}   # attr -> QLabel (nhạc thay / voiceover)
        self._pos_combos: dict = {}     # 'hook'/'cta' -> combobox vị trí (đồng bộ khi đặt trực quan)

        self.setWindowTitle("Video Repurpose Studio")
        self.resize(1000, 640)
        db.resume_queue()   # job đang chạy dở lần trước -> Interrupted để tiếp tục
        self.tabs = QTabWidget()
        self._download_idx = self.tabs.addTab(self._build_download_tab(), "Tải xuống")
        # Queue Manager là nơi điều khiển + theo dõi tiến trình edit (driver duy nhất).
        # Hàng đợi dùng bản cấu hình đã lưu. Các thay đổi trong tab Cài đặt là
        # bản nháp cho đến khi người dùng bấm "Lưu cài đặt".
        self._queue_tab = QueueTab(deepcopy(cfg), db, device=device)
        self._queue_tab.data_changed.connect(self._on_queue_data_changed)
        self._queue_tab.edit_progress.connect(self._on_edit_progress)
        self._queue_idx = self.tabs.addTab(self._queue_tab, "Hàng đợi")
        # Tab cài đặt đặt SAU 'Hàng đợi' theo yêu cầu; vẫn dựng đầy đủ nhóm bên trong.
        self.tabs.addTab(self._build_edit_tab(), "Cài đặt video")
        self._settings_dirty = False
        self.lbl_edit_save_status.setText("✓ Đã lưu")
        self.lbl_edit_save_status.setStyleSheet("color:#15803d;")
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
        self._bridge.progress.connect(self._on_download_progress)
        self._bridge.downloaded.connect(self._on_downloaded)   # scheduler-thread -> UI thread

        # Scheduler tự động 30': tải xong -> báo (qua bridge) để cập nhật Hàng đợi.
        self._auto = ScanService(cfg, db,
                                 on_log=self._bridge.log.emit,
                                 on_status=lambda a, b, c: self._bridge.status.emit(a, b, c),
                                 on_progress=lambda a, b, c: self._bridge.progress.emit(a, b, c),
                                 on_downloaded=lambda vid: self._bridge.downloaded.emit(vid))
        self._apply_download_mode()
        if self.cfg.download.enabled and self.cfg.download.auto_scan_enabled:
            self._auto.start_scheduler()
        self._setup_input_folder_watch()
        # Resume an existing edit backlog without waiting for a new download event.
        if self.cfg.editor.auto_edit_after_download:
            QTimer.singleShot(0, self._queue_tab.on_start)

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

        self.download_mode_bar = QWidget()
        mode_row = QHBoxLayout(self.download_mode_bar)
        mode_row.setContentsMargins(0, 0, 0, 0)
        self.chk_download_enabled = QCheckBox("Bật tính năng tải video từ YouTube")
        self.chk_download_enabled.setChecked(
            bool(getattr(self.cfg.download, "enabled", True)))
        self.chk_download_enabled.setToolTip(
            "Tắt khi chỉ cần nhập và biên tập video có sẵn. "
            "Tab Hàng đợi và Cài đặt video vẫn hoạt động bình thường.")
        self.chk_download_enabled.toggled.connect(self._toggle_download_mode)
        mode_row.addWidget(self.chk_download_enabled)
        self.lbl_download_mode = QLabel()
        self.lbl_download_mode.setStyleSheet("color:#64748b;")
        mode_row.addWidget(self.lbl_download_mode)
        mode_row.addStretch(1)
        lay.addWidget(self.download_mode_bar)

        self.download_content = QWidget()
        content_lay = QVBoxLayout(self.download_content)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(6)

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
        self.chk_auto.setChecked(
            bool(getattr(self.cfg.download, "auto_scan_enabled", False)))
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
        self.btn_refresh_waiting = QPushButton("Cập nhật danh sách")
        self.btn_refresh_waiting.setToolTip(
            "Đối chiếu danh sách với file thực tế: video mất file sẽ được tải lại (hoặc xóa nếu là "
            "video nhập từ máy), đồng thời tìm video mới, rồi tự biên tập.")
        self.btn_refresh_waiting.clicked.connect(self.on_refresh_waiting)
        top.addWidget(self.btn_refresh_waiting)
        self.btn_redownload_errors = QPushButton("Tải lại video lỗi")
        self.btn_redownload_errors.setToolTip(
            "Tải lại toàn bộ video đang lỗi rồi biên tập lại. Bỏ qua video nhập từ máy.")
        self.btn_redownload_errors.setStyleSheet("color:#b45309;border-color:#fed7aa;")
        self.btn_redownload_errors.clicked.connect(self.on_redownload_errors)
        self.btn_redownload_errors.hide()
        top.addWidget(self.btn_redownload_errors)
        self.btn_stop_downloads = QPushButton("Dừng tất cả")
        self.btn_stop_downloads.setStyleSheet("color:#b91c1c;border-color:#fecaca;")
        self.btn_stop_downloads.clicked.connect(self.on_stop_all_downloads)
        self.btn_stop_downloads.hide()
        top.addWidget(self.btn_stop_downloads)
        self.btn_resume_downloads = QPushButton("Tải lại video đã dừng")
        self.btn_resume_downloads.setToolTip(
            "Đưa toàn bộ video đã dừng về trạng thái chờ và tiếp tục tải hàng loạt")
        self.btn_resume_downloads.setStyleSheet(
            "color:#1d4ed8;border-color:#93c5fd;")
        self.btn_resume_downloads.clicked.connect(self.on_resume_all_downloads)
        self.btn_resume_downloads.hide()
        top.addWidget(self.btn_resume_downloads)
        content_lay.addLayout(top)

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
        self.cmb_cookies.setMaximumWidth(180)
        self.cmb_cookies.setToolTip(
            "Chrome đang mở sẽ khóa cookie. Nếu tải báo lỗi cookie/đăng nhập, hãy chọn "
            "'Chọn file cookie…' và trỏ tới file cookies.txt đã xuất từ trình duyệt.")
        self._sync_cookie_combo()
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
        content_lay.addLayout(tools)

        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setMinimumSectionSize(32)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        make_columns_resizable(
            self.table, [1.0, 4.0, 1.35, 0.9, 1.05, 1.0], min_width=100)
        content_lay.addWidget(self.table, 1)
        lay.addWidget(self.download_content, 1)

        self.download_disabled = QFrame()
        self.download_disabled.setObjectName("downloadDisabledState")
        self.download_disabled.setStyleSheet(
            "#downloadDisabledState{background:#f8fafc;border:1px dashed #cbd5e1;"
            "border-radius:10px;}")
        disabled_lay = QVBoxLayout(self.download_disabled)
        disabled_lay.setContentsMargins(24, 24, 24, 24)
        disabled_lay.addStretch(1)
        disabled_title = QLabel("Tính năng tải YouTube đang tắt")
        disabled_title.setAlignment(Qt.AlignCenter)
        disabled_title.setStyleSheet(
            "font-size:18px;font-weight:600;color:#0f172a;")
        disabled_lay.addWidget(disabled_title)
        disabled_desc = QLabel(
            "Ứng dụng đang ở chế độ xử lý video có sẵn trên máy.\n"
            "Bạn có thể nhập video hoặc cả thư mục trực tiếp vào Hàng đợi.")
        disabled_desc.setWordWrap(True)
        disabled_desc.setAlignment(Qt.AlignCenter)
        disabled_desc.setStyleSheet("color:#64748b;margin:4px 0 10px 0;")
        disabled_lay.addWidget(disabled_desc)
        disabled_actions = QHBoxLayout()
        disabled_actions.addStretch(1)
        btn_enable_download = QPushButton("Bật tải YouTube")
        btn_enable_download.setStyleSheet(
            "background:#2563eb;color:white;font-weight:600;border-color:#2563eb;")
        btn_enable_download.clicked.connect(
            lambda: self.chk_download_enabled.setChecked(True))
        disabled_actions.addWidget(btn_enable_download)
        btn_local_video = QPushButton("Nhập video")
        btn_local_video.clicked.connect(self._open_queue_import_video)
        disabled_actions.addWidget(btn_local_video)
        btn_local_folder = QPushButton("Nhập thư mục")
        btn_local_folder.clicked.connect(self._open_queue_import_folder)
        disabled_actions.addWidget(btn_local_folder)
        disabled_actions.addStretch(1)
        disabled_lay.addLayout(disabled_actions)
        disabled_lay.addStretch(1)
        lay.addWidget(self.download_disabled, 1)
        self._apply_download_mode()
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
        if self.cfg.download.cookies_file:
            ck = f"file {Path(self.cfg.download.cookies_file).name}"
        else:
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
        make_columns_resizable(
            self.tbl_channels, [1.5, 4.0, 1.1], min_width=100)
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
        return SourceController.valid_reference(ref)

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
        try:
            change = self._sources.add(name, ref)
        except SourceError:
            QMessageBox.information(self, "Nguồn đã tồn tại", "Địa chỉ này đã có trong danh sách theo dõi.")
            return
        # channel_id nếu người dùng dán thẳng UC...; ngược lại coi là url/handle để resolve khi quét
        ch = change.channel
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
        try:
            self._sources.update(idx, name, ref)
        except SourceError:
            QMessageBox.information(self, "Nguồn đã tồn tại", "Địa chỉ này đã có trong danh sách theo dõi.")
            return
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
        removed = self._sources.remove(idx)
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
            f"| Tinh chỉnh âm thanh: {getattr(e.audio, 'enhance_original_voice', False)} "
            f"| Voiceover: {bool(e.audio.voiceover)} | Pitch: {e.audio.pitch_shift_semitones} "
            f"| Audio speed: {e.audio.audio_speed}\n"
            f"Xuất: full={e.export.make_full}, short={e.export.short_seconds}s={e.export.make_short}, "
            f"txt={e.export.make_content_txt} | codec={e.export.video_codec}\n"
            f"Phụ đề: {e.subtitle.enabled}"
            + (f" (dịch {e.subtitle.translate_to or 'gốc'}, {e.subtitle.position}, cỡ {e.subtitle.font_size})"
               if e.subtitle.enabled else "")
            + (f" | Edge TTS: {e.tts.voice or e.tts.gender or 'tự động'}"
               if e.tts.enabled else "")
            + f"\nThư mục xuất: {e.output_dir}"
        )

    def _build_edit_tab(self) -> QWidget:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        # Giữ label ẩn để các callback cũ tiếp tục tương thích.
        self.lbl_edit_summary = QLabel(self._edit_summary()); self.lbl_edit_summary.hide()

        # Header cố định: thư mục xuất và trạng thái lưu luôn nhìn thấy.
        header = QHBoxLayout(); header.setSpacing(6)
        header.addWidget(QLabel("<b>Cài đặt biên tập</b>"))
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
        self.btn_save_editor = QPushButton("Lưu cài đặt")
        self.btn_save_editor.setStyleSheet("font-weight:600;")
        self.btn_save_editor.clicked.connect(self.on_save_editor_settings)
        header.addWidget(self.btn_save_editor)
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
            ("fast", "Fast — ưu tiên tốc độ"),
            ("medium", "Medium — cân bằng (khuyên dùng)"),
            ("slow", "Slow — chất lượng/dung lượng tốt"),
            ("slower", "Slower — rất chậm"),
        ], getattr(self.cfg.editor.export, "encoder_preset", "medium"))
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
        self.cmb_long_video_split = self._data_combo([
            (0, "Không chia video dài"),
            (4, "Chia thành từng phần khoảng 4 phút"),
            (5, "Chia thành từng phần khoảng 5 phút"),
        ], int(getattr(self.cfg.editor, "long_video_segment_minutes", 0)))
        self.cmb_long_video_split.setToolTip(
            "Chỉ áp dụng khi video dài hơn 10 phút. Mỗi phần được edit độc lập "
            "để giảm RAM dùng cho tách âm thanh và nhận diện lời nói.")
        self._bind_combo(self.cmb_long_video_split, self.cfg.editor,
                         "long_video_segment_minutes", "Chia video dài")
        bform.addRow("Video dài hơn 10 phút", self.cmb_long_video_split)

        self.chk_auto_edit_new = QCheckBox(
            "Tự động bắt đầu biên tập khi có video mới")
        self.chk_auto_edit_new.setChecked(
            bool(self.cfg.editor.auto_edit_after_download))
        self.chk_auto_edit_new.setToolTip(
            "Áp dụng cho video vừa tải và video mới trong thư mục theo dõi. "
            "Tắt để chỉ đưa video vào Hàng đợi và tự bấm Bắt đầu khi sẵn sàng.")
        self._bind_bool(
            self.chk_auto_edit_new, self.cfg.editor,
            "auto_edit_after_download", "Tự động xử lý video mới")
        bform.addRow("Luồng tự động", self.chk_auto_edit_new)

        self.chk_make_short = QCheckBox("Xuất thêm một video ngắn")
        self.chk_make_short.setChecked(bool(self.cfg.editor.export.make_short))
        self.chk_make_short.setToolTip(
            "Tắt để chỉ xuất video chỉnh sửa đầy đủ. Ứng dụng sẽ bỏ lần mã hóa "
            "video ngắn, giúp hoàn thành nhanh hơn.")
        self.chk_make_short.toggled.connect(self._on_make_short_toggled)
        bform.addRow("Video ngắn", self.chk_make_short)

        self.short_options = QGroupBox("Tùy chọn video ngắn")
        short_form = QFormLayout(self.short_options)
        self.cmb_short_seconds = QComboBox()
        current_short_seconds = int(self.cfg.editor.export.short_seconds)
        common_durations = (15, 30, 45, 60, 90, 120)
        if current_short_seconds not in common_durations:
            self.cmb_short_seconds.addItem(
                f"Đang dùng · {current_short_seconds} giây",
                current_short_seconds)
        for seconds in common_durations:
            suffix = "phút" if seconds % 60 == 0 else "giây"
            amount = seconds // 60 if suffix == "phút" else seconds
            label = f"{amount} {suffix}"
            if seconds == 60:
                label += " · phổ biến"
            self.cmb_short_seconds.addItem(label, seconds)
        self.cmb_short_seconds.addItem("Tùy chọn…", "custom")
        self.cmb_short_seconds.setCurrentIndex(
            max(0, self.cmb_short_seconds.findData(current_short_seconds)))
        self._last_short_seconds = current_short_seconds
        self.cmb_short_seconds.currentIndexChanged.connect(
            self._on_short_duration_changed)
        self.cmb_short_seconds.setToolTip(
            "Chọn nhanh thời lượng phổ biến hoặc dùng Tùy chọn để nhập từ 10–300 giây.")
        short_form.addRow("Thời lượng", self.cmb_short_seconds)
        self.cmb_shortmode = self._data_combo(
            [("start", "Lấy từ đầu video"),
             ("highlight", "Tự chọn đoạn sôi động nhất")],
            self.cfg.editor.export.short_mode)
        self.cmb_shortmode.setToolTip(
            "Đoạn đầu xử lý nhanh hơn. Đoạn sôi động cần phân tích âm thanh nguồn.")
        self._bind_combo(
            self.cmb_shortmode, self.cfg.editor.export, "short_mode",
            "Cách chọn video ngắn")
        short_form.addRow("Chọn nội dung", self.cmb_shortmode)
        self.short_options.setVisible(bool(self.cfg.editor.export.make_short))
        bform.addRow(self.short_options)

        self.lbl_short_speed_hint = QLabel(
            "Tắt video ngắn: chỉ mã hóa 1 lần và chỉ lưu bản video đã chỉnh sửa.")
        self.lbl_short_speed_hint.setWordWrap(True)
        self.lbl_short_speed_hint.setStyleSheet("color:#15803d;")
        self.lbl_short_speed_hint.setVisible(not self.cfg.editor.export.make_short)
        bform.addRow("", self.lbl_short_speed_hint)
        nav = QListWidget()
        nav.setMinimumWidth(125)
        nav.setMaximumWidth(155)
        nav.setStyleSheet("QListWidget::item { min-height: 34px; padding: 4px 10px; }")
        nav.addItems([
            "Cơ bản", "Hình ảnh", "Âm thanh", "Phụ đề & dịch", "Thương hiệu",
            "Che nội dung cũ",
        ])
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
        # Công cụ che chữ/logo/phụ đề có sẵn là một quy trình riêng. Widget được
        # dựng cùng nhóm hình ảnh để dùng chung cấu hình, nhưng hiển thị ở trang
        # độc lập ngay dưới Thương hiệu để không làm thay đổi bố cục tab Hình ảnh.
        add_page(self.mask_settings_widget)
        # Cả tab Âm thanh (ô Lồng tiếng) và Phụ đề (chk_sub) đã dựng xong -> đồng bộ lại
        # để ô 'Ngôn ngữ lồng tiếng' khóa đúng khi phụ đề bật (giọng bám theo 'Dịch sang').
        self._sync_voiceover_controls()
        nav.currentRowChanged.connect(
            lambda index: self._on_editor_settings_page_changed(index, stack))
        preview_box = QGroupBox("Xem trước")
        self.preview_box = preview_box
        pv = QVBoxLayout(preview_box)
        self.lbl_live_preview = _InteractivePreview(
            "Bấm “Tạo xem trước” để tạo khung hình\n\n"
            "Nếu chưa chọn thư mục nguồn, ứng dụng dùng video đang xử lý "
            "hoặc đang chọn trong Hàng đợi.")
        self.lbl_live_preview.clicked.connect(self._on_inline_position_click)
        self.lbl_live_preview.mask_changed.connect(self._on_preview_mask_changed)
        self.lbl_live_preview.mask_selected.connect(self._on_preview_mask_selected)
        self.lbl_live_preview.mask_edit_finished.connect(
            self._on_preview_mask_edit_finished)
        self.lbl_live_preview.setMinimumSize(260, 220)
        self.lbl_live_preview.setStyleSheet(
            "background:#0f172a;color:#cbd5e1;border-radius:6px;padding:12px;")
        self._on_subtitle_safe_frame_changed()
        pv.addWidget(self.lbl_live_preview, 1)
        position_row = QHBoxLayout()
        self.cmb_preview_mode = self._data_combo([
            ("result", "Kết quả"), ("edit", "Chỉnh khung")], "result")
        self.cmb_preview_mode.setMaximumWidth(112)
        self.cmb_preview_mode.setToolTip(
            "Kết quả: xem khung hình sạch. Chỉnh khung: hiện viền và tay nắm của đối tượng đang chỉnh.")
        self.cmb_preview_mode.currentIndexChanged.connect(
            self._on_preview_mode_changed)
        position_row.addWidget(self.cmb_preview_mode)
        # Mục tiêu đặt vị trí được suy ra từ trang cài đặt đang mở. Không cho
        # người dùng chọn lại ở đây vì sẽ tạo hai nguồn điều khiển cùng một giá trị.
        self.lbl_preview_target = QLabel("Đang chỉnh:")
        position_row.addWidget(self.lbl_preview_target)
        self.lbl_preview_target.hide()
        self.cmb_preview_target = QComboBox()
        self.cmb_preview_target.addItem("Phụ đề", "subtitle")
        self.cmb_preview_target.addItem("Logo", "logo")
        self.cmb_preview_target.addItem("Hook", "hook")
        self.cmb_preview_target.addItem("CTA", "cta")
        self.cmb_preview_target.addItem("Vùng che", "mask")
        self.cmb_preview_target.currentIndexChanged.connect(self._on_preview_target_changed)
        self.cmb_preview_target.hide()  # chỉ giữ làm trạng thái nội bộ
        self.lbl_preview_context = QLabel("Phụ đề")
        self.lbl_preview_context.setStyleSheet("font-weight:600;color:#334155;")
        position_row.addWidget(self.lbl_preview_context)
        self.lbl_preview_context.hide()
        self.lbl_preview_position = QLabel("Click lên ảnh")
        self.lbl_preview_position.setStyleSheet("color:#64748b;")
        # Cho phần mô tả co lại trước, giữ nguyên chiều rộng của nút hành động.
        self.lbl_preview_position.setMinimumWidth(0)
        self.lbl_preview_position.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        position_row.addWidget(self.lbl_preview_position, 1)
        self.sp_preview_time = self._spin(QDoubleSpinBox, 0, 86400, 1, 1)
        self.sp_preview_time.setSuffix(" s")
        self.sp_preview_time.setMaximumWidth(78)
        self.sp_preview_time.setToolTip("Khung thời gian dùng để kiểm tra vùng che và các lớp chữ.")
        position_row.addWidget(self.sp_preview_time)
        self.btn_preview_refresh = QPushButton("Xem trước bản nháp")
        self.btn_preview_refresh.setMinimumWidth(142)
        self.btn_preview_refresh.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.btn_preview_refresh.setToolTip(
            "Tạo một khung hình theo đúng cấu hình hiện tại. "
            "Xử lý nhiều video được thực hiện trong tab Hàng đợi.")
        self.btn_preview_refresh.clicked.connect(lambda: self.on_preview(batch=False))
        position_row.addWidget(self.btn_preview_refresh)
        pv.addLayout(position_row)
        ebrand = self.cfg.editor
        for key, pos in (("subtitle", ebrand.subtitle.position),
                         ("logo", ebrand.overlay.position),
                         ("hook", ebrand.intro_hook.position),
                         ("cta", ebrand.outro_cta.position)):
            fx, fy = self._position_point(key, pos)
            self.lbl_live_preview.set_marker(key, fx, fy)
        self.lbl_live_preview.set_active("subtitle")
        self._sync_masks_to_preview()
        self._on_preview_target_changed()
        self._on_preview_mode_changed()
        preview_box.setMinimumWidth(280)
        preview_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        settings_host = QWidget()
        settings_lay = QHBoxLayout(settings_host)
        settings_lay.setContentsMargins(0, 0, 0, 0)
        settings_lay.setSpacing(8)
        settings_lay.addWidget(nav)
        settings_lay.addWidget(stack, 1)
        self.edit_splitter = QSplitter(Qt.Horizontal)
        self.edit_splitter.setChildrenCollapsible(False)
        self.edit_splitter.addWidget(settings_host)
        self.edit_splitter.addWidget(preview_box)
        self.edit_splitter.setStretchFactor(0, 3)
        self.edit_splitter.setStretchFactor(1, 2)
        self.edit_splitter.setSizes([700, 430])
        lay.addWidget(self.edit_splitter, 1)

        # Footer cố định: nguồn đầu vào và hành động chạy.
        frow = QHBoxLayout()
        source_label = QLabel("Thư mục theo dõi:")
        source_label.setToolTip(
            "Khác với 'Nhập thư mục' trong Hàng đợi: thư mục này được theo dõi "
            "liên tục và video mới sẽ tự động được thêm.")
        frow.addWidget(source_label)
        self.lbl_input = QLabel(self.cfg.editor.input_folder or "(chưa chọn)")
        self.lbl_input.setWordWrap(True)
        frow.addWidget(self.lbl_input, 1)
        btn_pick = QPushButton("Chọn thư mục theo dõi…")
        btn_pick.setToolTip(
            "Theo dõi liên tục thư mục này. Nếu chỉ muốn thêm một lần, "
            "hãy dùng 'Nhập thư mục' trong tab Hàng đợi.")
        btn_pick.clicked.connect(self.on_pick_input_folder)
        frow.addWidget(btn_pick)
        lay.addLayout(frow)
        # currentRow được đặt trước khi nối signal, vì vậy cần đồng bộ trạng thái
        # preview lần đầu để trang Cơ bản không hiện điều khiển đặt vị trí thừa.
        self._on_editor_settings_page_changed(nav.currentRow(), stack)
        return root

    # ---------------- Tab Lịch sử / Xuất bản ----------------
    _EXPORT_HDR = ["Video ID", "Tên video", "Kênh", "Tệp đã xuất", "Thư mục", "Thời gian"]
    _EVENT_HDR = ["Thời gian", "Trạng thái", "Nguồn", "Nội dung"]

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
        export_tools = QHBoxLayout()
        self.exports_hint = QLabel("Các video đã xuất thành công. Bấm đúp một dòng để mở thư mục kết quả.")
        self.exports_hint.setStyleSheet("color:#64748b;")
        export_tools.addWidget(self.exports_hint, 1)
        self.exports_search = QLineEdit()
        self.exports_search.setPlaceholderText("Tìm video hoặc kênh…")
        self.exports_search.setClearButtonEnabled(True)
        self.exports_search.setMaximumWidth(280)
        self.exports_search.setMinimumWidth(200)
        self.exports_search.textChanged.connect(self._apply_export_filter)
        export_tools.addWidget(self.exports_search)
        exports_lay.addLayout(export_tools)
        self.tbl_exports = QTableWidget(0, len(self._EXPORT_HDR))
        self.tbl_exports.setHorizontalHeaderLabels(self._EXPORT_HDR)
        self.tbl_exports.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_exports.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_exports.setAlternatingRowColors(True); self.tbl_exports.setShowGrid(False)
        self.tbl_exports.verticalHeader().setMinimumSectionSize(32)
        self.tbl_exports.verticalHeader().setDefaultSectionSize(32)
        self.tbl_exports.cellDoubleClicked.connect(self._open_export_dir)
        make_columns_resizable(
            self.tbl_exports, [1.15, 2.8, 1.2, 1.35, 1.0, 1.4],
            min_width=80)
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
        self.history_search.setMinimumWidth(250)
        self.history_search.textChanged.connect(self._apply_event_filters); filters.addWidget(self.history_search)
        self.history_level = QComboBox()
        self.history_level.addItem("Tất cả trạng thái", "all")
        self.history_level.addItem("Bắt đầu", "started")
        self.history_level.addItem("Hoàn thành", "completed")
        self.history_level.addItem("Đã dừng", "stopped")
        self.history_level.addItem("Lỗi", "error")
        self.history_level.addItem("Thông tin khác", "info")
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
        make_columns_resizable(
            self.tbl_events, [1.35, 0.8, 1.0, 4.5], min_width=100)
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
        self._history_video_titles = {
            str(row.get("video_id") or ""): str(row.get("title") or "")
            for row in self.db.all_video_rows()
            if row.get("video_id")
        }
        exports = list(reversed(self.db.recent_exports()))
        self._fill_exports(exports)
        self._history_events = list(reversed(self.db.recent_events()))
        current_source = self.history_source.currentData()
        sources = sorted({str(e.get("source") or "khác") for e in self._history_events})
        self.history_source.blockSignals(True); self.history_source.clear()
        self.history_source.addItem("Tất cả nguồn", "all")
        for source in sources:
            self.history_source.addItem(self._event_source_label(source), source)
        idx = self.history_source.findData(current_source)
        self.history_source.setCurrentIndex(max(0, idx)); self.history_source.blockSignals(False)
        self._apply_event_filters()
        errors = sum(str(e.get("level", "")).upper() == "ERROR" for e in self._history_events)
        self.lbl_history_summary.setText(f"{len(exports)} kết quả · {errors} lỗi")
        if not exports and self._history_events and self.history_pages.currentIndex() == 0:
            self.history_pages.setCurrentIndex(1)

    def _fill_exports(self, rows: list[dict]) -> None:
        self._export_rows = list(rows)
        self.tbl_exports.setRowCount(len(rows))
        for row, data in enumerate(rows):
            files = []
            for key, label in (("full_path", "Video"), ("short_path", "Short"),
                               ("content_txt", "Nội dung"), ("srt_path", "SRT")):
                if data.get(key):
                    files.append(label)
            values = [
                str(data.get("video_id") or ""),
                str(data.get("video_title") or self._history_video_titles.get(
                    str(data.get("video_id") or ""), "")),
                str(data.get("channel_name") or ""),
                " · ".join(files) if files else "—",
                "Mở thư mục",
                str(data.get("exported_at") or ""),
            ]
            tooltips = [
                values[0], values[1], values[2], values[3],
                str(data.get("output_dir") or ""), values[5],
            ]
            for col, shown in enumerate(values):
                item = QTableWidgetItem(shown)
                item.setToolTip(tooltips[col])
                if col in (3, 4, 5):
                    item.setTextAlignment(Qt.AlignCenter)
                self.tbl_exports.setItem(row, col, item)
            folder_host = QWidget()
            folder_layout = QHBoxLayout(folder_host)
            folder_layout.setContentsMargins(4, 1, 4, 1)
            folder_layout.setAlignment(Qt.AlignCenter)
            folder_button = QPushButton("Mở thư mục")
            folder_button.setFixedSize(96, 30)
            folder_button.setStyleSheet(
                "min-height:0px;max-height:30px;padding:1px 8px;")
            folder_button.clicked.connect(
                lambda _checked=False, current_row=row:
                self._open_export_dir(current_row, 4))
            folder_layout.addWidget(folder_button)
            self.tbl_exports.setCellWidget(row, 4, folder_host)
        self.lbl_exports_empty.setVisible(not rows)
        self.tbl_exports.setVisible(bool(rows))
        self._apply_export_filter()
        if rows:
            self.tbl_exports.scrollToTop()

    def _apply_export_filter(self, *_):
        if not hasattr(self, "tbl_exports"):
            return
        query = self.exports_search.text().strip().lower()
        for row in range(self.tbl_exports.rowCount()):
            text = " ".join(
                self.tbl_exports.item(row, col).text()
                for col in (0, 1, 2, 3)
                if self.tbl_exports.item(row, col) is not None
            ).lower()
            self.tbl_exports.setRowHidden(row, bool(query and query not in text))

    def _apply_event_filters(self, *_):
        if not hasattr(self, "_history_events"): return
        query = self.history_search.text().strip().lower()
        status = self.history_level.currentData(); source = self.history_source.currentData()
        rows = [e for e in self._history_events
                if (status == "all" or self._event_status(e)[0] == status)
                and (source == "all" or str(e.get("source") or "khác") == source)
                and (not query or query in self._event_message(e).lower())]
        display_rows = [{
            "time": event.get("time"),
            "status": self._event_status(event)[1],
            "source": self._event_source_label(event.get("source")),
            "message": self._event_message(event),
        } for event in rows]
        self._fill_table(self.tbl_events, display_rows, ["time", "status", "source", "message"])
        for row, event in enumerate(rows):
            is_error = str(event.get("level", "")).upper() == "ERROR"
            if is_error:
                for col in range(self.tbl_events.columnCount()):
                    self.tbl_events.item(row, col).setBackground(QColor("#fef2f2"))
                    self.tbl_events.item(row, col).setForeground(QColor("#991b1b"))
            for col in range(self.tbl_events.columnCount()):
                self.tbl_events.item(row, col).setToolTip(self._event_message(event))
        self.lbl_event_count.setText(f"Hiển thị {len(rows)}/{len(self._history_events)} sự kiện")

    @staticmethod
    def _event_source_label(source) -> str:
        return {
            "edit": "Biên tập", "export": "Xuất bản",
            "download": "Tải xuống", "scan": "Quét nguồn",
        }.get(str(source or "khác").lower(), str(source or "Khác").capitalize())

    @staticmethod
    def _event_status(event: dict) -> tuple[str, str]:
        message = str(event.get("message") or "").lower()
        if str(event.get("level") or "").upper() == "ERROR" or " lỗi" in message:
            return "error", "Lỗi"
        if "bắt đầu" in message:
            return "started", "Bắt đầu"
        if any(word in message for word in ("kết thúc", "hoàn thành", "tải xong")):
            return "completed", "Hoàn thành"
        if any(word in message for word in ("đã dừng", "tạm dừng")):
            return "stopped", "Đã dừng"
        return "info", "Thông tin"

    def _event_message(self, event: dict) -> str:
        message = str(event.get("message") or "")
        # Dữ liệu lịch sử cũ chỉ lưu ID; thay bằng tên khi video vẫn còn trong kho.
        for video_id, title in sorted(
                getattr(self, "_history_video_titles", {}).items(),
                key=lambda pair: len(pair[0]), reverse=True):
            if title and video_id in message:
                message = message.replace(video_id, title)
        return message

    def _show_event_detail(self, row: int, _col: int) -> None:
        level = self.tbl_events.item(row, 1).text() if self.tbl_events.item(row, 1) else ""
        source = self.tbl_events.item(row, 2).text() if self.tbl_events.item(row, 2) else ""
        message = self.tbl_events.item(row, 3).text() if self.tbl_events.item(row, 3) else ""
        QMessageBox.information(self, f"{level} · {source}", message)

    def _on_tab_changed(self, idx: int) -> None:
        if idx == 0:
            self._reload_table()
        elif idx == self._queue_idx:
            self._queue_tab.refresh()
        elif idx == self._history_idx:
            self._reload_history()

    def _on_queue_data_changed(self) -> None:
        """Keep the edit status on the Download tab synchronized with Queue/DB."""
        self._reload_table()
        if self.tabs.currentIndex() == self._history_idx:
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
        item = self.tbl_exports.item(row, 4)  # cột "Thư mục"
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
        self._scan_worker.progress.connect(self._on_download_progress)
        self._scan_worker.downloaded.connect(self._on_downloaded)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.start()

    def on_refresh_waiting(self) -> None:
        """Cập nhật danh sách: đối chiếu file thực tế + tìm video mới, rồi tải lại + biên tập.

        Video 'đã tải' nhưng mất file trong folder -> tải lại (YouTube) hoặc xóa (local).
        """
        if self._scan_worker and self._scan_worker.isRunning():
            self._log_dl("Đang có phiên quét, chờ hoàn tất…")
            return
        if not self.cfg.download.channels:
            removed = self.db.remove_incomplete_remote_videos()
            self._reload_table()
            self._queue_tab.refresh()
            self.lbl_download_activity.setText(
                f"● Đã cập nhật · xóa {removed} video chưa tải xong · giữ nguyên video local và đã tải")
            self.lbl_download_activity.setStyleSheet("color:#15803d;")
            self._log_dl(
                f"Nguồn theo dõi đang trống: đã dọn {removed} bản ghi tải chưa hoàn tất; "
                "giữ nguyên video local và video đã tải.")
            return
        if not self._download_space_ok():
            return
        self.btn_scan.setEnabled(False)
        self.btn_refresh_waiting.setEnabled(False)
        self.btn_stop_downloads.show()
        self.lbl_download_activity.setText("● Đang đối chiếu & cập nhật danh sách…")
        self.lbl_download_activity.setStyleSheet("color:#2563eb;")
        self._scan_worker = ScanWorker(self.cfg, self.db, reconcile=True)
        self._scan_worker.log.connect(self._log_dl)
        self._scan_worker.status.connect(self._on_status)
        self._scan_worker.progress.connect(self._on_download_progress)
        self._scan_worker.downloaded.connect(self._on_downloaded)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, stats: dict) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_refresh_waiting.setEnabled(True)
        self.btn_scan.setText("Quét ngay")
        self.btn_stop_downloads.hide(); self.btn_stop_downloads.setEnabled(True)
        failed = stats.get("failed", 0)
        cancelled = stats.get("cancelled", 0)
        stopped = stats.get("stopped", False)
        if stats.get("reconcile") and not stopped:
            self.lbl_download_activity.setText(
                f"● Đã đối chiếu · tải lại {stats.get('reconciled_reset', 0)} video mất file · "
                f"xóa {stats.get('reconciled_removed', 0)} video local · "
                f"thêm mới {stats.get('discovered', 0)}")
        elif stats.get("discover_only") and not stopped:
            self.lbl_download_activity.setText(
                f"● Đã cập nhật danh sách · thêm {stats.get('discovered', 0)} video")
        else:
            self.lbl_download_activity.setText(
                "● Đã dừng tải" if stopped else ("● Hoàn tất, có lỗi" if failed else "● Quét hoàn tất"))
        self.lbl_download_activity.setStyleSheet(
            "color:#b45309;" if stopped else ("color:#dc2626;" if failed else "color:#15803d;"))
        self._reload_table()
        # done được phát ngay trước khi QThread thoát hẳn; cập nhật thêm một lần
        # sau đó để nút tải-lại không bị giữ disabled bởi trạng thái worker cũ.
        QTimer.singleShot(100, self._reload_table)
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
        self._manual_worker.progress.connect(self._on_download_progress)
        self._manual_worker.done.connect(self._on_manual_done)
        self._manual_worker.start()

    def _on_manual_done(self, r: dict) -> None:
        self.btn_manual.setEnabled(True); self.btn_manual.setText("Tải video")
        if not (self._scan_worker and self._scan_worker.isRunning()):
            self.btn_scan.setEnabled(True)
            self.btn_refresh_waiting.setEnabled(True)
        self.btn_stop_downloads.setEnabled(True)
        if not (self._scan_worker and self._scan_worker.isRunning()): self.btn_stop_downloads.hide()
        self.lbl_download_activity.setText(
            "● Đã dừng tải" if r.get("cancelled") else ("● Tải hoàn tất" if r.get("ok") else "● Tải thất bại"))
        self.lbl_download_activity.setStyleSheet(
            "color:#b45309;" if r.get("cancelled") else ("color:#15803d;" if r.get("ok") else "color:#dc2626;"))
        self._reload_table()
        QTimer.singleShot(100, self._reload_table)
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

    def _redownload_one(self, video_id: str, url: str) -> None:
        """Ép tải lại 1 video lỗi: reset tải-lại-từ-đầu rồi tải (tự biên tập sau)."""
        if not url:
            QMessageBox.information(self, "Tải lại", "Video này không có nguồn để tải lại.")
            return
        self.db.reset_for_redownload(video_id)
        self._reload_table()
        self._download_from_table(url)   # ManualDownloadWorker -> _on_manual_done -> tự edit

    def on_redownload_errors(self) -> None:
        """Tải lại TOÀN BỘ video đang lỗi (bỏ qua video nhập từ máy) rồi biên tập lại."""
        if self._scan_worker and self._scan_worker.isRunning():
            self._log_dl("Đang có phiên quét, chờ hoàn tất…")
            return
        targets, local = [], 0
        for r in self.db.all_videos():
            if r.edit_status != "failed" and r.download_status != "failed":
                continue
            if str(r.video_id).startswith("local_"):
                local += 1
            else:
                targets.append(r)
        if not targets:
            msg = "Không có video lỗi nào có thể tải lại."
            if local:
                msg += f"\n({local} video nhập từ máy không tải lại được — hãy chép lại file.)"
            QMessageBox.information(self, "Tải lại video lỗi", msg)
            return
        for r in targets:
            self.db.reset_for_redownload(r.video_id)
        self._reload_table()
        note = f"Đã đặt lại {len(targets)} video lỗi để tải lại."
        if local:
            note += f" Bỏ qua {local} video nhập từ máy."
        self._log_dl(note)
        self.on_scan_clicked()   # tải pending + tự edit (downloaded -> _on_downloaded)

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
        self.btn_scan.setEnabled(False)
        self.btn_refresh_waiting.setEnabled(False)
        self.btn_resume_downloads.setText("Đang dừng video…")
        self.btn_resume_downloads.setVisible(True)
        self.btn_resume_downloads.setEnabled(False)
        self.lbl_download_activity.setText("● Đang dừng các video…")
        self.lbl_download_activity.setStyleSheet("color:#b45309;")

    def _resume_one_download(self, video_id: str, url: str) -> None:
        if self.db.resume_paused_downloads([video_id]):
            self.le_manual_url.setText(url)
            self.on_manual_download()

    def on_resume_all_downloads(self) -> None:
        count = self.db.resume_paused_downloads()
        if count:
            self._log_dl(f"Tải lại {count} video đã dừng.")
            self.lbl_download_activity.setText(
                f"● Đã đưa {count} video về hàng chờ tải lại…")
            self.lbl_download_activity.setStyleSheet("color:#2563eb;")
        # Một số video khác có thể vẫn đang thoát yt-dlp. Không khóa các video đã
        # chuyển hẳn sang paused: đưa chúng về pending ngay, rồi tự khởi chạy khi
        # phiên cũ đã nhả worker/khóa tải.
        if count or self._resume_downloads_pending:
            self._resume_downloads_pending = True
            self._start_resumed_downloads_when_idle()

    def _downloads_are_busy(self) -> bool:
        return bool(
            (self._scan_worker and self._scan_worker.isRunning())
            or (self._manual_worker and self._manual_worker.isRunning())
            or (getattr(self, "_auto", None) and self._auto.is_running())
        )

    def _start_resumed_downloads_when_idle(self) -> None:
        if not self._resume_downloads_pending:
            return
        if self._downloads_are_busy():
            self.btn_resume_downloads.setText("Đã xếp tải lại · đang chờ dừng xong…")
            self.btn_resume_downloads.setEnabled(False)
            QTimer.singleShot(250, self._start_resumed_downloads_when_idle)
            return
        self._resume_downloads_pending = False
        self._reload_table()
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
    _COOKIE_FILE_ITEM = "Chọn file cookie…"
    # (giá trị codec ffmpeg, nhãn hiển thị)
    _CODECS = [
        ("h264_qsv", "h264_qsv — GPU Intel Quick Sync (H.264)"),
        ("hevc_qsv", "hevc_qsv — GPU Intel Quick Sync (H.265/HEVC)"),
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
        self._refresh_mask_list()
        self._sync_masks_to_preview()
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        self._log_ed(f"Khung hình đầu ra: {text} — đang chờ lưu.")

    def _set_sep_row_visible(self, visible: bool) -> None:
        """Ẩn/hiện dòng 'Công nghệ tách giọng' (cả nhãn) theo lựa chọn âm thanh."""
        if not hasattr(self, "cmb_sep"):
            return
        self.cmb_sep.setVisible(visible)
        label = self._audio_form.labelForField(self.cmb_sep)
        if label is not None:
            label.setVisible(visible)

    def _sync_audio_mode_combo(self) -> None:
        """Đồng bộ ô 'Âm thanh đầu ra' theo config (dùng khi nơi khác đổi mute/tách)."""
        if not hasattr(self, "cmb_audio_mode"):
            return
        a = self.cfg.editor.audio
        mode = "mute" if a.mute_all else ("separate" if a.separate_speech else "keep")
        self.cmb_audio_mode.blockSignals(True)
        idx = self.cmb_audio_mode.findData(mode)
        if idx >= 0:
            self.cmb_audio_mode.setCurrentIndex(idx)
        self.cmb_audio_mode.blockSignals(False)
        self._set_sep_row_visible(mode == "separate")
        self._set_enhance_controls_enabled(
            bool(getattr(a, "enhance_original_voice", False)))

    def _set_enhance_controls_enabled(self, enabled: bool) -> None:
        for widget in getattr(self, "_enhance_audio_controls", []):
            widget.setEnabled(enabled)

    def _on_audio_mode_changed(self, index: int) -> None:
        mode = self.cmb_audio_mode.itemData(index) or "keep"
        a = self.cfg.editor.audio
        a.mute_all = (mode == "mute")
        a.separate_speech = (mode == "separate")
        self._set_sep_row_visible(mode == "separate")
        # 'Xóa hết âm thanh' mà đang bật lồng tiếng thì mâu thuẫn -> tắt lồng tiếng.
        if a.mute_all and self.cfg.editor.tts.enabled:
            self.cfg.editor.tts.enabled = False
            self._sync_voiceover_controls()   # ô 'Lồng tiếng' -> 'Không dùng'
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        names = {"keep": "giữ nguyên âm thanh gốc",
                 "separate": "tách & giữ lời thoại (cần requirements-ml.txt + GPU)",
                 "mute": "xóa hết âm thanh"}
        self._log_ed(f"Âm thanh đầu ra: {names.get(mode, mode)} — đang chờ lưu.")

    def _cookie_file_label(self) -> str:
        return f"📄 {Path(self.cfg.download.cookies_file).name}"

    def _sync_cookie_combo(self) -> None:
        """Dựng lại danh sách ô Cookie: các trình duyệt + [file đang dùng] + 'Chọn file cookie…'."""
        cf = (self.cfg.download.cookies_file or "").strip()
        cb = (self.cfg.download.cookies_from_browser or "").strip().lower()
        self.cmb_cookies.blockSignals(True)
        self.cmb_cookies.clear()
        self.cmb_cookies.addItems(self._BROWSERS)
        if cf:
            self.cmb_cookies.addItem(self._cookie_file_label())
        elif cb and cb not in self._BROWSERS:
            self.cmb_cookies.addItem(cb)
        self.cmb_cookies.addItem(self._COOKIE_FILE_ITEM)
        self.cmb_cookies.setCurrentText(
            self._cookie_file_label() if cf else (cb if cb else self._BROWSERS[0]))
        self.cmb_cookies.blockSignals(False)

    def _on_cookies_changed(self, text: str) -> None:
        if text == self._COOKIE_FILE_ITEM:
            path, _ = QFileDialog.getOpenFileName(
                self, "Chọn file cookie (Netscape .txt)", "",
                "Cookie (*.txt);;Tất cả (*.*)")
            if path:
                self.cfg.download.cookies_file = path
                self.cfg.download.cookies_from_browser = ""
                self._log_dl(f"Dùng file cookie: {path} — đã lưu.")
            self._sync_cookie_combo()   # hiện file vừa chọn, hoặc revert nếu bấm Hủy
            self.lbl_channels.setText(self._channels_summary())
            self._save_cfg()
            return
        if text.startswith("📄"):
            return                       # đang dùng file, chọn lại chính nó -> bỏ qua
        # Trình duyệt hoặc "(không dùng)": bỏ file, dùng cookie trình duyệt (hoặc none).
        val = "" if text.startswith("(") else text.strip()
        self.cfg.download.cookies_from_browser = val
        self.cfg.download.cookies_file = ""
        self.lbl_channels.setText(self._channels_summary())
        self._save_cfg()
        self._log_dl(f"Cookies: {val or '(không dùng)'} — đã lưu, giữ tới khi đổi lại.")

    def _on_codec_changed(self, idx: int) -> None:
        val = self.cmb_codec.itemData(idx)
        if not val:
            return
        self.cfg.editor.export.video_codec = val
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        self._log_ed(f"Codec xuất: {val} — đang chờ lưu.")

    def _on_ai_device_changed(self, idx: int) -> None:
        val = self.cmb_ai_device.itemData(idx) or "auto"
        self.cfg.editor.processing_device = val
        self._mark_settings_dirty()
        labels = {"auto": "Tự động (GPU lỗi sẽ chuyển CPU)",
                  "cuda": "GPU NVIDIA (CUDA)", "cpu": "CPU"}
        self._log_ed(f"Thiết bị xử lý AI: {labels.get(val, val)} — đang chờ lưu.")

    # ---------------- Cài đặt nâng cao ----------------
    def _set_cfg(self, obj, attr: str, value, label: str | None = None) -> None:
        setattr(obj, attr, value)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        if label:
            self._log_ed(f"{label}: {value} — đang chờ lưu.")

    def _mark_settings_dirty(self) -> None:
        self._settings_dirty = True
        if hasattr(self, "lbl_edit_save_status"):
            self.lbl_edit_save_status.setText("● Chưa lưu")
            self.lbl_edit_save_status.setStyleSheet("color:#b45309;")

    def on_save_editor_settings(self) -> bool:
        if not self.config_path:
            QMessageBox.warning(
                self, "Không thể lưu", "Ứng dụng chưa có đường dẫn file cấu hình.")
            return False
        try:
            save_config(self.cfg, self.config_path)
            self._saved_editor = deepcopy(self.cfg.editor)
            self._queue_tab.cfg.editor = deepcopy(self.cfg.editor)
            self._queue_tab.device = self.cfg.editor.processing_device
            if self._queue_tab._worker and self._queue_tab._worker.isRunning():
                self._queue_tab._worker.device = self.cfg.editor.processing_device
            self._settings_dirty = False
            self.lbl_edit_save_status.setText("✓ Đã lưu")
            self.lbl_edit_save_status.setStyleSheet("color:#15803d;")
            self._on_preview_target_changed()
            self._setup_input_folder_watch()
            self._log_ed("Đã lưu cài đặt; các job bắt đầu sau thời điểm này sẽ dùng cấu hình mới.")
            return True
        except Exception as ex:
            QMessageBox.warning(self, "Lỗi lưu cài đặt", str(ex))
            return False

    def _bind_bool(self, w, obj, attr, label=None):
        w.toggled.connect(lambda v: self._set_cfg(obj, attr, bool(v), label))

    def _on_make_short_toggled(self, enabled: bool) -> None:
        export_cfg = self.cfg.editor.export
        export_cfg.make_short = bool(enabled)
        # Giao diện này luôn xuất bản chỉnh sửa chính. Tắt short không được làm
        # mất cả hai đầu ra nếu một config cũ từng đặt make_full=False.
        export_cfg.make_full = True
        self.short_options.setVisible(bool(enabled))
        self.lbl_short_speed_hint.setVisible(not enabled)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        self._log_ed(
            "Đã bật video ngắn trong bản nháp."
            if enabled else
            "Đã tắt video ngắn trong bản nháp: khi lưu sẽ chỉ xuất bản chỉnh sửa đầy đủ.")

    def _on_short_duration_changed(self, index: int) -> None:
        value = self.cmb_short_seconds.itemData(index)
        if value == "custom":
            seconds, accepted = QInputDialog.getInt(
                self, "Thời lượng video ngắn",
                "Nhập thời lượng (giây):",
                int(self._last_short_seconds), 10, 300, 5)
            if not accepted:
                self.cmb_short_seconds.blockSignals(True)
                self.cmb_short_seconds.setCurrentIndex(
                    max(0, self.cmb_short_seconds.findData(
                        self._last_short_seconds)))
                self.cmb_short_seconds.blockSignals(False)
                return
            value = int(seconds)
            custom_index = self.cmb_short_seconds.findData(value)
            if custom_index < 0:
                custom_index = self.cmb_short_seconds.count() - 1
                self.cmb_short_seconds.insertItem(
                    custom_index, f"Tùy chọn · {value} giây", value)
            self.cmb_short_seconds.blockSignals(True)
            self.cmb_short_seconds.setCurrentIndex(custom_index)
            self.cmb_short_seconds.blockSignals(False)
        value = int(value)
        self._last_short_seconds = value
        self._set_cfg(
            self.cfg.editor.export, "short_seconds", value,
            "Thời lượng video ngắn")
        self.lbl_edit_summary.setText(self._edit_summary())

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
    # Ngôn ngữ ĐỌC (Edge TTS) — dùng locale cụ thể để chọn đúng chất giọng, gồm nhiều
    # biến thể tiếng Anh hay dùng trong video + Nhật/Hàn/Nga.
    _TTS_LANGS = [
        ("vi", "Tiếng Việt"),
        ("en-US", "English (US) · Anh-Mỹ"),
        ("en-GB", "English (UK) · Anh-Anh"),
        ("en-AU", "English (AU) · Anh-Úc"),
        ("en-CA", "English (CA) · Anh-Canada"),
        ("en-IN", "English (IN) · Anh-Ấn"),
        ("ja", "日本語 · Tiếng Nhật"),
        ("ko", "한국어 · Tiếng Hàn"),
        ("ru", "Русский · Tiếng Nga"),
        ("zh-CN", "中文 · Tiếng Trung"),
        ("fr", "Français · Pháp"), ("es", "Español · Tây Ban Nha"),
        ("de", "Deutsch · Đức"), ("th", "ไทย · Thái"),
    ]

    def _subtitle_color_control(
            self, obj, attr: str, label: str,
            presets: list[tuple[str, str]]) -> QComboBox:
        """Danh sách màu phổ biến; tùy chỉnh vẫn có nhưng không yêu cầu nhập mã màu."""
        combo = QComboBox()
        current = str(getattr(obj, attr, "#FFFFFF")).upper()
        values = {value.upper() for value, _name in presets}
        if current not in values:
            combo.addItem(f"Đang dùng · {current}", current)
            combo.setItemData(0, QColor(current), Qt.DecorationRole)
        for value, name in presets:
            index = combo.count()
            value = value.upper()
            combo.addItem(f"{name} · {value}", value)
            combo.setItemData(index, QColor(value), Qt.DecorationRole)
        combo.addItem("Tùy chỉnh…", "__custom__")
        selected = combo.findData(current)
        combo.setCurrentIndex(max(0, selected))
        combo.setToolTip(
            "Chọn một màu dựng sẵn. Mục Tùy chỉnh mở bảng màu và không cần nhập mã.")

        def select_color(index: int) -> None:
            value = combo.itemData(index)
            if value == "__custom__":
                old = str(getattr(obj, attr, current)).upper()
                initial = QColor(old)
                color = QColorDialog.getColor(
                    initial if initial.isValid() else QColor("#FFFFFF"),
                    self, label)
                if not color.isValid():
                    combo.blockSignals(True)
                    combo.setCurrentIndex(max(0, combo.findData(old)))
                    combo.blockSignals(False)
                    return
                value = color.name().upper()
                custom_index = combo.findData(value)
                if custom_index < 0:
                    custom_index = combo.count() - 1
                    combo.insertItem(custom_index, f"Tùy chỉnh · {value}", value)
                    combo.setItemData(custom_index, QColor(value), Qt.DecorationRole)
                combo.blockSignals(True)
                combo.setCurrentIndex(custom_index)
                combo.blockSignals(False)
            self._set_cfg(obj, attr, str(value).upper(), label)
            self._mark_preview_stale("subtitle")

        combo.currentIndexChanged.connect(select_color)
        return combo

    def _build_subtitle_group(self) -> QWidget:
        s = self.cfg.editor.subtitle
        g = QGroupBox("Phụ đề & dịch")
        form = QFormLayout(g)

        self.chk_sub = QCheckBox("Hiển thị phụ đề trên video")
        self.chk_sub.setChecked(s.enabled)
        self._bind_bool(self.chk_sub, s, "enabled", "Phụ đề")
        self.chk_sub.toggled.connect(
            lambda on: self._set_cfg(s, "burn_in", bool(on), "Ghi phụ đề lên video"))
        form.addRow("Bật phụ đề", self.chk_sub)

        self.chk_sub_replacement_box = QCheckBox(
            "Dùng chung khung che phụ đề cũ (khuyên dùng)")
        self.chk_sub_replacement_box.setChecked(
            bool(getattr(s, "replacement_box_enabled", False)))
        self.chk_sub_replacement_box.setToolTip(
            "Nền che phụ đề cũ và chữ phụ đề mới dùng chung một khung. "
            "Nền được vẽ trước, chữ được vẽ sau nên màu chữ không bị che.")
        self.chk_sub_replacement_box.toggled.connect(
            self._set_subtitle_replacement_box)
        form.addRow("Khung phụ đề thay thế", self.chk_sub_replacement_box)

        self.cmb_tr = self._data_combo(self._SUB_LANGS, s.translate_to)
        self._bind_combo(self.cmb_tr, s, "translate_to", "Dịch sang")
        form.addRow("Dịch sang (chỉ hiển thị bản dịch)", self.cmb_tr)

        self.cmb_subpos = self._data_combo(
            [("blur_bottom", "Vùng mờ phía dưới"),
             ("bottom", "Sát đáy"), ("middle", "Giữa"), ("top", "Trên")], s.position)
        self._bind_combo(self.cmb_subpos, s, "position", "Vị trí phụ đề")
        self.cmb_subpos.currentIndexChanged.connect(
            lambda _i: (
                self._on_preview_target_changed()
                if hasattr(self, "cmb_preview_target") else None,
                self._mark_preview_stale("subtitle"),
                self._refresh_overlay_warnings(),
            ))
        form.addRow("Vị trí dòng phụ đề", self.cmb_subpos)

        self.sp_subsize = self._spin(QSpinBox, 8, 72, s.font_size)
        self._bind_int(self.sp_subsize, s, "font_size", "Cỡ chữ phụ đề")
        self.sp_subsize.setToolTip(
            "Mặc định 14. Giá trị chỉ có hiệu lực với hàng đợi sau khi bấm "
            "'Lưu cài đặt'; preset không ghi đè cỡ chữ.")
        self.sp_subsize.valueChanged.connect(
            lambda _v: self._mark_preview_stale("subtitle"))
        form.addRow("Cỡ chữ phụ đề", self.sp_subsize)

        self.sub_font_color = self._subtitle_color_control(
            s, "font_color", "Chọn màu chữ phụ đề", [
                ("#FFFFFF", "Trắng"), ("#000000", "Đen"),
                ("#FFF200", "Vàng"), ("#00E5FF", "Xanh cyan"),
                ("#7CFF6B", "Xanh lá"), ("#FFB347", "Cam"),
                ("#FF8FCB", "Hồng"),
            ])
        form.addRow("Màu chữ", self.sub_font_color)
        self.sub_background_color = self._subtitle_color_control(
            s, "background_color", "Chọn màu nền phụ đề", [
                ("#000000", "Đen"), ("#FFFFFF", "Trắng"),
                ("#111827", "Xám đen"), ("#172554", "Xanh navy"),
                ("#3F1D1D", "Đỏ đậm"), ("#163A2A", "Xanh lá đậm"),
                ("#1E3A5F", "Xanh dương đậm"),
            ])
        form.addRow("Màu nền ô chữ", self.sub_background_color)

        opacity = float(getattr(s, "background_opacity", 0.55))
        self.sp_sub_background_opacity = self._data_combo([
            (0.0, "Trong suốt · 0%"),
            (0.25, "Rất nhẹ · 25%"),
            (0.40, "Nhẹ · 40%"),
            (0.50, "Cân bằng · 50%"),
            (0.55, "Khuyên dùng · 55%"),
            (0.70, "Đậm · 70%"),
            (0.85, "Rất đậm · 85%"),
        ], opacity)
        self.sp_sub_background_opacity.setToolTip(
            "Độ rõ của ô nền phụ đề. 50–55% phù hợp với phần lớn video.")
        self.sp_sub_background_opacity.currentIndexChanged.connect(
            lambda index: (
                self._set_cfg(
                    s, "background_opacity",
                    float(self.sp_sub_background_opacity.itemData(index)),
                    "Độ đậm nền phụ đề"),
                self._mark_preview_stale("subtitle"),
            ))
        form.addRow("Độ đậm nền", self.sp_sub_background_opacity)

        margins = QWidget()
        margin_grid = QGridLayout(margins)
        margin_grid.setContentsMargins(0, 0, 0, 0)
        margin_grid.setHorizontalSpacing(8)
        margin_grid.setVerticalSpacing(6)
        margin_specs = [
            ("Trái", "margin_left_percent", 0, 0),
            ("Phải", "margin_right_percent", 0, 2),
            ("Trên", "margin_top_percent", 1, 0),
            ("Dưới", "margin_bottom_percent", 1, 2),
        ]
        self.subtitle_margin_spins = {}
        for text, attr, row, col in margin_specs:
            spin = self._spin(QSpinBox, 0, 45, int(getattr(s, attr)))
            spin.setSuffix("%")
            spin.setSingleStep(1)
            spin.setToolTip(
                "Khoảng cách theo % kích thước video đầu ra. "
                "Khung nét đứt trên preview là vùng an toàn thực tế.")
            self._bind_int(spin, s, attr, f"Lề phụ đề {text.lower()}")
            spin.valueChanged.connect(
                lambda _value: self._on_subtitle_safe_frame_changed())
            margin_grid.addWidget(QLabel(text), row, col)
            margin_grid.addWidget(spin, row, col + 1)
            self.subtitle_margin_spins[attr] = spin
        form.addRow("Khung an toàn phụ đề", margins)

        children = (
            self.chk_sub_replacement_box,
            self.cmb_tr, self.cmb_subpos, self.sp_subsize,
            self.sub_font_color, self.sub_background_color,
            self.sp_sub_background_opacity, margins)
        for widget in children:
            widget.setEnabled(s.enabled)
        self.chk_sub.toggled.connect(
            lambda on: [widget.setEnabled(on) for widget in children])
        self.chk_sub.toggled.connect(lambda _on: self._refresh_overlay_warnings())
        # 'Lồng tiếng' đã chuyển sang tab Âm thanh. Ngôn ngữ đọc Edge TTS bám theo
        # 'Dịch sang', nên đồng bộ lại khi bật/tắt phụ đề hoặc đổi ngôn ngữ dịch.
        self.chk_sub.toggled.connect(lambda _on: self._sync_voiceover_controls())
        self.cmb_tr.currentIndexChanged.connect(lambda _i: self._filter_edge_voices())
        if self.chk_sub_replacement_box.isChecked():
            self._ensure_subtitle_replacement_mask()
        return g

    def _ensure_subtitle_replacement_mask(self) -> int:
        """Return/create the mask that is also the new subtitle container."""
        subtitle = self.cfg.editor.subtitle
        for index, mask in enumerate(self.cfg.editor.mask_regions):
            if mask.purpose == "old_subtitle":
                mask.timing_mode = "subtitle"
                mask.mode = "solid"
                mask.color = subtitle.background_color
                mask.opacity = subtitle.background_opacity
                mask.visible = True
                mask.linked_to_subtitle = True
                mask.subtitle_pad_before = 0.0
                mask.subtitle_pad_after = 0.0
                return index
        self.cfg.editor.mask_regions.append(MaskRegionCfg(
            name="Khung phụ đề thay thế", purpose="old_subtitle",
            mode="solid", x=.08, y=.76, width=.84, height=.14,
            color=subtitle.background_color,
            opacity=subtitle.background_opacity, timing_mode="subtitle",
            subtitle_pad_before=0.0, subtitle_pad_after=0.0,
            linked_to_subtitle=True))
        return len(self.cfg.editor.mask_regions) - 1

    def _set_subtitle_replacement_box(self, enabled: bool) -> None:
        subtitle = self.cfg.editor.subtitle
        self._set_cfg(subtitle, "replacement_box_enabled", bool(enabled),
                      "Khung phụ đề thay thế")
        if enabled:
            active = self._ensure_subtitle_replacement_mask()
            if hasattr(self, "cmb_preview_mode"):
                self._set_combo_data(self.cmb_preview_mode, "edit")
                self._on_preview_mode_changed()
            if hasattr(self, "lbl_live_preview"):
                self._sync_masks_to_preview(active)
            if hasattr(self, "cmb_preview_target"):
                target = self.cmb_preview_target.findData("mask")
                if target >= 0:
                    self.cmb_preview_target.setCurrentIndex(target)
            if hasattr(self, "lbl_live_preview"):
                self._sync_masks_to_preview(active)
            if hasattr(self, "lbl_preview_context"):
                self.lbl_preview_context.setText("Khung phụ đề thay thế")
        else:
            for mask in self.cfg.editor.mask_regions:
                if getattr(mask, "linked_to_subtitle", False):
                    mask.visible = False
            if hasattr(self, "cmb_preview_target"):
                target = self.cmb_preview_target.findData("subtitle")
                if target >= 0:
                    self.cmb_preview_target.setCurrentIndex(target)
            if hasattr(self, "cmb_preview_mode"):
                self._set_combo_data(self.cmb_preview_mode, "result")
                self._on_preview_mode_changed()
        self._mark_preview_stale("subtitle")
        if enabled and hasattr(self, "cmb_preview_target"):
            target = self.cmb_preview_target.findData("mask")
            if target >= 0:
                self.cmb_preview_target.setCurrentIndex(target)
            self._sync_masks_to_preview(active)
            self.lbl_preview_position.setText(
                "Kéo khung để di chuyển; kéo bốn góc để đổi kích thước · chưa lưu")
        self._mark_settings_dirty()

    def _tts_effective_language(self) -> str:
        # Ngôn ngữ lồng tiếng độc lập với ngôn ngữ phụ đề.
        return self.cmb_tts_lang.currentData() or self.cfg.editor.tts.language

    def _build_voiceover_controls(self, form) -> None:
        """Ô 'Lồng tiếng' (tab Âm thanh): gộp Edge TTS + file thu sẵn, loại trừ nhau."""
        a, tts = self.cfg.editor.audio, self.cfg.editor.tts
        self.cmb_voiceover_mode = self._data_combo(
            [("none", "Không dùng"), ("tts", "Tự đọc bằng Edge TTS"),
             ("file", "Chèn file thu sẵn")],
            "tts" if tts.enabled else ("file" if a.voiceover else "none"))
        self.cmb_voiceover_mode.currentIndexChanged.connect(self._on_voiceover_mode_changed)
        form.addRow("Lồng tiếng", self.cmb_voiceover_mode)

        self._tts_box = QWidget(); tf = QFormLayout(self._tts_box)
        tf.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Edge TTS đọc từ transcript và thay lời gốc. Ngôn ngữ lồng tiếng "
                      "được chọn độc lập với ngôn ngữ phụ đề.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#64748b;")
        tf.addRow("", hint)
        self.cmb_tts_lang = self._data_combo(self._TTS_LANGS, tts.language)
        self.cmb_tts_lang.currentIndexChanged.connect(
            lambda i: self._on_tts_filter_changed("language", self.cmb_tts_lang.itemData(i) or "vi"))
        tf.addRow("Ngôn ngữ lồng tiếng", self.cmb_tts_lang)
        self.cmb_tts_gender = self._data_combo(
            [("", "Tất cả nam và nữ"), ("Female", "Nữ"), ("Male", "Nam")], tts.gender)
        self.cmb_tts_gender.currentIndexChanged.connect(
            lambda i: self._on_tts_filter_changed("gender", self.cmb_tts_gender.itemData(i) or ""))
        tf.addRow("Giới tính giọng", self.cmb_tts_gender)
        voice_row = QWidget(); vlay = QHBoxLayout(voice_row)
        vlay.setContentsMargins(0, 0, 0, 0); vlay.setSpacing(6)
        self.cmb_tts_voice = QComboBox()
        self.cmb_tts_voice.addItem(tts.voice or "Đang tải danh sách giọng…", tts.voice)
        self.cmb_tts_voice.currentIndexChanged.connect(self._on_tts_voice_changed)
        vlay.addWidget(self.cmb_tts_voice, 1)
        self.btn_tts_voices = QPushButton("Tải lại")
        self.btn_tts_voices.clicked.connect(self._load_edge_voices)
        vlay.addWidget(self.btn_tts_voices)
        tf.addRow("Giọng đọc", voice_row)
        self.sp_tts_rate = self._spin(QSpinBox, -50, 100, tts.rate_percent, 5)
        self.sp_tts_rate.setSuffix("%")
        self.sp_tts_rate.setToolTip(
            "Nhịp đọc của giọng Edge TTS. 0% = bình thường; +50% đọc nhanh hơn, "
            "−50% chậm hơn. Đây là tốc độ ĐỌC, khác với 'Tốc độ âm thanh/video'.")
        self._bind_int(self.sp_tts_rate, tts, "rate_percent", "Tốc độ Edge TTS")
        tf.addRow("Tốc độ giọng", self.sp_tts_rate)
        form.addRow(self._tts_box)

        self._voice_file_box = QWidget(); vf = QFormLayout(self._voice_file_box)
        vf.setContentsMargins(0, 0, 0, 0)
        vf.addRow("File voiceover", self._audio_file_row("voiceover"))
        form.addRow(self._voice_file_box)

        self._edge_voices = []
        self._sync_voiceover_controls()

    def _sync_voiceover_controls(self) -> None:
        """Đồng bộ ô 'Lồng tiếng' + ẩn/hiện hộp con theo config (dùng khi init / nơi khác đổi)."""
        if not hasattr(self, "cmb_voiceover_mode"):
            return
        a, tts = self.cfg.editor.audio, self.cfg.editor.tts
        mode = "tts" if tts.enabled else ("file" if a.voiceover else "none")
        self.cmb_voiceover_mode.blockSignals(True)
        idx = self.cmb_voiceover_mode.findData(mode)
        if idx >= 0:
            self.cmb_voiceover_mode.setCurrentIndex(idx)
        self.cmb_voiceover_mode.blockSignals(False)
        self._apply_voiceover_mode(mode)

    def _apply_voiceover_mode(self, mode: str) -> None:
        """Ẩn/hiện hộp con theo mode ĐÃ CHỌN (không suy lại từ config)."""
        self._tts_box.setVisible(mode == "tts")
        self._voice_file_box.setVisible(mode == "file")
        if mode == "tts":
            self.cmb_tts_lang.setEnabled(True)
            self.cmb_tts_lang.setToolTip(
                "Chọn ngôn ngữ đọc; ô Giọng đọc sẽ lọc theo ngôn ngữ này.")
            self._filter_edge_voices()
            if hasattr(self, "_edge_voices") and not self._edge_voices:
                QTimer.singleShot(0, self._load_edge_voices)

    def _on_voiceover_mode_changed(self, index: int) -> None:
        mode = self.cmb_voiceover_mode.itemData(index) or "none"
        a, tts = self.cfg.editor.audio, self.cfg.editor.tts
        tts.enabled = (mode == "tts")
        if mode != "file":
            a.voiceover = ""                    # bỏ file khi không ở chế độ file
        if mode == "tts" and a.mute_all:        # TTS thay lời -> không thể 'Xóa hết âm thanh'
            a.mute_all = False
            self._sync_audio_mode_combo()
            self._log_ed("Đã tắt 'Xóa hết âm thanh' để giữ giọng lồng tiếng.")
        self._apply_voiceover_mode(mode)        # theo mode đã chọn (giữ hộp file khi chưa chọn file)
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        names = {"none": "không dùng", "tts": "Edge TTS tự đọc", "file": "file thu sẵn"}
        self._log_ed(f"Lồng tiếng: {names.get(mode, mode)} — đang chờ lưu.")

    def _on_tts_filter_changed(self, attr: str, value: str) -> None:
        self._set_cfg(self.cfg.editor.tts, attr, value)
        self._filter_edge_voices()

    def _on_tts_voice_changed(self, index: int) -> None:
        if index < 0:
            return
        value = self.cmb_tts_voice.itemData(index)
        if value is not None:
            self._set_cfg(self.cfg.editor.tts, "voice", str(value), "Giọng Edge TTS")

    def _load_edge_voices(self) -> None:
        if hasattr(self, "_edge_voice_worker") and self._edge_voice_worker.isRunning():
            return
        self.btn_tts_voices.setEnabled(False)
        self.btn_tts_voices.setText("Đang tải…")
        self._edge_voice_worker = _EdgeVoicesWorker(self)
        self._edge_voice_worker.loaded.connect(self._on_edge_voices_loaded)
        self._edge_voice_worker.failed.connect(self._on_edge_voices_failed)
        self._edge_voice_worker.start()

    def _on_edge_voices_loaded(self, voices: list) -> None:
        self._edge_voices = voices
        self.btn_tts_voices.setText("Tải lại")
        self.btn_tts_voices.setEnabled(self.cfg.editor.tts.enabled)
        self._filter_edge_voices()

    def _on_edge_voices_failed(self, message: str) -> None:
        self.btn_tts_voices.setText("Thử lại")
        self.btn_tts_voices.setEnabled(self.cfg.editor.tts.enabled)
        self.cmb_tts_voice.clear()
        self.cmb_tts_voice.addItem("Không tải được danh sách giọng", "")
        self.cmb_tts_voice.setToolTip(message)

    def _filter_edge_voices(self) -> None:
        if not hasattr(self, "cmb_tts_voice") or not hasattr(self, "_edge_voices"):
            return
        language = self._tts_effective_language().lower()
        gender = self.cmb_tts_gender.currentData() or ""
        current = self.cfg.editor.tts.voice
        voices = [
            voice for voice in self._edge_voices
            if (not language or str(voice.get("Locale", "")).lower().startswith(language))
            and (not gender or voice.get("Gender") == gender)
        ]
        self.cmb_tts_voice.blockSignals(True)
        self.cmb_tts_voice.clear()
        self.cmb_tts_voice.addItem("Tự động chọn giọng phù hợp", "")
        selected = 0
        for voice in voices:
            short = str(voice.get("ShortName", ""))
            sex = "Nữ" if voice.get("Gender") == "Female" else "Nam"
            locale = str(voice.get("Locale", ""))
            self.cmb_tts_voice.addItem(f"{locale} · {short} · {sex}", short)
            if short == current:
                selected = self.cmb_tts_voice.count() - 1
        self.cmb_tts_voice.setCurrentIndex(selected)
        self.cmb_tts_voice.blockSignals(False)
        if current and selected == 0 and self._edge_voices:
            self.cfg.editor.tts.voice = ""
            self._mark_settings_dirty()

    def _bind_text(self, w, obj, attr, label=None):
        w.editingFinished.connect(lambda: self._set_cfg(obj, attr, w.text(), label))

    def _after_cfg_change(self) -> None:
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()

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
        style = self._data_combo([
            ("minimal", "Tối giản · chữ có viền"),
            ("soft_box", "Nền mờ · dễ đọc"),
            ("highlight", "Nổi bật · chữ vàng"),
            ("title_bar", "Thanh nhấn · nền đậm"),
            ("custom", "Tùy chỉnh"),
        ], getattr(tcfg, "style_preset", "soft_box"))
        self._bind_combo(style, tcfg, "style_preset", f"Mẫu {prefix}")
        style.currentIndexChanged.connect(
            lambda _i, k=key: self._mark_preview_stale(k))
        form.addRow("Mẫu hiển thị", style); children.append(style)
        cmb = self._data_combo([("top", "Trên"), ("middle", "Giữa"), ("bottom", "Dưới")],
                               tcfg.position)
        self._bind_combo(cmb, tcfg, "position")
        cmb.currentIndexChanged.connect(lambda _i, k=key, c=cmb: self._on_brand_combo_position(k, c))
        self._pos_combos[key] = cmb
        form.addRow("Vị trí", cmb); children.append(cmb)
        durations = [2, 3, 4, 5] if is_hook else [3, 5, 7, 10]
        duration = QComboBox()
        current_seconds = float(tcfg.seconds)
        if current_seconds not in durations:
            duration.addItem(f"Đang dùng · {current_seconds:g} giây", current_seconds)
        for seconds in durations:
            duration.addItem(f"{seconds} giây", float(seconds))
        duration.addItem("Tùy chọn…", "custom")
        duration.setCurrentIndex(max(0, duration.findData(current_seconds)))

        def change_duration(index, combo=duration, obj=tcfg, name=prefix):
            value = combo.itemData(index)
            if value == "custom":
                value, accepted = QInputDialog.getDouble(
                    self, f"Thời lượng {name}", "Nhập thời lượng (giây):",
                    float(obj.seconds), 0.5, 60.0, 1)
                if not accepted:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(max(0, combo.findData(float(obj.seconds))))
                    combo.blockSignals(False)
                    return
                found = combo.findData(float(value))
                if found < 0:
                    found = combo.count() - 1
                    combo.insertItem(found, f"Tùy chọn · {value:g} giây", float(value))
                combo.blockSignals(True)
                combo.setCurrentIndex(found)
                combo.blockSignals(False)
            self._set_cfg(obj, "seconds", float(value), f"Thời lượng {name}")
            self._refresh_overlay_warnings()

        duration.currentIndexChanged.connect(change_duration)
        form.addRow("Thời gian hiển thị", duration); children.append(duration)
        spf = self._spin(QSpinBox, 16, 96, tcfg.font_size)
        self._bind_int(spf, tcfg, "font_size")
        spf.valueChanged.connect(lambda _v, k=key: self._mark_preview_stale(k))
        form.addRow("Cỡ chữ", spf); children.append(spf)
        chkb = QCheckBox("Nền hộp cho dễ đọc")
        chkb.setChecked(tcfg.box)
        self._bind_bool(chkb, tcfg, "box")
        form.addRow("Nền chữ (mẫu tùy chỉnh)", chkb); children.append(chkb)
        safe = self._data_combo([
            (0, "Không lề · 0%"), (3, "Mỏng · 3%"),
            (5, "An toàn · 5%"), (8, "Rộng · 8%"),
            (10, "Rất rộng · 10%"),
        ], int(getattr(tcfg, "safe_margin_percent", 5)))
        safe.setToolTip(
            "Khoảng cách của chữ Hook/CTA với mép video. "
            "Không thêm lề đen và không thay đổi kích thước hình ảnh.")
        self._bind_combo(safe, tcfg, "safe_margin_percent", f"Cách mép chữ {prefix}")
        safe.currentIndexChanged.connect(
            lambda _i, k=key: (
                self._sync_overlay_safe_frame(k),
                self._mark_preview_stale(k)))
        form.addRow("Cách mép chữ", safe); children.append(safe)
        warning = QLabel()
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "color:#9a6700;background:#fff8c5;border-radius:4px;padding:5px;")
        warning.setVisible(False)
        if not hasattr(self, "_overlay_warning_labels"):
            self._overlay_warning_labels = {}
        self._overlay_warning_labels[key] = warning
        form.addRow("", warning)
        for widget in children:
            widget.setEnabled(tcfg.enabled)
        chk.toggled.connect(lambda on, ws=children: [w.setEnabled(on) for w in ws])
        chk.toggled.connect(lambda _on: self._refresh_overlay_warnings())
        cmb.currentIndexChanged.connect(lambda _i: self._refresh_overlay_warnings())

        def sync_custom_controls():
            chkb.setEnabled(bool(tcfg.enabled) and style.currentData() == "custom")

        style.currentIndexChanged.connect(lambda _i: sync_custom_controls())
        chk.toggled.connect(lambda _on: sync_custom_controls())
        sync_custom_controls()

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
        self.cmb_logopos.currentIndexChanged.connect(
            lambda _i: self._refresh_overlay_warnings())
        form.addRow("Vị trí logo", self.cmb_logopos)
        self.sp_logoscale = self._spin(QDoubleSpinBox, 0.0, 0.5, ov.scale, 0.01)
        self._bind_float(self.sp_logoscale, ov, "scale", "Size logo")
        self.sp_logoscale.valueChanged.connect(
            lambda _v: self._mark_preview_stale("logo"))
        form.addRow("Kích thước (0.15 = 15%)", self.sp_logoscale)
        self.sp_logoop = self._spin(QDoubleSpinBox, 0.0, 1.0, ov.opacity, 0.05)
        self._bind_float(self.sp_logoop, ov, "opacity")
        form.addRow("Độ trong suốt", self.sp_logoop)
        logo_children = (logo_path, self.cmb_logopos, self.sp_logoscale, self.sp_logoop)
        for widget in logo_children:
            widget.setEnabled(ov.enabled)
        self.chk_logo.toggled.connect(
            lambda on: [widget.setEnabled(on) for widget in logo_children])
        self.chk_logo.toggled.connect(lambda _on: self._refresh_overlay_warnings())
        tabs.addTab(logo_page, "Logo")

        hook_page = QWidget(); hook_form = QFormLayout(hook_page)
        self._add_text_overlay_rows(hook_form, e.intro_hook, "Hook mở đầu", "hook", is_hook=True)
        tabs.addTab(hook_page, "Hook mở đầu")
        cta_page = QWidget(); cta_form = QFormLayout(cta_page)
        self._add_text_overlay_rows(cta_form, e.outro_cta, "CTA cuối video", "cta")
        tabs.addTab(cta_page, "CTA cuối video")
        self.brand_tabs = tabs
        tabs.currentChanged.connect(self._on_brand_tab_changed)
        self._refresh_overlay_warnings()
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
        self._mark_settings_dirty()

    def _clear_audio_file(self, attr: str, lbl: QLabel) -> None:
        setattr(self.cfg.editor.audio, attr, "")
        lbl.setText("(không)")
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()

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
            "16:9: co ngang theo %, cắt 2 bên rồi phóng; nền mờ lấp phần trên/dưới. "
            "1:1: cắt/phóng và dùng nền mờ. 9:16: cắt đều 4 cạnh, không thêm nền mờ. "
            "Preview giống video xuất.")
        crop_hint.setWordWrap(True)
        crop_hint.setContentsMargins(0, 0, 0, 0)
        crop_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        crop_hint.setStyleSheet("color:#64748b;")
        form.addRow("", crop_hint)
        self.chk_flip = QCheckBox("Lật hình theo chiều ngang")
        self.chk_flip.setChecked(e.flip_horizontal)
        self._bind_bool(self.chk_flip, e, "flip_horizontal", "Lật ngang")
        form.addRow("Lật ngang", self.chk_flip)
        # Tính năng mirror-crop cũ cắt nửa khung rồi nhân đôi, dễ bị hiểu nhầm là
        # lật ngang. Ngừng hiển thị và vô hiệu hóa; chk ẩn giữ tương thích reset cũ.
        e.mirror_crop = False
        self.chk_mirror = QCheckBox(); self.chk_mirror.setChecked(False)
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
        self.sp_squeeze = self._spin(QSpinBox, 0, 10, int(e.side_squeeze_percent))
        self.sp_squeeze.setSuffix("%")
        self.sp_squeeze.setToolTip(
            "CHỈ áp dụng cho video ĐÚNG 16:9: nén bớt chiều rộng TRƯỚC khi cắt 2 bên "
            "-> giữ nhiều nội dung hai bên hơn khi về 9:16 (đổi lại hình hơi thon).")
        self._bind_int(self.sp_squeeze, e, "side_squeeze_percent", "Co ngang")
        form.addRow("Co ngang trước cắt (16:9)", self.sp_squeeze)
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

        mask_box = QGroupBox("Các vùng che")
        mask_layout = QVBoxLayout(mask_box)
        hint = QLabel(
            "Che phụ đề, logo, chữ hoặc thông tin đã có trong video. Thêm một vùng, "
            "sau đó kéo hoặc thay đổi kích thước trực tiếp trên preview.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#64748b;")
        mask_layout.addWidget(hint)
        # Bốn tác vụ phổ biến luôn nhìn thấy, không giấu trong một dropdown.
        self.cmb_mask_preset = self._data_combo([
            ("old_subtitle", "Phụ đề cũ · khung ngang sát đáy"),
            ("old_logo", "Logo/watermark cũ · khung góc"),
            ("privacy", "Thông tin riêng tư · pixel hóa"),
            ("custom", "Vùng tùy chỉnh"),
        ], "old_subtitle")
        self.cmb_mask_preset.hide()
        preset_grid = QGridLayout(); preset_grid.setSpacing(6)
        for column, (label, preset) in enumerate((
                ("+ Phụ đề cũ", "old_subtitle"),
                ("+ Logo cũ", "old_logo"),
                ("+ Che thông tin", "privacy"),
                ("+ Vùng tùy chỉnh", "custom"))):
            button = QPushButton(label)
            button.setToolTip(f"Tạo nhanh vùng {label[2:].lower()} và chọn ngay trên preview.")
            button.clicked.connect(
                lambda _checked=False, value=preset: self._add_mask_preset(value))
            preset_grid.addWidget(button, 0, column)
        mask_layout.addLayout(preset_grid)
        self.lst_masks = QListWidget(); self.lst_masks.setMaximumHeight(128)
        self.lst_masks.currentRowChanged.connect(self._select_mask_region)
        mask_layout.addWidget(self.lst_masks)
        mask_actions = QHBoxLayout()
        self.chk_mask_visible = QCheckBox("Hiển thị")
        self.chk_mask_visible.toggled.connect(self._update_mask_controls)
        mask_actions.addWidget(self.chk_mask_visible); mask_actions.addStretch(1)
        self.chk_mask_locked = QCheckBox("Khóa vị trí")
        self.chk_mask_locked.toggled.connect(self._update_mask_controls)
        mask_actions.addWidget(self.chk_mask_locked)
        mask_layout.addLayout(mask_actions)
        mask_form = QFormLayout()
        self.txt_mask_name = QLineEdit()
        self.txt_mask_name.setPlaceholderText("Tên để nhận biết vùng che")
        self.txt_mask_name.editingFinished.connect(self._rename_mask_region)
        mask_form.addRow("Tên vùng", self.txt_mask_name)
        self.cmb_mask_mode = self._data_combo([
            ("blur", "Làm mờ"),
            ("pixelate", "Pixel hóa"),
            ("solid", "Phủ màu"),
        ], "blur")
        self.cmb_mask_mode.currentIndexChanged.connect(self._update_mask_controls)
        mask_form.addRow("Kiểu che", self.cmb_mask_mode)
        self.sp_mask_strength = self._spin(QSpinBox, 2, 40, 16)
        self.sp_mask_strength.setToolTip(
            "Làm mờ: bán kính làm mờ. Pixel hóa: kích thước ô. Mức 8–20 thường tự nhiên.")
        self.sp_mask_strength.valueChanged.connect(self._update_mask_controls)
        self.lbl_mask_strength = QLabel("Mức làm mờ")
        mask_form.addRow(self.lbl_mask_strength, self.sp_mask_strength)
        self.sp_mask_opacity = self._spin(QSpinBox, 10, 100, 80, 5)
        self.sp_mask_opacity.setSuffix("%")
        self.sp_mask_opacity.valueChanged.connect(self._update_mask_controls)
        self.lbl_mask_opacity = QLabel("Độ đậm")
        mask_form.addRow(self.lbl_mask_opacity, self.sp_mask_opacity)
        self.cmb_mask_color = self._data_combo([
            ("#000000", "Đen"), ("#111827", "Xanh đen"),
            ("#FFFFFF", "Trắng"), ("#374151", "Xám đậm"),
            ("#78350F", "Nâu đậm"), ("#1E3A8A", "Xanh đậm"),
        ], "#000000")
        self.cmb_mask_color.currentIndexChanged.connect(self._update_mask_controls)
        self.lbl_mask_color = QLabel("Màu phủ")
        mask_form.addRow(self.lbl_mask_color, self.cmb_mask_color)
        self.cmb_mask_timing = self._data_combo([
            ("subtitle", "Khi có phụ đề mới (khuyên dùng)"),
            ("full", "Toàn bộ video"),
            ("custom", "Khoảng thời gian tùy chỉnh"),
        ], "full")
        self.cmb_mask_timing.currentIndexChanged.connect(self._toggle_mask_time_range)
        mask_form.addRow("Thời điểm che", self.cmb_mask_timing)
        self.mask_subtitle_padding_widget = QWidget()
        padding_row = QHBoxLayout(self.mask_subtitle_padding_widget)
        padding_row.setContentsMargins(0, 0, 0, 0); padding_row.setSpacing(6)
        self.sp_mask_pad_before = self._spin(QDoubleSpinBox, 0, 2, .10, .05)
        self.sp_mask_pad_after = self._spin(QDoubleSpinBox, 0, 2, .15, .05)
        self.sp_mask_pad_before.setSuffix(" s"); self.sp_mask_pad_after.setSuffix(" s")
        self.sp_mask_pad_before.valueChanged.connect(self._update_mask_controls)
        self.sp_mask_pad_after.valueChanged.connect(self._update_mask_controls)
        padding_row.addWidget(QLabel("Trước")); padding_row.addWidget(self.sp_mask_pad_before)
        padding_row.addWidget(QLabel("Sau")); padding_row.addWidget(self.sp_mask_pad_after)
        mask_form.addRow("Đệm thời gian", self.mask_subtitle_padding_widget)
        self.mask_time_widget = QWidget()
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        self.sp_mask_start = self._spin(QDoubleSpinBox, 0, 86400, 0, .5)
        self.sp_mask_end = self._spin(QDoubleSpinBox, 0, 86400, 0, .5)
        self.sp_mask_start.setSuffix(" s"); self.sp_mask_end.setSuffix(" s")
        self.sp_mask_start.valueChanged.connect(self._update_mask_controls)
        self.sp_mask_end.valueChanged.connect(self._update_mask_controls)
        time_row.addWidget(QLabel("Từ")); time_row.addWidget(self.sp_mask_start)
        time_row.addWidget(QLabel("đến")); time_row.addWidget(self.sp_mask_end)
        self.mask_time_widget.setLayout(time_row)
        mask_form.addRow("Thời gian", self.mask_time_widget)
        self.lbl_mask_time_hint = QLabel("Mặc định áp dụng toàn bộ video.")
        self.lbl_mask_time_hint.setStyleSheet("color:#64748b;")
        mask_form.addRow("", self.lbl_mask_time_hint)
        self.cmb_mask_shape = self._data_combo([
            ("rectangle", "Chữ nhật tự do"), ("square", "Hình vuông"),
        ], "rectangle")
        self.cmb_mask_shape.currentIndexChanged.connect(self._update_mask_controls)
        mask_form.addRow("Hình dạng nâng cao", self.cmb_mask_shape)
        mask_layout.addLayout(mask_form)
        # Không chèn vào trang Hình ảnh. Trang thiết lập chính sẽ đặt widget này
        # thành một mục điều hướng độc lập ngay dưới "Thương hiệu".
        self.mask_settings_widget = mask_box
        self._refresh_mask_list()
        return g

    def _refresh_mask_list(self, selected: int | None = None) -> None:
        if not hasattr(self, "lst_masks"):
            return
        if selected is None:
            selected = self.lst_masks.currentRow()
        self.lst_masks.blockSignals(True); self.lst_masks.clear()
        modes = {"blur":"Làm mờ", "pixelate":"Vỡ hạt", "solid":"Phủ nền"}
        for index, mask in enumerate(self.cfg.editor.mask_regions):
            state = ("" if mask.visible else " · đang ẩn") + (" · đã khóa" if mask.locked else "")
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 28))
            self.lst_masks.addItem(item)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 1, 3, 1)
            row_layout.setSpacing(4)
            select_button = QPushButton(
                f"{index + 1}. {mask.name} · {modes.get(mask.mode, mask.mode)}{state}")
            select_button.setFlat(True)
            select_button.setStyleSheet(
                "QPushButton{text-align:left;border:0;background:transparent;padding:1px 2px;}"
                "QPushButton:hover{color:#1d4ed8;}")
            select_button.clicked.connect(
                lambda _checked=False, current=item: self.lst_masks.setCurrentItem(current))
            row_layout.addWidget(select_button, 1)
            delete_button = QPushButton("×")
            delete_button.setFixedSize(24, 22)
            delete_button.setToolTip("Xóa vùng che này")
            delete_button.setStyleSheet(
                "QPushButton{border:0;background:transparent;color:#64748b;"
                "font-size:17px;font-weight:600;padding:0;}"
                "QPushButton:hover{background:#fee2e2;color:#b91c1c;border-radius:4px;}")
            delete_button.clicked.connect(
                lambda _checked=False, current=item: self._delete_mask_list_item(current))
            row_layout.addWidget(delete_button)
            self.lst_masks.setItemWidget(item, row)
        if self.cfg.editor.mask_regions:
            selected = max(0, min(int(selected or 0), len(self.cfg.editor.mask_regions)-1))
            self.lst_masks.setCurrentRow(selected)
        self.lst_masks.blockSignals(False)
        self._select_mask_region(self.lst_masks.currentRow())

    def _add_mask_region(self) -> None:
        preset = self.cmb_mask_preset.currentData() or "custom"
        values = {
            "old_subtitle": dict(name="Phụ đề cũ", purpose=preset, mode="solid",
                                 x=.08, y=.78, width=.84, height=.14, strength=16,
                                 opacity=.80, timing_mode="subtitle",
                                 subtitle_pad_before=.10, subtitle_pad_after=.15),
            "old_logo": dict(name="Logo cũ", purpose=preset, mode="blur",
                             x=.73, y=.04, width=.23, height=.10, strength=16),
            "privacy": dict(name="Thông tin riêng tư", purpose=preset, mode="pixelate",
                            x=.30, y=.38, width=.40, height=.18, strength=12),
            "custom": dict(name="Vùng che tùy chỉnh", purpose=preset, mode="blur",
                           x=.30, y=.35, width=.40, height=.20, strength=16),
        }[preset]
        self.cfg.editor.mask_regions.append(MaskRegionCfg(**values))
        index = len(self.cfg.editor.mask_regions) - 1
        self._refresh_mask_list(index)
        self._mark_settings_dirty()
        if hasattr(self, "cmb_preview_target"):
            mask_item = self.cmb_preview_target.findData("mask")
            if mask_item >= 0: self.cmb_preview_target.setCurrentIndex(mask_item)
        self._sync_masks_to_preview(index)
        # Một lần bấm phải cho thấy ngay kết quả. Nếu preview chưa có ảnh, tự lấy
        # video đang xử lý/đang chọn thay vì bắt người dùng tìm thêm nút khác.
        if (hasattr(self, "lbl_live_preview")
                and self.lbl_live_preview._source.isNull()):
            QTimer.singleShot(0, lambda: self.on_preview(batch=False))

    def _add_mask_preset(self, preset: str) -> None:
        """Tạo vùng từ nút tác vụ nhanh và dùng chung luồng thêm hiện tại."""
        self._set_combo_data(self.cmb_mask_preset, preset)
        self._add_mask_region()

    def _duplicate_mask_region(self) -> None:
        index = self.lst_masks.currentRow() if hasattr(self, "lst_masks") else -1
        if not (0 <= index < len(self.cfg.editor.mask_regions)):
            return
        duplicate = deepcopy(self.cfg.editor.mask_regions[index])
        duplicate.name = f"{duplicate.name} (bản sao)"
        duplicate.x = min(max(0.0, 1.0 - duplicate.width), duplicate.x + .03)
        duplicate.y = min(max(0.0, 1.0 - duplicate.height), duplicate.y + .03)
        duplicate.locked = False
        self.cfg.editor.mask_regions.insert(index + 1, duplicate)
        self._refresh_mask_list(index + 1)
        self._mark_settings_dirty(); self._sync_masks_to_preview(index + 1)

    def _rename_mask_region(self) -> None:
        index = self.lst_masks.currentRow() if hasattr(self, "lst_masks") else -1
        if not (0 <= index < len(self.cfg.editor.mask_regions)):
            return
        name = self.txt_mask_name.text().strip()
        if not name:
            name = f"Vùng che {index + 1}"
            self.txt_mask_name.setText(name)
        if self.cfg.editor.mask_regions[index].name != name:
            self.cfg.editor.mask_regions[index].name = name
            self._mark_settings_dirty(); self._refresh_mask_list(index)
            self._sync_masks_to_preview(index)

    def _toggle_mask_time_range(self, _index: int = 0) -> None:
        timing = (self.cmb_mask_timing.currentData()
                  if hasattr(self, "cmb_mask_timing") else "full")
        custom = timing == "custom"
        if hasattr(self, "mask_time_widget"):
            self.mask_time_widget.setVisible(custom)
            self.mask_time_widget.setEnabled(custom)
        if hasattr(self, "mask_subtitle_padding_widget"):
            self.mask_subtitle_padding_widget.setVisible(timing == "subtitle")
        if hasattr(self, "lbl_mask_time_hint"):
            hints = {
                "subtitle": "Chỉ che theo từng câu SRT/ASS mới; phụ đề mới nằm phía trên.",
                "custom": "Nhập thời điểm bắt đầu và kết thúc.",
                "full": "Vùng che xuất hiện xuyên suốt video.",
            }
            self.lbl_mask_time_hint.setText(hints.get(timing, hints["full"]))
        if custom and self.sp_mask_end.value() <= self.sp_mask_start.value():
            self.sp_mask_end.setValue(self.sp_mask_start.value() + 5.0)
        self._update_mask_controls()

    def _delete_mask_region(self) -> None:
        index = self.lst_masks.currentRow() if hasattr(self, "lst_masks") else -1
        self._delete_mask_at(index)

    def _delete_mask_list_item(self, item: QListWidgetItem) -> None:
        """Xóa đúng vùng từ nút × của từng dòng, không phụ thuộc dòng đang chọn."""
        index = self.lst_masks.row(item) if hasattr(self, "lst_masks") else -1
        self._delete_mask_at(index)

    def _delete_mask_at(self, index: int) -> None:
        if 0 <= index < len(self.cfg.editor.mask_regions):
            linked_subtitle = bool(getattr(
                self.cfg.editor.mask_regions[index], "linked_to_subtitle", False))
            del self.cfg.editor.mask_regions[index]
            if linked_subtitle:
                self.cfg.editor.subtitle.replacement_box_enabled = False
                if hasattr(self, "chk_sub_replacement_box"):
                    self.chk_sub_replacement_box.blockSignals(True)
                    self.chk_sub_replacement_box.setChecked(False)
                    self.chk_sub_replacement_box.blockSignals(False)
            self._refresh_mask_list(min(index, len(self.cfg.editor.mask_regions)-1))
            self._mark_settings_dirty(); self._sync_masks_to_preview()

    def _select_mask_region(self, index: int) -> None:
        enabled = 0 <= index < len(self.cfg.editor.mask_regions)
        for name in ("txt_mask_name", "cmb_mask_mode", "cmb_mask_shape", "sp_mask_strength",
                     "sp_mask_opacity", "cmb_mask_color", "cmb_mask_timing",
                     "sp_mask_pad_before", "sp_mask_pad_after",
                     "sp_mask_start", "sp_mask_end", "chk_mask_visible", "chk_mask_locked"):
            if hasattr(self, name): getattr(self, name).setEnabled(enabled)
        if not enabled:
            self._sync_masks_to_preview(); return
        if hasattr(self, "cmb_preview_target"):
            mask_target = self.cmb_preview_target.findData("mask")
            if mask_target >= 0 and self.cmb_preview_target.currentData() != "mask":
                self.cmb_preview_target.setCurrentIndex(mask_target)
        mask = self.cfg.editor.mask_regions[index]
        controls = (self.txt_mask_name, self.cmb_mask_mode, self.cmb_mask_shape,
                    self.sp_mask_strength, self.sp_mask_opacity, self.cmb_mask_color,
                    self.cmb_mask_timing, self.sp_mask_pad_before, self.sp_mask_pad_after,
                    self.sp_mask_start, self.sp_mask_end, self.chk_mask_visible,
                    self.chk_mask_locked)
        for control in controls: control.blockSignals(True)
        self.txt_mask_name.setText(mask.name)
        self._set_combo_data(self.cmb_mask_mode, mask.mode)
        self._set_combo_data(self.cmb_mask_shape, mask.shape)
        self.sp_mask_strength.setValue(mask.strength)
        self.sp_mask_opacity.setValue(round(mask.opacity * 100))
        self._set_combo_data(self.cmb_mask_color, mask.color)
        self._set_combo_data(self.cmb_mask_timing, getattr(mask, "timing_mode", "full"))
        self.sp_mask_pad_before.setValue(
            float(getattr(mask, "subtitle_pad_before", .10)))
        self.sp_mask_pad_after.setValue(
            float(getattr(mask, "subtitle_pad_after", .15)))
        self.sp_mask_start.setValue(mask.start_seconds); self.sp_mask_end.setValue(mask.end_seconds)
        self.chk_mask_visible.setChecked(mask.visible)
        self.chk_mask_locked.setChecked(mask.locked)
        for control in controls: control.blockSignals(False)
        self._refresh_mask_context_controls(
            mask.mode, getattr(mask, "timing_mode", "full"))
        self._sync_masks_to_preview(index)

    def _update_mask_controls(self, *_args) -> None:
        if not hasattr(self, "lst_masks"):
            return
        index = self.lst_masks.currentRow()
        if not (0 <= index < len(self.cfg.editor.mask_regions)):
            return
        mask = self.cfg.editor.mask_regions[index]
        mask.mode = self.cmb_mask_mode.currentData() or "blur"
        mask.shape = self.cmb_mask_shape.currentData() or "rectangle"
        mask.strength = self.sp_mask_strength.value()
        mask.opacity = self.sp_mask_opacity.value() / 100.0
        mask.color = self.cmb_mask_color.currentData() or "#000000"
        timing = self.cmb_mask_timing.currentData() or "full"
        mask.timing_mode = timing
        mask.subtitle_pad_before = self.sp_mask_pad_before.value()
        mask.subtitle_pad_after = self.sp_mask_pad_after.value()
        mask.start_seconds = self.sp_mask_start.value() if timing == "custom" else 0.0
        mask.end_seconds = self.sp_mask_end.value() if timing == "custom" else 0.0
        mask.visible = self.chk_mask_visible.isChecked()
        mask.locked = self.chk_mask_locked.isChecked()
        if mask.shape == "square":
            aw, ah = (float(v) for v in self.cfg.editor.target_aspect.split(":"))
            mask.height = min(1.0 - mask.y, mask.width * aw / ah)
        self._refresh_mask_context_controls(mask.mode, timing)
        self._mark_settings_dirty(); self._refresh_mask_list(index)
        self._sync_masks_to_preview(index)

    def _refresh_mask_context_controls(self, mode: str, timing: str) -> None:
        solid = mode == "solid"
        pixel = mode == "pixelate"
        self.sp_mask_opacity.setVisible(solid)
        self.lbl_mask_opacity.setVisible(solid)
        self.cmb_mask_color.setVisible(solid)
        self.lbl_mask_color.setVisible(solid)
        self.sp_mask_strength.setVisible(not solid)
        self.lbl_mask_strength.setVisible(not solid)
        if not solid:
            self.lbl_mask_strength.setText("Kích thước hạt" if pixel else "Mức làm mờ")
        custom = timing == "custom"
        self.mask_time_widget.setVisible(custom)
        self.mask_time_widget.setEnabled(custom)
        self.mask_subtitle_padding_widget.setVisible(timing == "subtitle")
        hints = {
            "subtitle": "Chỉ che theo từng câu SRT/ASS mới; phụ đề mới nằm phía trên.",
            "custom": "Nhập thời điểm bắt đầu và kết thúc.",
            "full": "Vùng che xuất hiện xuyên suốt video.",
        }
        self.lbl_mask_time_hint.setText(hints.get(timing, hints["full"]))

    def _sync_masks_to_preview(self, active: int = -1) -> None:
        if hasattr(self, "lbl_live_preview"):
            self.lbl_live_preview.set_masks(self.cfg.editor.mask_regions, active)
            page = getattr(self, "_editor_settings_page_index", 0)
            if page == 5:
                allowed = None
            elif page == 3 and getattr(
                    self.cfg.editor.subtitle, "replacement_box_enabled", False):
                allowed = {
                    index for index, mask in enumerate(self.cfg.editor.mask_regions)
                    if getattr(mask, "linked_to_subtitle", False)
                    and getattr(mask, "visible", True)}
            else:
                allowed = set()
            self.lbl_live_preview.set_mask_context(allowed)

    def _on_preview_mode_changed(self, _index: int = 0) -> None:
        if not hasattr(self, "lbl_live_preview"):
            return
        editing = (hasattr(self, "cmb_preview_mode")
                   and self.cmb_preview_mode.currentData() == "edit")
        self.lbl_live_preview.set_edit_chrome(editing)
        page = getattr(self, "_editor_settings_page_index", 0)
        active = -1
        if editing and page == 3 and getattr(
                self.cfg.editor.subtitle, "replacement_box_enabled", False):
            active = self._ensure_subtitle_replacement_mask()
        elif editing and page == 5 and hasattr(self, "lst_masks"):
            active = self.lst_masks.currentRow()
        self._sync_masks_to_preview(active)
        if not editing:
            self.lbl_preview_position.setText("Bản xem trước sạch · giống video đầu ra")
        elif page == 3 and active >= 0:
            self.lbl_live_preview.set_active_mask(active)
            self.lbl_preview_position.setText(
                "Kéo khung để di chuyển; kéo góc để đổi kích thước")
        else:
            self._on_preview_target_changed()

    def _on_preview_mask_changed(self, index: int, x: float, y: float,
                                 width: float, height: float) -> None:
        if not (0 <= index < len(self.cfg.editor.mask_regions)):
            return
        mask = self.cfg.editor.mask_regions[index]
        if mask.shape == "square":
            aw, ah = (float(v) for v in self.cfg.editor.target_aspect.split(":"))
            height = min(1.0 - y, width * aw / ah)
        mask.x, mask.y, mask.width, mask.height = x, y, width, height
        self._sync_masks_to_preview(index)
        self._mark_settings_dirty()
        if hasattr(self, "lbl_preview_position"):
            self.lbl_preview_position.setText(
                f"{mask.name}: x {x:.0%}, y {y:.0%}, rộng {width:.0%}, cao {height:.0%} · đang cập nhật")

    def _on_preview_mask_edit_finished(self, index: int) -> None:
        """Thả chuột = tự lưu tọa độ và dựng lại preview, không cần nút Áp dụng."""
        if not self.config_path:
            return
        try:
            # Chỉ đưa danh sách vùng che vào cấu hình đã lưu. Những mục khác trong
            # tab Cài đặt vẫn giữ nguyên cơ chế bấm "Lưu cài đặt".
            self._saved_editor.mask_regions = deepcopy(self.cfg.editor.mask_regions)
            self._queue_tab.cfg.editor.mask_regions = deepcopy(
                self.cfg.editor.mask_regions)
            other_dirty = self.cfg.editor != self._saved_editor
            snapshot = deepcopy(self.cfg)
            snapshot.editor = deepcopy(self._saved_editor)
            save_config(snapshot, self.config_path)
            self._settings_dirty = other_dirty
            if other_dirty:
                self.lbl_edit_save_status.setText("● Cài đặt khác chưa lưu")
                self.lbl_edit_save_status.setStyleSheet("color:#b45309;")
            else:
                self.lbl_edit_save_status.setText("✓ Vùng che đã tự lưu")
                self.lbl_edit_save_status.setStyleSheet("color:#15803d;")
            if 0 <= index < len(self.cfg.editor.mask_regions):
                name = self.cfg.editor.mask_regions[index].name
                self.lbl_preview_position.setText(
                    f"{name} · đã tự lưu vị trí và kích thước")
            # Dựng lại hiệu ứng thật sau khi thả chuột; trong lúc kéo khung vẫn
            # chuyển động tức thời nên giao diện không bị giật ở từng pixel.
            QTimer.singleShot(150, lambda: self.on_preview(batch=False))
        except Exception as ex:
            self._settings_dirty = True
            self.lbl_edit_save_status.setText("● Chưa lưu được vùng che")
            self.lbl_edit_save_status.setStyleSheet("color:#b91c1c;")
            QMessageBox.warning(self, "Không thể tự lưu vùng che", str(ex))

    def _on_preview_mask_selected(self, index: int) -> None:
        if hasattr(self, "lst_masks"):
            self.lst_masks.setCurrentRow(index)

    def _build_audio_settings_group(self) -> QWidget:
        e, a = self.cfg.editor, self.cfg.editor.audio
        g = QGroupBox("Âm thanh")
        form = QFormLayout(g)
        self._audio_form = form
        self._enhance_audio_controls = []
        # Gộp "âm thanh đầu ra" + "tách lời thoại" thành 1 lựa chọn loại trừ nhau.
        self.cmb_audio_mode = self._data_combo(
            [("keep", "Giữ nguyên âm thanh gốc"),
             ("separate", "Tách & giữ lời thoại (bớt nhạc gốc)"),
             ("mute", "Xóa hết âm thanh")],
            "mute" if a.mute_all else ("separate" if a.separate_speech else "keep"))
        self.cmb_audio_mode.currentIndexChanged.connect(self._on_audio_mode_changed)
        form.addRow("Âm thanh đầu ra", self.cmb_audio_mode)
        self.cmb_sep = self._data_combo(
            [("mdx", "MDX — nhẹ, chạy CPU"), ("demucs", "Demucs — chất lượng cao, GPU"),
             ("vr", "VR Architecture")], a.separator_backend)
        self._bind_combo(self.cmb_sep, a, "separator_backend", "Công nghệ tách giọng")
        form.addRow("Công nghệ tách giọng", self.cmb_sep)
        self._set_sep_row_visible(bool(a.separate_speech))   # chỉ hiện khi 'Tách & giữ lời thoại'

        self.chk_enhance_audio = QCheckBox(
            "Bật xử lý cho nguồn âm thanh đang dùng (gốc, tách giọng hoặc lồng tiếng)")
        self.chk_enhance_audio.setChecked(
            bool(getattr(a, "enhance_original_voice", False)))
        self._bind_bool(
            self.chk_enhance_audio, a, "enhance_original_voice", "Tinh chỉnh âm thanh")
        self.chk_enhance_audio.toggled.connect(self._set_enhance_controls_enabled)
        form.addRow("Tinh chỉnh âm thanh", self.chk_enhance_audio)

        enhance_box = QGroupBox("Thông số tinh chỉnh")
        enhance_grid = QGridLayout(enhance_box)
        enhance_grid.setContentsMargins(10, 12, 10, 10)
        enhance_grid.setHorizontalSpacing(12)
        enhance_grid.setVerticalSpacing(7)
        enhance_grid.setColumnStretch(1, 1)
        enhance_grid.setColumnStretch(3, 1)
        enhance_hint = QLabel(
            "Di chuột vào từng thông số để xem tác dụng và khoảng an toàn.")
        enhance_hint.setStyleSheet("color:#64748b;")
        enhance_grid.addWidget(enhance_hint, 0, 0, 1, 4)
        enhance_index = 0

        guidance = {
            "Gain": "Tăng/giảm mức tổng. An toàn: -1 đến +1 dB.",
            "Bass": "Điều chỉnh âm trầm. An toàn: -2 đến +2 dB.",
            "Mid": "Độ rõ thân giọng. An toàn: -2 đến +2 dB.",
            "Treble": "Độ sáng và chi tiết. An toàn: -2 đến +2 dB.",
            "High-pass": "Loại tiếng ù thấp. Giọng nói: 80–100 Hz.",
            "Low-pass": "Giảm nhiễu tần số cao. An toàn: 16–18 kHz.",
            "Giảm nhiễu": "Giảm tiếng nền liên tục. An toàn: 10–20%; tránh quá 20%.",
            "Compressor": "Cân bằng đoạn nói nhỏ/lớn. Tỷ lệ an toàn: 2:1–3:1.",
            "Tỷ lệ nén": "Mức nén chênh lệch âm lượng. An toàn: 2:1–3:1.",
            "Ngưỡng nén": "Mức bắt đầu nén. An toàn: -24 đến -12 dB.",
            "Attack": "Tốc độ compressor phản ứng. An toàn: 5–20 ms.",
            "Release": "Thời gian compressor nhả. An toàn: 80–150 ms.",
            "De-esser": "Giảm âm S/X gắt. Chỉ dùng khi cần: 2–4 dB.",
            "Chuẩn hóa âm lượng": "Đưa âm lượng về mức nghe ổn định.",
            "Stereo": "Mục tiêu stereo. Khuyên dùng: -16 LUFS.",
            "Mono": "Mục tiêu mono. Khuyên dùng: -19 LUFS.",
            "Limiter": "Chặn đỉnh âm thanh để tránh vỡ tiếng.",
            "Ceiling": "Đỉnh tối đa. Khuyên dùng: -1 dB.",
        }

        def enhanced_row(label, widget, original):
            nonlocal enhance_index
            host = QWidget(); line = QHBoxLayout(host)
            line.setContentsMargins(0, 0, 0, 0); line.setSpacing(8)
            line.addWidget(widget, 1)
            hint = QLabel(f"Gốc: {original}")
            hint.setStyleSheet("color:#64748b;")
            hint.setMinimumWidth(82)
            line.addWidget(hint)
            self._enhance_audio_controls.append(host)
            grid_row = enhance_index // 2 + 1
            grid_col = (enhance_index % 2) * 2
            field_label = QLabel(label)
            field_label.setMinimumWidth(105)
            tip = guidance.get(label, "")
            field_label.setToolTip(tip)
            widget.setToolTip(tip)
            host.setToolTip(tip)
            enhance_grid.addWidget(field_label, grid_row, grid_col)
            enhance_grid.addWidget(host, grid_row, grid_col + 1)
            enhance_index += 1

        self.sp_gain = self._spin(QDoubleSpinBox, -1.0, 1.0, a.gain_db, 0.1)
        self.sp_gain.setSuffix(" dB")
        self._bind_float(self.sp_gain, a, "gain_db", "Gain")
        enhanced_row("Gain", self.sp_gain, "0 dB")

        self.sp_bass = self._spin(QDoubleSpinBox, -2.0, 2.0, a.bass_db, 0.1)
        self.sp_bass.setSuffix(" dB")
        self._bind_float(self.sp_bass, a, "bass_db", "Bass")
        enhanced_row("Bass", self.sp_bass, "0 dB")

        self.sp_mid = self._spin(QDoubleSpinBox, -2.0, 2.0, a.mid_db, 0.1)
        self.sp_mid.setSuffix(" dB")
        self._bind_float(self.sp_mid, a, "mid_db", "Mid")
        enhanced_row("Mid", self.sp_mid, "0 dB")

        self.sp_treble = self._spin(QDoubleSpinBox, -2.0, 2.0, a.treble_db, 0.1)
        self.sp_treble.setSuffix(" dB")
        self._bind_float(self.sp_treble, a, "treble_db", "Treble")
        enhanced_row("Treble", self.sp_treble, "0 dB")

        self.cmb_highpass = self._data_combo(
            [(0, "Tắt"), (80, "80 Hz"), (90, "90 Hz"), (100, "100 Hz")],
            a.highpass_hz)
        self._bind_combo(self.cmb_highpass, a, "highpass_hz", "High-pass")
        enhanced_row("High-pass", self.cmb_highpass, "Tắt")

        self.cmb_lowpass = self._data_combo(
            [(0, "Tắt"), (16000, "16 kHz"), (17000, "17 kHz"), (18000, "18 kHz")],
            a.lowpass_hz)
        self._bind_combo(self.cmb_lowpass, a, "lowpass_hz", "Low-pass")
        enhanced_row("Low-pass", self.cmb_lowpass, "Tắt")

        self.sp_noise = self._spin(
            QSpinBox, 0, 20, a.noise_reduction_percent)
        self.sp_noise.setSuffix("%")
        self._bind_int(self.sp_noise, a, "noise_reduction_percent", "Giảm nhiễu")
        enhanced_row("Giảm nhiễu", self.sp_noise, "0%")

        self.chk_compressor = QCheckBox("Bật")
        self.chk_compressor.setChecked(a.compressor_enabled)
        self._bind_bool(self.chk_compressor, a, "compressor_enabled", "Compressor")
        enhanced_row("Compressor", self.chk_compressor, "Tắt")

        self.sp_comp_ratio = self._spin(
            QDoubleSpinBox, 2.0, 3.0, a.compressor_ratio, 0.1)
        self.sp_comp_ratio.setSuffix(":1")
        self._bind_float(self.sp_comp_ratio, a, "compressor_ratio", "Compressor ratio")
        enhanced_row("Tỷ lệ nén", self.sp_comp_ratio, "Tắt")

        self.sp_comp_threshold = self._spin(
            QDoubleSpinBox, -24.0, -12.0, a.compressor_threshold_db, 1.0)
        self.sp_comp_threshold.setSuffix(" dB")
        self._bind_float(
            self.sp_comp_threshold, a, "compressor_threshold_db", "Ngưỡng nén")
        enhanced_row("Ngưỡng nén", self.sp_comp_threshold, "Tắt")

        self.sp_attack = self._spin(
            QDoubleSpinBox, 5.0, 20.0, a.compressor_attack_ms, 1.0)
        self.sp_attack.setSuffix(" ms")
        self._bind_float(self.sp_attack, a, "compressor_attack_ms", "Attack")
        enhanced_row("Attack", self.sp_attack, "Tắt")

        self.sp_release = self._spin(
            QDoubleSpinBox, 80.0, 150.0, a.compressor_release_ms, 5.0)
        self.sp_release.setSuffix(" ms")
        self._bind_float(self.sp_release, a, "compressor_release_ms", "Release")
        enhanced_row("Release", self.sp_release, "Tắt")

        self.cmb_deesser = self._data_combo(
            [(0.0, "Tắt"), (2.0, "2 dB"), (3.0, "3 dB"), (4.0, "4 dB")],
            a.deesser_db)
        self._bind_combo(self.cmb_deesser, a, "deesser_db", "De-esser")
        enhanced_row("De-esser", self.cmb_deesser, "Tắt")

        self.chk_loudness = QCheckBox("Bật")
        self.chk_loudness.setChecked(a.loudness_enabled)
        self._bind_bool(self.chk_loudness, a, "loudness_enabled", "Loudness")
        enhanced_row("Chuẩn hóa âm lượng", self.chk_loudness, "Theo nguồn")

        self.sp_loud_stereo = self._spin(
            QDoubleSpinBox, -19.0, -14.0, a.loudness_stereo_lufs, 1.0)
        self.sp_loud_stereo.setSuffix(" LUFS")
        self._bind_float(
            self.sp_loud_stereo, a, "loudness_stereo_lufs", "Loudness stereo")
        enhanced_row("Stereo", self.sp_loud_stereo, "Theo nguồn")

        self.sp_loud_mono = self._spin(
            QDoubleSpinBox, -21.0, -16.0, a.loudness_mono_lufs, 1.0)
        self.sp_loud_mono.setSuffix(" LUFS")
        self._bind_float(
            self.sp_loud_mono, a, "loudness_mono_lufs", "Loudness mono")
        enhanced_row("Mono", self.sp_loud_mono, "Theo nguồn")

        self.chk_limiter = QCheckBox("Bật")
        self.chk_limiter.setChecked(a.limiter_enabled)
        self._bind_bool(self.chk_limiter, a, "limiter_enabled", "Limiter")
        enhanced_row("Limiter", self.chk_limiter, "Tắt")

        self.sp_ceiling = self._spin(
            QDoubleSpinBox, -2.0, -0.5, a.limiter_ceiling_db, 0.1)
        self.sp_ceiling.setSuffix(" dB")
        self._bind_float(self.sp_ceiling, a, "limiter_ceiling_db", "Ceiling")
        enhanced_row("Ceiling", self.sp_ceiling, "Tắt")

        form.addRow(enhance_box)
        self._set_enhance_controls_enabled(
            bool(getattr(a, "enhance_original_voice", False)))
        self.chk_duck = QCheckBox("Tự giảm nhạc nền khi có lời thoại")
        self.chk_duck.setChecked(a.duck_music)
        self._bind_bool(self.chk_duck, a, "duck_music", "Tự giảm nhạc")
        form.addRow("Tự giảm nhạc", self.chk_duck)
        self.sp_pitch = self._spin(QSpinBox, -12, 12, a.pitch_shift_semitones)
        self._bind_int(self.sp_pitch, a, "pitch_shift_semitones", "Độ cao giọng")
        form.addRow("Độ cao giọng", self.sp_pitch)
        self.sp_aspeed = self._spin(QDoubleSpinBox, 0.25, 4.0, a.audio_speed, 0.05)
        self._bind_float(self.sp_aspeed, a, "audio_speed", "Tốc độ audio")
        self.sp_aspeed.setToolTip(
            "Chỉ áp dụng cho file âm thanh mới hoặc Edge TTS thay thế âm thanh cũ. "
            "Nếu giữ âm thanh gốc, âm thanh tự bám theo tốc độ video.")
        form.addRow("Tốc độ âm thanh mới", self.sp_aspeed)
        self.sp_mvol = self._spin(QDoubleSpinBox, 0.0, 1.0, a.music_volume, 0.05)
        self._bind_float(self.sp_mvol, a, "music_volume", "Âm lượng nhạc nền")
        form.addRow("Âm lượng nhạc nền", self.sp_mvol)
        form.addRow("Nhạc nền thay thế", self._audio_file_row("replace_music"))
        self._build_voiceover_controls(form)   # "Lồng tiếng": gộp Edge TTS + file thu sẵn
        btn_reset = QPushButton("Khôi phục mặc định")
        btn_reset.clicked.connect(self._restore_advanced_defaults)
        form.addRow("", btn_reset)
        return g

    def _build_advanced_group(self) -> QWidget:
        e = self.cfg.editor
        cg = e.color_grading
        a = e.audio
        g = QGroupBox("Cài đặt nâng cao")
        form = QFormLayout(g)

        # --- Video ---
        self.chk_flip = QCheckBox()
        self.chk_flip.setChecked(e.flip_horizontal)
        self._bind_bool(self.chk_flip, e, "flip_horizontal", "Lật ngang")
        form.addRow("Lật ngang (flip)", self.chk_flip)

        e.mirror_crop = False
        self.chk_mirror = QCheckBox(); self.chk_mirror.setChecked(False)

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
        self.chk_fp = QCheckBox("Fingerprint: biến đổi kỹ thuật nhẹ mỗi video")
        self.chk_fp.setChecked(e.fingerprint_enabled)
        self.chk_fp.setToolTip(
            "Thay đổi rất nhẹ tốc độ, FPS, màu, zoom và trạng thái lật theo từng video. "
            "Không làm thay đổi quyền sử dụng nội dung hoặc bảo đảm kết quả Content ID.")
        self._bind_bool(self.chk_fp, e, "fingerprint_enabled", "Fingerprint")
        form.addRow("Chống trùng", self.chk_fp)
        self.sp_pitch = self._spin(QSpinBox, -12, 12, a.pitch_shift_semitones)
        self._bind_int(self.sp_pitch, a, "pitch_shift_semitones", "Pitch")
        form.addRow("Pitch shift (nửa cung)", self.sp_pitch)
        self.sp_aspeed = self._spin(QDoubleSpinBox, 0.25, 4.0, a.audio_speed, 0.05)
        self._bind_float(self.sp_aspeed, a, "audio_speed", "Tốc độ audio")
        self.sp_aspeed.setToolTip(
            "Chỉ dùng cho voiceover/TTS hoặc âm thanh mới thay hoàn toàn âm thanh cũ.")
        form.addRow("Tốc độ âm thanh mới", self.sp_aspeed)
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
        e.side_squeeze_percent = d.side_squeeze_percent
        e.color_grading.enabled = d.color_grading.enabled
        e.color_grading.brightness = d.color_grading.brightness
        e.color_grading.contrast = d.color_grading.contrast
        e.color_grading.saturation = d.color_grading.saturation
        e.mask_regions = []
        e.audio.mute_all = d.audio.mute_all
        e.audio.separate_speech = d.audio.separate_speech
        e.audio.pitch_shift_semitones = d.audio.pitch_shift_semitones
        e.audio.audio_speed = d.audio.audio_speed
        e.audio.music_volume = d.audio.music_volume
        e.audio.replace_music = d.audio.replace_music
        e.audio.voiceover = d.audio.voiceover
        for attr in (
                "enhance_original_voice", "gain_db", "bass_db", "mid_db", "treble_db",
                "highpass_hz", "lowpass_hz", "noise_reduction_percent",
                "compressor_enabled", "compressor_threshold_db", "compressor_ratio",
                "compressor_attack_ms", "compressor_release_ms", "deesser_db",
                "loudness_enabled", "loudness_stereo_lufs", "loudness_mono_lufs",
                "limiter_enabled", "limiter_ceiling_db"):
            setattr(e.audio, attr, getattr(d.audio, attr))

        # cập nhật widget, chặn signal để KHÔNG lưu nhiều lần
        pairs = [
            (self.chk_flip, e.flip_horizontal), (self.chk_mirror, e.mirror_crop),
            (self.chk_color, e.color_grading.enabled),
            (self.sp_speed, e.speed), (self.sp_zoom, e.zoom_fill_percent),
            (self.sp_sidecrop, int(e.side_crop_percent)),
            (self.sp_squeeze, int(e.side_squeeze_percent)),
            (self.sp_bright, e.color_grading.brightness),
            (self.sp_contrast, e.color_grading.contrast),
            (self.sp_sat, e.color_grading.saturation),
            (self.sp_pitch, e.audio.pitch_shift_semitones),
            (self.sp_aspeed, e.audio.audio_speed), (self.sp_mvol, e.audio.music_volume),
            (self.sp_gain, e.audio.gain_db), (self.sp_bass, e.audio.bass_db),
            (self.chk_enhance_audio, e.audio.enhance_original_voice),
            (self.sp_mid, e.audio.mid_db), (self.sp_treble, e.audio.treble_db),
            (self.sp_noise, e.audio.noise_reduction_percent),
            (self.chk_compressor, e.audio.compressor_enabled),
            (self.sp_comp_ratio, e.audio.compressor_ratio),
            (self.sp_comp_threshold, e.audio.compressor_threshold_db),
            (self.sp_attack, e.audio.compressor_attack_ms),
            (self.sp_release, e.audio.compressor_release_ms),
            (self.chk_loudness, e.audio.loudness_enabled),
            (self.sp_loud_stereo, e.audio.loudness_stereo_lufs),
            (self.sp_loud_mono, e.audio.loudness_mono_lufs),
            (self.chk_limiter, e.audio.limiter_enabled),
            (self.sp_ceiling, e.audio.limiter_ceiling_db),
        ]
        for w, val in pairs:
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(val))
            else:
                w.setValue(val)
            w.blockSignals(False)
        for combo, val in (
                (self.cmb_highpass, e.audio.highpass_hz),
                (self.cmb_lowpass, e.audio.lowpass_hz),
                (self.cmb_deesser, e.audio.deesser_db)):
            self._set_combo_data(combo, val)
        for attr in ("replace_music", "voiceover"):
            if attr in self._audio_labels:
                self._audio_labels[attr].setText("(không)")
        self._sync_audio_mode_combo()      # ô 'Âm thanh đầu ra' về mặc định (giữ nguyên gốc)
        self._sync_voiceover_controls()    # ô 'Lồng tiếng' theo config sau khi reset voiceover

        self._refresh_mask_list()
        self._sync_masks_to_preview()
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        self._log_ed("Đã khôi phục cài đặt nâng cao về bản nháp mặc định; chưa lưu.")

    def _save_cfg(self) -> bool:
        """Save download/source settings without accidentally committing editor draft."""
        if not self.config_path:
            return False
        try:
            snapshot = deepcopy(self.cfg)
            snapshot.editor = deepcopy(self._saved_editor)
            save_config(snapshot, self.config_path)
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
        self._mark_settings_dirty()
        self._log_ed(f"Đã chọn thư mục xuất: {d} — đang chờ lưu.")

    @staticmethod
    def _set_combo_data(w, value) -> None:
        for i in range(w.count()):
            if w.itemData(i) == value:
                w.blockSignals(True); w.setCurrentIndex(i); w.blockSignals(False)
                return

    def _downloaded_videos(self) -> list[str]:
        return [r.download_path for r in self.db.all_videos()
                if r.download_status == "downloaded" and r.download_path
                and Path(r.download_path).exists()]

    def _downloaded_preview_items(self) -> list[tuple[str, str]]:
        folder = (self.cfg.editor.input_folder or "").strip()
        if folder:
            local_items = [
                (video_id_for(path), str(path))
                for path in list_videos(folder)
            ]
            if local_items:
                return local_items
        if hasattr(self, "_queue_tab"):
            current = self._queue_tab.preview_source()
            if current:
                return [current]
        return [(r.video_id, r.download_path) for r in self.db.all_videos()
                if r.download_status == "downloaded" and r.download_path
                and Path(r.download_path).exists()]

    def on_preview(self, batch: bool = False) -> None:
        items = self._downloaded_preview_items()
        if not items:
            QMessageBox.information(
                self, "Chưa có video xem trước",
                "Hãy chọn thư mục theo dõi có video, hoặc chọn một video trong tab Hàng đợi.")
            return
        video_ids = [item[0] for item in items]
        vids = [item[1] for item in items]
        out_dir = str(Path(self.cfg.editor.output_dir) / "_previews")
        target = (self.cmb_preview_target.currentData()
                  if hasattr(self, "cmb_preview_target") else "logo")
        at_seconds = (self.sp_preview_time.value()
                      if hasattr(self, "sp_preview_time") else 1.0)
        try:
            if batch:
                res = preview.preview_batch(
                    self.cfg, vids, out_dir, at_seconds=at_seconds, overlay_target=target,
                    video_ids=video_ids)
                ok = sum(r["ok"] for r in res)
                self._log_ed(f"Xem trước hàng loạt: {ok}/{len(res)} ảnh -> {out_dir}")
                if os.path.isdir(out_dir):
                    os.startfile(out_dir)
            else:
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                png = str(Path(out_dir) / "preview.png")
                preview.preview_frame(
                    self.cfg, vids[0], png, at_seconds=at_seconds, overlay_target=target,
                    video_id=video_ids[0])
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
        # Chỉ các trang Phụ đề/Thương hiệu mới dùng click để đặt lớp mới.
        # Trang Che nội dung cũ có luồng kéo/thay đổi kích thước riêng.
        if getattr(self, "_editor_settings_page_index", 0) not in (3, 4):
            return
        target = self.cmb_preview_target.currentData()
        if target == "mask":
            return
        if hasattr(self, "brand_tabs") and target in ("logo", "hook", "cta"):
            wanted = {"logo": 0, "hook": 1, "cta": 2}.get(target, 0)
            if self.brand_tabs.currentIndex() != wanted:
                self.brand_tabs.setCurrentIndex(wanted)
        e = self.cfg.editor
        if target == "subtitle":
            pos = (
                "top" if fy < 0.28 else
                "middle" if fy < 0.58 else
                "blur_bottom" if fy < 0.82 else
                "bottom")
            e.subtitle.position = pos
            self._set_combo_data(self.cmb_subpos, pos)
            label = {
                "top": "Trên", "middle": "Giữa", "bottom": "Sát đáy",
                "blur_bottom": "Vùng mờ phía dưới"}[pos]
        elif target == "logo":
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
        self.lbl_preview_position.setText(f"{label} · chưa lưu")
        self._refresh_overlay_warnings()
        self._mark_settings_dirty()
        self._log_ed(f"Đặt {target} trực tiếp trên preview: {pos} — đang chờ lưu.")

    @staticmethod
    def _position_point(target: str, pos: str) -> tuple[float, float]:
        if target == "logo":
            return {
                "top-left": (0.15, 0.12), "top-right": (0.85, 0.12),
                "bottom-left": (0.15, 0.88), "bottom-right": (0.85, 0.88),
            }.get(pos, (0.85, 0.12))
        return {"top": (0.5, 0.15), "middle": (0.5, 0.5),
                "bottom": (0.5, 0.85), "blur_bottom": (0.5, 0.72)
                }.get(pos, (0.5, 0.85))

    def _on_preview_target_changed(self, _index: int = 0) -> None:
        if not hasattr(self, "lbl_live_preview"):
            return
        target = self.cmb_preview_target.currentData()
        e = self.cfg.editor
        if target == "mask":
            active = self.lst_masks.currentRow() if hasattr(self, "lst_masks") else -1
            self._sync_masks_to_preview(active)
            self.lbl_live_preview.set_active_mask(active)
            self.lbl_live_preview.set_safe_margins(0, 0, 0, 0)
            state = "chưa lưu" if self._settings_dirty else "đã lưu"
            self.lbl_preview_position.setText(
                f"Kéo trong khung để di chuyển; kéo bốn góc để đổi kích thước · {state}")
            return
        pos = (
            e.subtitle.position if target == "subtitle" else
            e.overlay.position if target == "logo" else
            e.intro_hook.position if target == "hook" else
            e.outro_cta.position)
        fx, fy = self._position_point(target, pos)
        self.lbl_live_preview.set_active(target)
        self.lbl_live_preview.set_marker(target, fx, fy)
        if target == "subtitle":
            subtitle = e.subtitle
            self.lbl_live_preview.set_safe_margins(
                subtitle.margin_left_percent, subtitle.margin_right_percent,
                subtitle.margin_top_percent, subtitle.margin_bottom_percent)
        elif target in ("hook", "cta"):
            overlay = e.intro_hook if target == "hook" else e.outro_cta
            margin = int(getattr(overlay, "safe_margin_percent", 5))
            self.lbl_live_preview.set_safe_margins(margin, margin, margin, margin)
        else:
            self.lbl_live_preview.set_safe_margins(0, 0, 0, 0)
        labels = {"top-left": "Trên trái", "top-right": "Trên phải",
                  "bottom-left": "Dưới trái", "bottom-right": "Dưới phải",
                  "top": "Trên", "middle": "Giữa", "bottom": "Dưới",
                  "blur_bottom": "Vùng mờ phía dưới"}
        state = "chưa lưu" if self._settings_dirty else "đã lưu"
        self.lbl_preview_position.setText(f"{labels.get(pos, pos)} · {state}")

    def _on_editor_settings_page_changed(self, index: int,
                                         stack: QStackedWidget) -> None:
        """Giữ preview tập trung vào đúng công cụ của trang đang mở."""
        stack.setCurrentIndex(index)
        self._editor_settings_page_index = index
        mask_page = index == 5
        if hasattr(self, "preview_box"):
            self.preview_box.setTitle(
                "Xem trước · Che nội dung cũ" if mask_page else "Xem trước")
        target = None
        if index == 3:
            if getattr(self.cfg.editor.subtitle, "replacement_box_enabled", False):
                target = "mask"
                active = self._ensure_subtitle_replacement_mask()
                self._sync_masks_to_preview(active)
            else:
                target = "subtitle"
        elif index == 4:
            brand_index = self.brand_tabs.currentIndex() if hasattr(self, "brand_tabs") else 0
            target = {0: "logo", 1: "hook", 2: "cta"}.get(brand_index, "logo")
        elif mask_page:
            target = "mask"

        show_position = target is not None
        if hasattr(self, "lbl_preview_target"):
            self.lbl_preview_target.hide()
        if hasattr(self, "lbl_preview_context"):
            self.lbl_preview_context.hide()
        if hasattr(self, "lbl_preview_position"):
            self.lbl_preview_position.setVisible(show_position)
        if target and hasattr(self, "cmb_preview_target"):
            target_index = self.cmb_preview_target.findData(target)
            if target_index >= 0:
                self.cmb_preview_target.setCurrentIndex(target_index)
            self._update_preview_context_label(target)
            if index == 3 and target == "mask":
                self.lbl_preview_context.setText("Khung phụ đề thay thế")
                self._sync_masks_to_preview(self._ensure_subtitle_replacement_mask())
        if mask_page:
            active = (self.lst_masks.currentRow()
                      if hasattr(self, "lst_masks") else -1)
            self._sync_masks_to_preview(active)
        else:
            self._sync_masks_to_preview()
        if hasattr(self, "btn_preview_refresh"):
            self.btn_preview_refresh.setText(
                "Cập nhật xem trước" if mask_page else "Xem trước bản nháp")

        self._on_preview_mode_changed()

    def _update_preview_context_label(self, target: str) -> None:
        if hasattr(self, "lbl_preview_context"):
            self.lbl_preview_context.setText({
                "subtitle": "Phụ đề", "logo": "Logo",
                "hook": "Hook mở đầu", "cta": "CTA cuối video",
                "mask": "Vùng che đang chọn",
            }.get(target, target))

    def _on_brand_tab_changed(self, index: int) -> None:
        """Đồng bộ đúng tab thương hiệu với lớp đang chỉnh trên preview."""
        if getattr(self, "_editor_settings_page_index", -1) != 4:
            return
        target = {0: "logo", 1: "hook", 2: "cta"}.get(index, "logo")
        if hasattr(self, "cmb_preview_target"):
            target_index = self.cmb_preview_target.findData(target)
            if target_index >= 0:
                self.cmb_preview_target.setCurrentIndex(target_index)
        self._update_preview_context_label(target)

    def _sync_overlay_safe_frame(self, target: str) -> None:
        if (not hasattr(self, "cmb_preview_target")
                or self.cmb_preview_target.currentData() != target):
            return
        self._on_preview_target_changed()

    def _refresh_overlay_warnings(self) -> None:
        """Cảnh báo xung đột vùng hiển thị, không tự ý di chuyển nội dung."""
        labels = getattr(self, "_overlay_warning_labels", {})
        if not labels:
            return
        editor = self.cfg.editor
        subtitle_pos = (
            "bottom" if editor.subtitle.position == "blur_bottom"
            else editor.subtitle.position)
        logo_vertical = (
            "top" if editor.overlay.position.startswith("top") else "bottom")
        both_overlays = (
            editor.intro_hook.enabled and editor.outro_cta.enabled
            and editor.intro_hook.position == editor.outro_cta.position)
        for key, overlay in (
                ("hook", editor.intro_hook), ("cta", editor.outro_cta)):
            conflicts = []
            if overlay.enabled and editor.subtitle.enabled:
                if overlay.position == subtitle_pos:
                    conflicts.append("phụ đề")
            if overlay.enabled and editor.overlay.enabled:
                if overlay.position == logo_vertical:
                    conflicts.append("logo")
            if overlay.enabled and both_overlays:
                other = editor.outro_cta if key == "hook" else editor.intro_hook
                threshold = float(overlay.seconds) + float(other.seconds)
                conflicts.append(
                    f"{'CTA' if key == 'hook' else 'Hook'} ở video ngắn hơn "
                    f"{threshold:g} giây")
            label = labels.get(key)
            if label is not None:
                label.setText(
                    "Có thể chồng với " + ", ".join(conflicts)
                    + ". Hãy kiểm tra trên preview."
                    if conflicts else "")
                label.setVisible(bool(conflicts))

    def _on_subtitle_safe_frame_changed(self) -> None:
        """Đồng bộ khung lề phụ đề trên preview với cấu hình bản nháp."""
        if not hasattr(self, "lbl_live_preview"):
            return
        subtitle = self.cfg.editor.subtitle
        self.lbl_live_preview.set_safe_margins(
            subtitle.margin_left_percent,
            subtitle.margin_right_percent,
            subtitle.margin_top_percent,
            subtitle.margin_bottom_percent)
        self._mark_preview_stale("subtitle")

    def _mark_preview_stale(self, target: str) -> None:
        """Báo rõ preview tĩnh cần render lại sau khi đổi cỡ chữ/logo."""
        if hasattr(self, "cmb_preview_target") and target in (
                "subtitle", "logo", "hook", "cta"):
            idx = self.cmb_preview_target.findData(target)
            if idx >= 0:
                self.cmb_preview_target.setCurrentIndex(idx)
        if hasattr(self, "lbl_preview_position"):
            name = {"subtitle": "phụ đề", "hook": "Hook",
                    "cta": "CTA", "logo": "Logo"}.get(target, target)
            self.lbl_preview_position.setText(
                f"Đã đổi {name} · bấm Tạo / cập nhật xem trước")

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
        items = self._downloaded_preview_items()
        if not items:
            QMessageBox.information(self, "Chưa có video", "Cần 1 video đã tải để xem trước vị trí.")
            return
        out_dir = Path(self.cfg.editor.output_dir) / "_previews"
        out_dir.mkdir(parents=True, exist_ok=True)
        png = str(out_dir / "pos_preview.png")
        try:
            preview.preview_frame(
                self.cfg, items[0][1], png, at_seconds=1.0,
                video_id=items[0][0])
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
        self._mark_settings_dirty()
        self._log_ed(f"Đã đặt vị trí trực quan vào bản nháp: {p}")

    def on_export_report(self) -> None:
        out = str(Path(self.cfg.editor.output_dir) / "bao_cao.md")
        try:
            report.write_report(self.db, out)
            self._log_ed(f"Đã xuất báo cáo: {out}")
            os.startfile(out)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi báo cáo", str(e))

    def _apply_download_mode(self) -> None:
        """Áp dụng chế độ chỉ biên tập local mà không ảnh hưởng hàng đợi."""
        enabled = bool(getattr(self.cfg.download, "enabled", True))
        if hasattr(self, "download_content"):
            self.download_content.setVisible(enabled)
        if hasattr(self, "download_disabled"):
            self.download_disabled.setVisible(not enabled)
        if hasattr(self, "download_mode_bar"):
            self.download_mode_bar.setVisible(enabled)
        if hasattr(self, "lbl_download_mode"):
            self.lbl_download_mode.setText(
                "" if enabled else
                "Chế độ xử lý video có sẵn · nhập video hoặc thư mục tại tab Hàng đợi")
        if hasattr(self, "tabs") and hasattr(self, "_download_idx"):
            self.tabs.setTabText(self._download_idx, "Tải xuống")

    def _open_queue_import_video(self) -> None:
        self.tabs.setCurrentIndex(self._queue_idx)
        QTimer.singleShot(0, self._queue_tab.on_import)

    def _open_queue_import_folder(self) -> None:
        self.tabs.setCurrentIndex(self._queue_idx)
        QTimer.singleShot(0, self._queue_tab.on_import_folder)

    def _toggle_download_mode(self, on: bool) -> None:
        """Tắt luồng YouTube nhưng vẫn giữ nguyên luồng biên tập video local."""
        self.cfg.download.enabled = bool(on)
        if not on:
            self.cfg.download.auto_scan_enabled = False
            self.chk_auto.blockSignals(True)
            self.chk_auto.setChecked(False)
            self.chk_auto.blockSignals(False)
            if hasattr(self, "_auto"):
                self._auto.stop_scheduler()
            active = ((self._scan_worker and self._scan_worker.isRunning())
                      or (self._manual_worker and self._manual_worker.isRunning()))
            if active and hasattr(self, "_auto"):
                self.on_stop_all_downloads()
            self._log_dl(
                "Đã tắt toàn bộ tính năng tải YouTube. "
                "Hàng đợi vẫn nhận và xử lý video có sẵn.")
        else:
            self._log_dl(
                "Đã bật tính năng tải YouTube. Quét tự động vẫn tắt cho đến khi được chọn.")
        self._apply_download_mode()
        self._save_cfg()

    def _toggle_auto(self, on: bool) -> None:
        """Bật/tắt tự động quét & tải định kỳ ở module cập nhật."""
        if on and not self.cfg.download.enabled:
            self.chk_auto.blockSignals(True)
            self.chk_auto.setChecked(False)
            self.chk_auto.blockSignals(False)
            return
        self.cfg.download.auto_scan_enabled = bool(on)
        if on:
            self._auto.start_scheduler()
            self._log_dl("Đã BẬT tự động quét & tải.")
        else:
            self._auto.stop_scheduler()
            self._log_dl("Đã TẮT tự động quét & tải (bấm 'Quét ngay' để chạy tay).")
        self._save_cfg()

    def on_pick_input_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Chọn folder video đầu vào", self.cfg.editor.input_folder or "")
        if not folder:
            return
        self.cfg.editor.input_folder = folder
        self.lbl_input.setText(folder)
        self._mark_settings_dirty()
        self._log_ed(
            "Đã chọn thư mục theo dõi — bấm 'Lưu cài đặt' để bắt đầu theo dõi.")

    def _setup_input_folder_watch(self) -> None:
        """Theo dõi folder đầu vào và quét nhẹ định kỳ để bắt cả thư mục con."""
        if hasattr(self, "_input_watcher"):
            self._watch_input_folder(self._saved_editor.input_folder)
            self._sync_input_folder()
            return
        self._input_watcher = QFileSystemWatcher(self)
        self._input_watcher.directoryChanged.connect(lambda _p: self._input_debounce.start())
        self._input_debounce = QTimer(self)
        self._input_debounce.setSingleShot(True); self._input_debounce.setInterval(700)
        self._input_debounce.timeout.connect(self._sync_input_folder)
        self._input_poll = QTimer(self)
        self._input_poll.setInterval(5000)
        self._input_poll.timeout.connect(self._sync_input_folder)
        self._input_file_state: dict[str, tuple[int, int]] = {}
        self._input_poll.start()
        self._watch_input_folder(self._saved_editor.input_folder)
        QTimer.singleShot(0, self._sync_input_folder)

    def _watch_input_folder(self, folder: str) -> None:
        if not hasattr(self, "_input_watcher"):
            return
        old = self._input_watcher.directories()
        if old:
            self._input_watcher.removePaths(old)
        if folder and Path(folder).is_dir():
            self._input_watcher.addPath(folder)

    def _sync_input_folder(self) -> None:
        folder = self._saved_editor.input_folder
        if not folder or not Path(folder).is_dir():
            return
        # Chỉ nhận file ổn định qua 2 lần kiểm tra để không edit khi file còn đang copy.
        stable = []
        current = {}
        for path in list_videos(folder):
            try:
                signature = (path.stat().st_size, path.stat().st_mtime_ns)
            except OSError:
                continue
            key = str(path).lower(); current[key] = signature
            if self._input_file_state.get(key) == signature:
                stable.append(path)
        self._input_file_state = current
        if not stable:
            return
        # ingest_paths không tự retry video lỗi; thao tác đó vẫn thuộc nút “Thử lại”.
        res = ingest_paths(self.db, stable)
        if res["added"] <= 0:
            return
        self._queue_tab.refresh()
        self._log_ed(f"Tự động phát hiện {res['added']} video mới trong folder đầu vào.")
        if self._saved_editor.auto_edit_after_download:
            self._queue_tab.on_start()

    def on_edit_folder(self) -> None:
        """Import video từ folder đầu vào rồi đẩy video MỚI/CHƯA EDIT vào hàng đợi."""
        folder = self._saved_editor.input_folder
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
        if hasattr(self, "cmb_crop_mode"):
            self._set_combo_data(self.cmb_crop_mode, "manual")
        if hasattr(self, "cmb_fill"):
            self._set_combo_data(self.cmb_fill, "none")
        self.lbl_edit_summary.setText(self._edit_summary())
        self._mark_settings_dirty()
        self._log_ed(
            f"Đã đặt vùng crop thủ công vào bản nháp: focus=({fx:.2f},{fy:.2f}); "
            "bấm 'Lưu cài đặt' để áp dụng cho hàng đợi.")

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

            is_local = str(r.video_id).startswith("local_")
            if (r.download_status == "downloaded" and r.edit_status == "failed"
                    and not is_local):
                action = QPushButton("Tải lại")
                action.setStyleSheet("color:#b45309;")
                action.clicked.connect(
                    lambda _checked=False, vid=r.video_id, url=r.url: self._redownload_one(vid, url))
            elif r.download_status == "downloaded" and r.download_path:
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
            action.setFixedSize(96, 30)
            action.setStyleSheet(
                action.styleSheet()
                + "min-height:0px;max-height:30px;padding:1px 8px;")
            action_host = QWidget()
            action_layout = QHBoxLayout(action_host)
            action_layout.setContentsMargins(4, 1, 4, 1)
            action_layout.setAlignment(Qt.AlignCenter)
            action_layout.addWidget(action)
            self.table.setCellWidget(i, 5, action_host)

        counts = {"pending": 0, "downloading": 0, "cancelling": 0,
                  "paused": 0, "downloaded": 0, "failed": 0}
        for row in rows:
            if row.download_status in counts:
                counts[row.download_status] += 1
        self.lbl_download_summary.setText(
            f"Chờ {counts['pending']} · Đang tải {counts['downloading']} · "
            f"Tạm dừng {counts['paused'] + counts['cancelling']} · "
            f"Đã tải {counts['downloaded']} · Lỗi {counts['failed']}")
        stopped_count = counts["paused"] + counts["cancelling"]
        self.btn_resume_downloads.setVisible(stopped_count > 0)
        if counts["paused"]:
            self.btn_resume_downloads.setText(
                f"Tải lại video đã dừng ({counts['paused']})")
            # Cho phép tải lại phần đã dừng xong ngay cả khi video khác vẫn đang
            # chuyển từ cancelling -> paused. Handler sẽ tự chờ phiên cũ thoát.
            self.btn_resume_downloads.setEnabled(not self._resume_downloads_pending)
        elif counts["cancelling"]:
            self.btn_resume_downloads.setText(
                f"Đang dừng video… ({stopped_count})")
            self.btn_resume_downloads.setEnabled(False)
        err_targets = [r for r in rows
                       if (r.edit_status == "failed" or r.download_status == "failed")
                       and not str(r.video_id).startswith("local_")]
        if err_targets:
            self.btn_redownload_errors.setText(f"Tải lại video lỗi ({len(err_targets)})")
            self.btn_redownload_errors.show()
        else:
            self.btn_redownload_errors.hide()
        if counts["downloading"] or counts["cancelling"]:
            self.btn_stop_downloads.show()
        elif not ((self._scan_worker and self._scan_worker.isRunning())
                  or (self._manual_worker and self._manual_worker.isRunning())):
            self.btn_stop_downloads.hide()
            self.btn_stop_downloads.setEnabled(True)
            # Bao gồm cả phiên tải tự động: sau khi callback cuối chuyển mọi
            # video sang paused, mở lại ngay các thao tác chính.
            self.btn_scan.setEnabled(True)
            self.btn_refresh_waiting.setEnabled(True)

    def _on_status(self, video_id: str, field: str, value: str) -> None:
        self._reload_table()

    def _on_auto_status(self, video_id: str, field: str, value: str) -> None:
        # Chạy trên UI thread (qua bridge) -> chạm widget an toàn.
        self._reload_table()
        self._log_dl(f"[auto] {video_id}: {field}={value}")

    def _update_row_cell(self, video_id: str, col: int, text: str,
                         fg: str = "#334155", bg: str = "#ffffff") -> None:
        """Cập nhật đúng 1 ô theo video_id (không reload cả bảng mỗi tick %)."""
        for i in range(self.table.rowCount()):
            head = self.table.item(i, 0)
            if head and head.text() == video_id:
                cell = QTableWidgetItem(text)
                cell.setForeground(QColor(fg))
                cell.setBackground(QColor(bg))
                cell.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, col, cell)
                return

    def _on_download_progress(self, video_id: str, percent: float, note: str = "") -> None:
        """% tải -> ô cột 'Tải'. _reload_table sẽ ghi đè bằng nhãn khi đổi trạng thái."""
        self._update_row_cell(video_id, 3, f"Đang tải {percent:.0f}%", "#1d4ed8", "#dbeafe")

    def _on_edit_progress(self, video_id: str, percent: int, eta: str = "") -> None:
        """% biên tập (từ QueueTab) -> ô cột 'Biên tập'."""
        text = f"Đang xử lý {percent}%"
        if eta:
            text += f" · còn {eta}"
        self._update_row_cell(video_id, 4, text, "#6d28d9", "#ede9fe")

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

    def resizeEvent(self, event) -> None:
        """Đổi bố cục theo không gian thật thay vì giả định một độ phân giải cố định."""
        super().resizeEvent(event)
        width = event.size().width()
        height = event.size().height()
        if hasattr(self, "edit_splitter"):
            orientation = Qt.Vertical if width < 820 else Qt.Horizontal
            if self.edit_splitter.orientation() != orientation:
                self.edit_splitter.setOrientation(orientation)
            if orientation == Qt.Vertical:
                usable = max(430, height - 145)
                self.edit_splitter.setSizes([
                    max(230, int(usable * 0.58)),
                    max(180, int(usable * 0.42)),
                ])
            else:
                preview_width = min(460, max(280, int(width * 0.32)))
                self.edit_splitter.setSizes([
                    max(420, width - preview_width - 40), preview_width,
                ])
        if hasattr(self, "exports_hint"):
            self.exports_hint.setVisible(width >= 850)
        if hasattr(self, "exports_search"):
            self.exports_search.setMaximumWidth(210 if width < 850 else 280)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Chờ Windows tính xong viền cửa sổ rồi loại phần taskbar khỏi kích thước tối đa.
        QTimer.singleShot(0, self._constrain_to_work_area)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._constrain_to_work_area)

    def _constrain_to_work_area(self) -> None:
        """Không cho cửa sổ hoặc nội dung chui xuống dưới taskbar Windows."""
        handle = self.windowHandle()
        screen = handle.screen() if handle is not None else self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        border_w = max(0, frame.width() - self.width())
        border_h = max(0, frame.height() - self.height())
        max_w = max(480, available.width() - border_w)
        max_h = max(420, available.height() - border_h)
        self.setMaximumSize(max_w, max_h)
        if self.width() > max_w or self.height() > max_h:
            self.resize(min(self.width(), max_w), min(self.height(), max_h))

    def closeEvent(self, event) -> None:
        if self._settings_dirty:
            choice = QMessageBox.question(
                self, "Cài đặt chưa được lưu",
                "Bạn muốn lưu các thay đổi trong tab Cài đặt video trước khi thoát?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if choice == QMessageBox.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.Save and not self.on_save_editor_settings():
                event.ignore()
                return
        # 1) Ngừng lịch quét nền + các bộ đếm giờ theo dõi folder.
        for stop in (lambda: self._auto.cancel_all_downloads(),
                     lambda: self._auto.stop_scheduler(),
                     lambda: self._input_poll.stop() if hasattr(self, "_input_poll") else None,
                     lambda: self._input_debounce.stop() if hasattr(self, "_input_debounce") else None,
                     lambda: self._scan_worker.cancel_all() if self._scan_worker else None,
                     lambda: self._manual_worker.cancel() if self._manual_worker else None,
                     lambda: self._queue_tab.shutdown()):
            try:
                stop()
            except Exception:
                pass
        # 2) CHỜ mọi QThread thật sự kết thúc trước khi widget bị hủy — nếu không
        #    Qt sẽ abort với 'QThread: Destroyed while thread is still running'.
        for name in ("_scan_worker", "_manual_worker", "_check_worker", "_edge_voice_worker"):
            w = getattr(self, name, None)
            try:
                if w is not None and w.isRunning() and not w.wait(10000):
                    w.terminate(); w.wait(2000)
            except Exception:
                pass
        super().closeEvent(event)
