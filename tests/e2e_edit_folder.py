"""Integration luồng EDIT ĐỘC LẬP từ folder (FFmpeg thật, KHÔNG dùng downloader):

  bỏ video vào folder -> ingest_folder -> edit các video eligible -> export ->
  output/<tên folder>/<video_id>/<id>_full.mp4|_short.mp4 + sheet exports.
Chạy lại lần 2 (đã 'done') -> eligible rỗng (bỏ qua video đã edit).
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import AppConfig
from app.store import ExcelStore
from app.editor.pipeline import EditPipeline
from app.editor.local_source import ingest_folder
from app.paths import safe_name


def make_clip(path):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)


def build_cfg(out_dir):
    cfg = AppConfig()
    e = cfg.editor
    e.output_dir = str(out_dir)
    e.target_aspect = "9:16"; e.fill_missing = "blur"; e.side_crop_percent = 5
    e.export.video_codec = "libx264"; e.export.crf_or_cq = 28
    e.export.short_seconds = 1
    e.export.make_content_txt = False      # Whisper không có ở sandbox
    e.audio.separate_speech = False        # Demucs không có ở sandbox
    return cfg


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vrs_folder_"))
    inp = tmp / "my_inputs"; inp.mkdir()
    out = tmp / "output"
    for name in ("clipA.mp4", "clipB.mp4"):
        make_clip(inp / name)

    store = ExcelStore(str(tmp / "data.xlsx"))
    cfg = build_cfg(out)
    pipe = EditPipeline(cfg, store, on_log=lambda m: None, device="cpu")

    res = ingest_folder(store, inp)
    for vid in res["eligible"]:
        pipe.process_one_by_id(vid)

    results = []

    def check(name, cond):
        results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name)

    check("ingest thấy 2 video, 2 eligible", res["total"] == 2 and len(res["eligible"]) == 2)
    check("channel = tên folder", res["channel"] == "my_inputs")
    chan_dir = out / safe_name("my_inputs")
    check("có folder output theo tên folder", chan_dir.is_dir())
    n_full = len(list(chan_dir.glob("*/*_full.mp4")))
    n_short = len(list(chan_dir.glob("*/*_short.mp4")))
    check("2 file full + 2 file short", n_full == 2 and n_short == 2)
    check("sheet exports có 2 dòng", len(store.recent_exports()) == 2)
    check("cả 2 video edit_status=done",
          all(store.get_video(v).edit_status == "done" for v in res["eligible"]))

    # lần 2: đã done -> không còn eligible (không edit lại)
    res2 = ingest_folder(store, inp)
    check("lần 2: eligible rỗng, skip 2 done", len(res2["eligible"]) == 0 and res2["skipped_done"] == 2)

    # thêm 1 video mới vào folder -> chỉ video mới eligible
    make_clip(inp / "clipC.mp4")
    res3 = ingest_folder(store, inp)
    check("thêm file mới -> đúng 1 eligible", len(res3["eligible"]) == 1)

    n_ok = sum(results)
    print(f"\nE2E edit-folder: {n_ok}/{len(results)} PASS  (tmp={tmp})")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
