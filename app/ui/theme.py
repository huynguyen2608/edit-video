"""Theme sáng dùng chung, ưu tiên độ tương phản trên mọi màn hình Windows."""
from __future__ import annotations


APP_STYLESHEET = r"""
QWidget {
    color: #0f172a;
    font-size: 12px;
}
QMainWindow, QDialog {
    background: #f8fafc;
}
QToolTip {
    color: #0f172a;
    background: #fffbea;
    border: 1px solid #d6b85c;
    padding: 4px;
}

QPushButton, QToolButton {
    min-height: 24px;
    padding: 3px 10px;
    color: #1f2937;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
}
QPushButton:hover, QToolButton:hover {
    color: #1e3a8a;
    background: #e8f1ff;
    border-color: #93b4df;
}
QPushButton:pressed, QToolButton:pressed {
    color: #172554;
    background: #cfe2ff;
    border-color: #6b9bd2;
}
QPushButton:focus, QToolButton:focus {
    border: 2px solid #3b82f6;
}
QPushButton:disabled, QToolButton:disabled {
    color: #94a3b8;
    background: #f1f5f9;
    border-color: #e2e8f0;
}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    min-height: 28px;
    color: #0f172a;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    padding: 2px 7px;
    selection-background-color: #bfdbfe;
    selection-color: #0f172a;
}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover,
QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {
    border-color: #94a3b8;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 2px solid #3b82f6;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    color: #94a3b8;
    background: #f1f5f9;
    border-color: #e2e8f0;
}
QComboBox {
    /* Bung danh sách THẢ XUỐNG ngay dưới ô, không canh theo option đang chọn. */
    combobox-popup: 0;
}
QComboBox QAbstractItemView {
    color: #0f172a;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
    outline: 0;
    padding: 2px;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
}

QTableWidget, QTableView, QListWidget, QListView {
    color: #0f172a;
    background: #ffffff;
    alternate-background-color: #f8fafc;
    border: 1px solid #dbe3ee;
    gridline-color: #e2e8f0;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
    outline: 0;
}
QTableWidget::item:hover, QTableView::item:hover,
QListWidget::item:hover, QListView::item:hover {
    color: #0f172a;
    background: #eff6ff;
}
QTableWidget::item:selected, QTableView::item:selected,
QListWidget::item:selected, QListView::item:selected {
    color: #0f172a;
    background: #dbeafe;
}
QHeaderView::section {
    color: #334155;
    background: #f1f5f9;
    border: 0;
    border-right: 1px solid #dbe3ee;
    border-bottom: 1px solid #cbd5e1;
    padding: 5px;
    font-weight: 600;
}

QTabWidget::pane {
    border-top: 1px solid #dbe3ee;
    background: #ffffff;
}
QTabBar::tab {
    color: #475569;
    background: #f8fafc;
    border: 1px solid transparent;
    padding: 9px 26px;
    min-width: 130px;
    font-size: 13px;
}
QTabBar::tab:hover {
    color: #1e3a8a;
    background: #e8f1ff;
}
QTabBar::tab:selected {
    color: #0f172a;
    background: #ffffff;
    border-color: #dbe3ee;
    border-bottom-color: #ffffff;
    font-weight: 600;
}
QGroupBox {
    color: #334155;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    margin-top: 9px;
    padding-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 4px;
    color: #334155;
    background: #f8fafc;
}
QProgressBar {
    color: #0f172a;
    background: #e2e8f0;
    border: 0;
    border-radius: 3px;
    text-align: center;
}
QProgressBar::chunk {
    background: #3b82f6;
    border-radius: 3px;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background: #f1f5f9;
    border: 0;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #94a3b8;
    border-radius: 4px;
    min-height: 28px;
    min-width: 28px;
}
QScrollBar::handle:hover {
    background: #64748b;
}
"""


def apply_theme(app) -> None:
    """Áp dụng palette nhất quán trước khi tạo bất kỳ cửa sổ nào."""
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)


class _ProportionalColumns:
    """Giữ độ rộng cột theo TỈ LỆ % bề rộng bảng (tự lấp đầy), vẫn cho kéo chỉnh.

    weights: list mỗi cột — số THỰC (float) = trọng số tỉ lệ; số NGUYÊN (int) = px CỐ ĐỊNH
    (vd cột ảnh thu nhỏ). Phần rộng còn lại chia cho các cột tỉ lệ; cột tỉ lệ cuối lấy
    phần dư để lấp khít, tránh chừa khoảng trống bên phải.
    """
    def __init__(self, table, weights, min_width: int = 50):
        from PySide6.QtCore import QObject, QEvent, QTimer

        self.table = table
        self.weights = list(weights)
        self.min_width = min_width
        self._applying = False

        class _Filter(QObject):
            def eventFilter(_self, obj, event):
                if event.type() in (QEvent.Resize, QEvent.Show):
                    # The viewport receives its final width after the parent
                    # resize event. Recalculate on the next event-loop tick.
                    QTimer.singleShot(0, self.apply)
                return False

        self._filter = _Filter(table)
        table.installEventFilter(self._filter)
        table.viewport().installEventFilter(self._filter)
        QTimer.singleShot(0, self.apply)

    def apply(self) -> None:
        if self._applying:
            return
        vp = self.table.viewport().width()
        if vp <= 10:
            return
        self._applying = True
        try:
            weights = self.weights
            fixed_total = sum(w for w in weights if isinstance(w, int))
            flex_idx = [i for i, w in enumerate(weights) if not isinstance(w, int)]
            flex_total = sum(weights[i] for i in flex_idx) or 1.0
            avail = max(0, vp - fixed_total)
            header = self.table.horizontalHeader()
            used = 0
            for k, i in enumerate(flex_idx):
                if k == len(flex_idx) - 1:            # cột tỉ lệ CUỐI lấy phần dư -> khít mép
                    px = max(self.min_width, avail - used)
                else:
                    px = max(self.min_width, int(avail * weights[i] / flex_total))
                    used += px
                header.resizeSection(i, px)
            for i, w in enumerate(weights):
                if isinstance(w, int):
                    header.resizeSection(i, w)
        finally:
            self._applying = False


def make_columns_resizable(table, weights=None, min_width: int = 50) -> None:
    """Cho phép kéo chỉnh MỌI cột + tự phân bổ theo % để lấp đầy bảng (không chừa trống).

    weights: trọng số tỉ lệ mỗi cột (float) hoặc px cố định (int) — xem _ProportionalColumns.
    """
    from PySide6.QtWidgets import QHeaderView
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)   # kéo được
    # Cột cuối luôn lấp đầy phần rộng còn lại. Các cột trước vẫn kéo được,
    # nhưng không thể tạo một dải trống ở mép phải của bảng.
    header.setStretchLastSection(True)
    header.setMinimumSectionSize(min_width)
    if weights:
        # Giữ tham chiếu trên chính table để không bị thu gom rác.
        table._proportional_columns = _ProportionalColumns(table, weights, min_width)
