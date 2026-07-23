"""Integration ĐẦU-CUỐI luồng auto (FFmpeg thật, không cần torch/whisper/GUI):

  tải xong (mô phỏng) -> enqueue -> EditQueueService -> EditPipeline -> export
  -> ghi sheet 'exports'/'events' -> folder theo <kênh>/<video_id> với file theo id.

Kiểm 2 video, enqueue video thứ 2 khi video 1 đang xử lý -> phải xử lý LẦN LƯỢT tới hết.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import subprocess

from app.config import AppConfig
from app.store import ExcelStore
from app.editor.pipeline import EditPipeline
from app.editor.edit_service import EditQueueService


def make_clip(path, seconds=2):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def build_cfg(out_dir):
    cfg = AppConfig()
    e = cfg.editor
    e.output_dir = str(out_dir)
    e.target_aspect = "9:16"
    e.fill_missing = "blur"
    e.side_crop_percent = 5
    e.export.video_codec = "libx264"        # sandbox không GPU
    e.export.crf_or_cq = 28
    e.export.make_full = True
    e.export.make_short = True
    e.export.short_seconds = 1
    e.export.make_content_txt = False        # Whisper không có ở sandbox
    e.audio.separate_speech = False          # Demucs không có ở sandbox
    return cfg


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vrs_auto_"))
    dl = tmp / "downloads"; dl.mkdir()
    out = tmp / "output"
    store = ExcelStore(str(tmp / "data.xlsx"))

    # 2 video "đã tải xong" thuộc cùng 1 kênh (tên có dấu cách -> test safe_name)
    chan = "Kênh Của Tôi"
    ids = ["VID_aaa", "VID_bbb"]
    for vid in ids:
        clip = dl / f"{vid}.mp4"
        make_clip(clip)
        store.add_discovered(vid, "UC1", chan, f"Tiêu đề {vid}", f"http://x/{vid}", "2026-07-01")
        store.set_download_status(vid, "downloaded", path=str(clip))

    pipe = EditPipeline(cfg=build_cfg(out), db=store, on_log=lambda m: print("  ", m), device="cpu")
    svc = EditQueueService(pipe.process_one_by_id, on_log=lambda m: print("[queue]", m))

    # Mô phỏng: video 1 tải xong báo sang trước, rồi video 2 (nối đuôi)
    svc.enqueue(ids[0])
    svc.enqueue(ids[1])
    ok_wait = svc.wait_idle(timeout=180)
    svc.stop()

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(("PASS " if cond else "FAIL ") + name)

    check("wait_idle hoàn tất", ok_wait)
    from app.paths import safe_name
    for vid in ids:
        base = out / safe_name(chan) / vid
        full = base / f"{vid}_full.mp4"
        short = base / f"{vid}_short.mp4"
        check(f"{vid}: folder theo <kênh>/<id> tồn tại", base.is_dir())
        check(f"{vid}: có file full theo id", full.exists() and full.stat().st_size > 0)
        check(f"{vid}: có file short theo id", short.exists() and short.stat().st_size > 0)
        check(f"{vid}: edit_status=done", store.get_video(vid).edit_status == "done")

    exps = store.recent_exports()
    check("sheet exports có 2 dòng", len(exps) == 2)
    check("export ghi đúng output_dir + full_path",
          all(x["output_dir"] and x["full_path"] for x in exps))
    evs = store.recent_events()
    check("sheet events có log export", any(e["source"] == "export" for e in evs))
    check("sheet events có log bắt đầu edit", any("bắt đầu edit" in (e["message"] or "") for e in evs))

    n_ok = sum(1 for _, ok in results if ok)
    print(f"\nE2E auto-edit: {n_ok}/{len(results)} PASS  (tmp={tmp})")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
