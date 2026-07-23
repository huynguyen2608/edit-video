"""Điểm khởi động ứng dụng.

    python run.py                # chạy GUI
    python run.py --scan         # quét + tải 1 lần (headless), không mở GUI
    python run.py --edit         # biên tập các video mới (headless)
    python run.py --device cpu   # tùy chọn ép CPU; GUI mặc định tự chọn GPU/CPU

Tự tạo config.yaml từ config.example.yaml nếu chưa có.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.config import load_config
from app.sqlite_store import SQLiteStore
from app.logging_setup import setup_logging

# Khi build .exe (PyInstaller) sys.frozen=True: dữ liệu ghi cạnh file .exe, còn file
# mẫu config.example.yaml nằm trong bundle (sys._MEIPASS).
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
    BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).parent
    BUNDLE = ROOT
CONFIG = ROOT / "config.yaml"
EXAMPLE = BUNDLE / "config.example.yaml"


def ensure_config() -> None:
    if not CONFIG.exists():
        if EXAMPLE.exists():
            shutil.copy(EXAMPLE, CONFIG)
            print(f"Đã tạo {CONFIG} từ mẫu. Hãy mở ra chỉnh kênh + thư mục rồi chạy lại.")
        else:
            print("Thiếu cả config.yaml lẫn config.example.yaml.")
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="Quét + tải 1 lần rồi thoát")
    ap.add_argument("--edit", action="store_true", help="Biên tập video mới rồi thoát")
    ap.add_argument("--auto", action="store_true",
                    help="Chạy liên tục: quét định kỳ + tải xong tự đẩy sang edit (1 luồng)")
    ap.add_argument("--edit-folder", metavar="DIR", default="",
                    help="Edit ĐỘC LẬP các video trong 1 folder (không tải), bỏ qua video đã edit")
    ap.add_argument("--device", default="", choices=("", "auto", "cuda", "cpu"),
                    help="ghi đè thiết bị AI trong giao diện: auto | cuda | cpu")
    args = ap.parse_args()

    ensure_config()
    cfg = load_config(CONFIG)
    ai_device = args.device or getattr(cfg.editor, "processing_device", "auto")
    # Log ghi cạnh app (quan trọng khi chạy .exe: CWD có thể khác thư mục exe).
    log_file = cfg.logging.file
    if log_file and not Path(log_file).is_absolute():
        log_file = str(ROOT / log_file)
    setup_logging(cfg.logging.level, log_file)

    from app.preflight import check as preflight_check
    for warning in preflight_check(cfg):
        print(f"[CẢNH BÁO CẤU HÌNH] {warning}")

    # SQLite là nguồn chính; lần chạy đầu tự nhập workbook cũ để không mất lịch sử.
    db = SQLiteStore(ROOT / "data.db", legacy_excel=ROOT / "data.xlsx")
    db.reset_stuck()      # dọn trạng thái kẹt do lần chạy trước bị crash
    db.resume_queue()     # job đang chạy dở -> Interrupted để tiếp tục, giữ Completed

    if args.scan:
        from app.downloader.scheduler import ScanService
        ScanService(cfg, db, on_log=print).scan_once()
        return
    if args.edit:
        from app.editor.pipeline import EditPipeline
        EditPipeline(cfg, db, on_log=print, device=ai_device).process_pending()
        return
    if args.edit_folder:
        from app.editor.pipeline import EditPipeline
        from app.editor.local_source import ingest_folder
        res = ingest_folder(db, args.edit_folder)
        print(f"Folder '{res['channel']}': tổng {res['total']}, mới {res['added']}, "
              f"cần edit {len(res['eligible'])}, bỏ qua đã xong {res['skipped_done']}.")
        pipe = EditPipeline(cfg, db, on_log=print, device=ai_device)
        for vid in res["eligible"]:      # chỉ edit video của folder này (độc lập)
            pipe.process_one_by_id(vid)
        return
    if args.auto:
        _run_auto(cfg, db, device=ai_device)
        return

    # GUI
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow
    from app.ui.theme import apply_theme

    app = QApplication(sys.argv)
    apply_theme(app)
    win = MainWindow(cfg, db, device=ai_device, config_path=CONFIG)
    win.show()
    sys.exit(app.exec())


def _run_auto(cfg, db, device: str) -> None:
    """Luồng auto xuyên suốt: một hàng đợi edit chạy liên tục; mỗi video tải xong được
    đẩy vào hàng đợi; nếu đang edit thì nối đuôi và xử lý lần lượt tới hết."""
    import time
    from app.downloader.scheduler import ScanService
    from app.editor.pipeline import EditPipeline
    from app.editor.edit_service import EditQueueService

    pipe = EditPipeline(cfg, db, on_log=print, device=device)
    editor = EditQueueService(pipe.process_one_by_id, on_log=print)
    editor.start(initial_ids=[r.video_id for r in db.pending_edits()])  # nạp backlog

    scan = ScanService(cfg, db, on_log=print, on_downloaded=editor.enqueue)
    scan.scan_once()             # quét ngay 1 lần
    scan.start_scheduler()       # rồi quét định kỳ mỗi scan_interval_minutes
    print("Auto: đang chạy (quét định kỳ + edit liên tục). Ctrl+C để dừng.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Đang dừng…")
    finally:
        scan.stop_scheduler()
        editor.stop()


if __name__ == "__main__":
    main()
