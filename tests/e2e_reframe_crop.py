"""Kiểm chứng ĐẦU-CUỐI tính năng crop hai bên (16:9 -> 9:16 blur) bằng FFmpeg thật.

Ma trận input bắt buộc: 1920x1080, 1280x720, độ phân giải lẻ/không chuẩn, có & không
audio, FPS 25/30/60, chủ thể ở giữa. Với mỗi input, render 9:16 blur side_crop=5 và
kiểm: output đúng 1080x1920, audio đúng có/không, không viền đen (cropdetect phủ đầy),
độ dài > 0. Ngoài ra chứng minh "video chính TO HƠN": chiều cao foreground khi crop 5%
lớn hơn khi 0% (dùng chính hàm production side_crop_expr).
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import EditorCfg
from app.editor import export, video_ops
from app.editor.export import RenderInputs


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cmd fail: {' '.join(map(str, cmd))}\n{p.stderr[-600:]}")
    return p


def probe(path):
    out = sh(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,width,height:format=duration", "-of", "json", path]).stdout
    d = json.loads(out)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), {})
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)
    return {"w": v.get("width"), "h": v.get("height"), "has_audio": a is not None,
            "dur": float(d.get("format", {}).get("duration", 0) or 0)}


def black_border_free(path, ow=1080, oh=1920):
    """Dùng cropdetect: nếu vùng "có nội dung" phủ >=98% khung -> coi như không viền đen."""
    p = subprocess.run(
        ["ffmpeg", "-i", path, "-vf", "cropdetect=limit=24:round=2:reset=0",
         "-frames:v", "6", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    crops = re.findall(r"crop=(\d+):(\d+):", p.stderr)
    if not crops:
        return False, "no cropdetect output"
    w, h = int(crops[-1][0]), int(crops[-1][1])
    cover = (w * h) / (ow * oh)
    return cover >= 0.98, f"detected {w}x{h} cover={cover:.3f}"


def make_src(tmp, name, w, h, fps, audio=True, centered=False):
    out = str(tmp / f"{name}.mkv")  # mkv+ffv1: cho phép cả độ phân giải LẺ
    vf = f"testsrc2=size={w}x{h}:rate={fps}:duration=2"
    if centered:
        # chủ thể rõ ở CHÍNH GIỮA
        vf += ",drawbox=x=(iw-iw/4)/2:y=(ih-ih/3)/2:w=iw/4:h=ih/3:color=yellow:t=fill"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", vf]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration=2"]
    cmd += ["-c:v", "ffv1"]
    if audio:
        cmd += ["-c:a", "pcm_s16le", "-shortest"]
    cmd += [out]
    sh(cmd)
    return out, w, h


def fg_height(tmp, src, pct):
    """Chiều cao foreground hiển thị (dùng CHÍNH side_crop_expr của production)."""
    sc = video_ops.side_crop_expr(pct)
    vf = f"{sc}scale=1080:1920:force_original_aspect_ratio=decrease"
    out = str(tmp / f"fg_{pct}.png")
    sh(["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", "1", out])
    return probe(out)["h"]


def cfg_blur_crop(pct=5.0):
    e = EditorCfg()
    e.target_aspect = "9:16"
    e.fill_missing = "blur"
    e.side_crop_percent = pct
    e.export.video_codec = "libx264"
    e.export.crf_or_cq = 28
    return e


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vrs_crop_"))
    cases = [
        ("1920x1080_fps30_audio_centered", 1920, 1080, 30, True, True),
        ("1280x720_fps30_audio",            1280, 720, 30, True, False),
        ("1281x721_ODD_fps30_audio",        1281, 721, 30, True, False),
        ("1920x1080_fps30_NOAUDIO",         1920, 1080, 30, False, False),
        ("1920x1080_fps25_audio",           1920, 1080, 25, True, False),
        ("1920x1080_fps60_audio",           1920, 1080, 60, True, False),
        ("640x480_4x3_nonstd_audio",        640, 480, 30, True, False),
    ]
    results = []
    for name, w, h, fps, audio, centered in cases:
        src, sw, sh_ = make_src(tmp, name, w, h, fps, audio, centered)
        cfg = cfg_blur_crop(5.0)
        ri = RenderInputs(video=src, src_w=sw, src_h=sh_)
        outp = str(tmp / f"{name}_OUT.mp4")
        export.render(cfg, ri, outp)
        info = probe(outp)
        nob, nob_msg = black_border_free(outp)
        ok = (info["w"] == 1080 and info["h"] == 1920
              and info["has_audio"] == audio and info["dur"] > 0.1 and nob)
        results.append(ok)
        print(("PASS " if ok else "FAIL ") +
              f"{name:34s} out={info['w']}x{info['h']} audio={info['has_audio']} "
              f"dur={info['dur']:.2f} noBlackBorder={nob}({nob_msg})")

    # Chứng minh "video chính TO HƠN": fg height 5% > 0% cho landscape
    src16x9 = str(tmp / "1920x1080_fps30_audio_centered.mkv")
    h0, h5 = fg_height(tmp, src16x9, 0), fg_height(tmp, src16x9, 5)
    larger = h5 > h0
    results.append(larger)
    print(("PASS " if larger else "FAIL ") +
          f"{'foreground larger with side-crop':34s} fg_h(0%)={h0}  fg_h(5%)={h5}  "
          f"(+{h5-h0}px)")

    n_ok = sum(1 for r in results if r)
    print(f"\nE2E reframe-crop: {n_ok}/{len(results)} PASS  (tmp={tmp})")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
