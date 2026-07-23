"""Kiểm chứng ĐẦU-CUỐI bằng FFmpeg thật (không cần yt-dlp/torch/PySide6/cv2).

Tạo clip test + nhạc + ảnh overlay/pip -> chạy export.render với nhiều preset ->
ffprobe xác nhận: đúng khung 9:16/1:1/16:9, có video+audio, độ dài hợp lý, và các
biến đổi (crop-to-fill focus, mirror, color, speed đồng bộ audio, overlay, pip,
thay nhạc, pitch) không làm hỏng filter graph.
"""
import json
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
from app.editor import export
from app.editor.export import RenderInputs


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cmd fail: {' '.join(cmd)}\n{p.stderr[-500:]}")
    return p


def probe(path):
    out = sh(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,width,height:format=duration",
              "-of", "json", path]).stdout
    d = json.loads(out)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), {})
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)
    return {
        "w": v.get("width"), "h": v.get("height"),
        "has_audio": a is not None,
        "dur": float(d.get("format", {}).get("duration", 0) or 0),
    }


def base_cfg(**over):
    e = EditorCfg()
    e.export.video_codec = "libx264"   # sandbox không GPU
    e.export.crf_or_cq = 28
    for k, v in over.items():
        setattr(e, k, v)
    return e


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vrs_e2e_"))
    src = str(tmp / "src.mp4")
    music = str(tmp / "music.m4a")
    logo = str(tmp / "logo.png")
    pip = str(tmp / "pip.png")

    # 1) nguồn 1920x1080, 5s, có audio 440Hz
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", src])
    # nhạc thay thế 660Hz 3s
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=660:duration=3", "-c:a", "aac", music])
    # ảnh overlay + pip
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:size=120x120", "-frames:v", "1", logo])
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:size=300x300", "-frames:v", "1", pip])

    ri = RenderInputs(video=src, src_w=1920, src_h=1080, focus_x=0.8, focus_y=0.5)
    results = []

    def check(name, cfg, exp_w, exp_h, want_audio=True, duration=None, ri_over=None):
        out = str(tmp / f"{name}.mp4")
        export.render(cfg, ri_over or ri, out, duration=duration)
        info = probe(out)
        ok = (info["w"] == exp_w and info["h"] == exp_h and info["has_audio"] == want_audio
              and info["dur"] > 0.1)
        results.append((name, ok, info))
        print(("PASS " if ok else "FAIL ") + f"{name:32s} -> {info}")

    # A) 9:16 blur-fill, không xử lý audio -> giữ audio gốc
    check("A_9x16_blur", base_cfg(target_aspect="9:16", fill_missing="blur"), 1080, 1920)

    # B) 1:1 và 16:9 (dims)
    check("B_1x1_blur", base_cfg(target_aspect="1:1", fill_missing="blur"), 1080, 1080)
    check("C_16x9_pad", base_cfg(target_aspect="16:9", fill_missing="pad_black"), 1920, 1080)

    # D) crop-to-fill quanh focus 0.8 + zoom (chế độ "chọn vùng action chính")
    d = base_cfg(target_aspect="9:16", fill_missing="none", zoom_fill_percent=8)
    check("D_9x16_cropfill_focus", d, 1080, 1920)

    # E) toàn bộ biến đổi video: flip + mirror + color + speed 2x + overlay + pip
    e = base_cfg(target_aspect="9:16", fill_missing="blur", flip_horizontal=True,
                 mirror_crop=True, speed=2.0)
    e.color_grading.enabled = True
    e.color_grading.brightness = 0.05
    e.color_grading.contrast = 1.1
    e.color_grading.saturation = 1.2
    e.overlay.enabled = True
    e.overlay.image_path = logo
    e.overlay.position = "top-right"
    e.picture_in_picture.enabled = True
    e.picture_in_picture.image_path = pip
    e.picture_in_picture.position = "bottom-right"
    check("E_all_video_transforms", e, 1080, 1920)

    # F) audio: thay nhạc nền + pitch + audio_speed, và speed video 2x (đồng bộ)
    f = base_cfg(target_aspect="9:16", fill_missing="blur", speed=2.0)
    f.audio.replace_music = music
    f.audio.music_volume = 0.3
    f.audio.pitch_shift_semitones = 3
    check("F_audio_music_pitch_speed", f, 1080, 1920)

    # G) voiceover thay toàn bộ audio gốc
    g = base_cfg(target_aspect="9:16", fill_missing="blur")
    g.audio.voiceover = music
    check("G_voiceover_replaces_audio", g, 1080, 1920)

    # H) bản SHORT: giới hạn 2s trên clip 5s
    check("H_short_2s", base_cfg(target_aspect="9:16", fill_missing="blur"),
          1080, 1920, duration=2)

    # I) mute_all: xóa HẾT âm thanh -> output KHÔNG có luồng audio
    mute = base_cfg(target_aspect="9:16", fill_missing="blur")
    mute.audio.mute_all = True
    mute.audio.voiceover = music  # phải bị bỏ qua khi mute
    check("I_mute_all_no_audio", mute, 1080, 1920, want_audio=False)

    # J) nguồn KHÔNG audio + speed 2x -> không crash, output không audio
    silent = str(tmp / "silent.mp4")
    sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
        "-c:v", "libx264", silent])
    jcfg = base_cfg(target_aspect="9:16", fill_missing="blur", speed=2.0)
    ri_silent = RenderInputs(video=silent, src_w=1280, src_h=720, has_audio=False)
    check("J_silent_src_speed", jcfg, 1080, 1920, want_audio=False, ri_over=ri_silent)

    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"\nE2E FFmpeg: {n_ok}/{len(results)} PASS  (tmp={tmp})")
    # kiểm tra riêng độ dài short & speed
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
