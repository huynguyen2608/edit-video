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
    min-height: 24px;
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
QComboBox QAbstractItemView {
    color: #0f172a;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
    outline: 0;
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
    padding: 6px 12px;
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
