"""Integration luồng PHỤ ĐỀ đầy đủ (FFmpeg thật; tiêm fake transcribe + fake dịch để
khỏi cần Whisper/deep-translator):

  transcribe(cue) -> GỘP cue ngắn gần nhau -> DỊCH -> xuất .srt (bản dịch) +
  content.txt 2 cột -> BURN phụ đề lên full.mp4 -> log_export có srt_path.
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
from app.editor import pipeline as pl
from app.editor import transcribe, translate
from app.editor.subtitles import Cue


def make_clip(path, seconds=3):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vrs_sub_"))
    clip = tmp / "clip.mp4"
    make_clip(clip)

    # ---- tiêm fake: transcribe trả cue ngắn sát nhau + 1 cue xa; dịch = prefix EN: ----
    def fake_segments(path, model_size="small", device="cuda", language=None):
        return [Cue(0.0, 0.4, "xin"), Cue(0.5, 0.9, "chào"),
                Cue(1.0, 1.3, "bạn"), Cue(4.0, 5.0, "tạm biệt")], "vi"
    transcribe.transcribe_segments = fake_segments
    translate.resolve_backend = lambda backend="auto": (
        lambda texts, target, source: [f"EN:{t}" for t in texts])

    cfg = AppConfig()
    e = cfg.editor
    e.output_dir = str(tmp / "out")
    e.target_aspect = "9:16"; e.fill_missing = "blur"
    e.export.video_codec = "libx264"; e.export.crf_or_cq = 30
    e.export.short_seconds = 1
    e.export.make_content_txt = True
    e.subtitle.enabled = True
    e.subtitle.translate_to = "en"
    e.subtitle.burn_in = True
    e.subtitle.position = "bottom"
    e.subtitle.font_size = 26
    # hook 3s đầu + CTA giây cuối (qua pipeline -> sinh .ass + burn)
    e.intro_hook.enabled = True; e.intro_hook.text = "Xem đến cuối nhé"; e.intro_hook.seconds = 2
    e.outro_cta.enabled = True; e.outro_cta.text = "Theo dõi kênh!"; e.outro_cta.seconds = 1

    store = ExcelStore(str(tmp / "data.xlsx"))
    store.add_local_video("VID1", "KenhX", "Tiêu đề", str(clip))
    row = store.get_video("VID1")

    pipe = pl.EditPipeline(cfg, store, on_log=lambda m: None, device="cpu")
    outs = pipe.process_one(row)

    results = []
    def check(name, cond):
        results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name)

    srt = Path(outs.srt) if outs.srt else None
    check("có file .srt", srt is not None and srt.exists())
    srt_txt = srt.read_text(encoding="utf-8") if srt else ""
    # 4 cue -> gộp còn 2 (xin chào bạn | tạm biệt); srt hiển thị BẢN DỊCH
    check("srt hiển thị bản dịch (EN:)", "EN:xin chào bạn" in srt_txt)
    check("srt gộp cue ngắn (2 block)", srt_txt.count("-->") == 2)
    check("srt KHÔNG hiện tiếng gốc rời", "\nxin\n" not in srt_txt)

    content = Path(outs.content_txt).read_text(encoding="utf-8") if outs.content_txt else ""
    check("content 2 cột (gốc + en)", "gốc :" in content and "EN:" in content)

    # hook/CTA .ass sinh cạnh output
    ass = list(Path(outs.full).parent.glob("*_overlay.ass"))
    check("có file hook/CTA .ass", len(ass) == 1)
    if ass:
        atxt = ass[0].read_text(encoding="utf-8")
        check("ass có hook (an8) + CTA (an2)", "\\an8" in atxt and "\\an2" in atxt)

    check("full.mp4 tồn tại (đã burn)", outs.full and Path(outs.full).exists())
    # kiểm output là video 9:16
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", outs.full],
                       capture_output=True, text=True)
    check("full 1080x1920", r.stdout.strip() == "1080,1920")
    check("log_export có srt_path", store.recent_exports()[0]["srt_path"] == outs.srt)

    n = sum(results)
    print(f"\nE2E subtitle: {n}/{len(results)} PASS  (tmp={tmp})")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
