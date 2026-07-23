"""Xử lý audio: tách giọng/nhạc, thay nhạc, voiceover, pitch, tempo — và DỰNG
filter_complex audio để export ghép vào lệnh ffmpeg cuối.

Ưu tiên nguồn audio đích:  voiceover  >  vocals (đã tách giọng)  >  audio gốc.
Nếu có nhạc thay thế -> trộn xuống nền ở `music_volume`.
Tempo audio = editor.speed * audio.audio_speed:
  - editor.speed áp cho CẢ video (setpts) lẫn audio -> giữ đồng bộ hình/tiếng.
  - audio.audio_speed là tinh chỉnh tempo RIÊNG cho audio (đổi dấu vân tay âm thanh);
    để = 1.0 nếu chỉ muốn đổi tốc độ chung mà không lệch tiếng.

Cảnh báo thực tế:
- separate_speech (Demucs) tách "vocals" vs "nhạc": video nói trên nền nhạc thì được
  nhưng SẼ có artifact, không sạch tuyệt đối; cần GPU để nhanh.
- pitch/tempo nặng thì giọng thoại nghe méo.

Các hàm hình-chuỗi (_atempo_chain, pitch_speed_filters, build_audio_filtergraph) là
hàm THUẦN nên unit test được mà không cần ffmpeg/torch.
"""
from __future__ import annotations

import subprocess
import shutil
import sys
from pathlib import Path

from ..logging_setup import get_logger
from .cancel import EditCancelled, run_cancellable

log = get_logger("audio_ops")

_SR = 44100  # tần số mẫu chuẩn hoá cho toàn chuỗi audio


def extract_audio(video_path: str, out_wav: str, ffmpeg: str = "ffmpeg", cancel_cb=None) -> str:
    try:
        run_cancellable(
        [ffmpeg, "-y", "-i", video_path, "-vn", "-ac", "2", "-ar", str(_SR), out_wav],
        cancel_cb=cancel_cb, check=True, capture_output=True)
    except EditCancelled:
        Path(out_wav).unlink(missing_ok=True)
        raise
    return out_wav


# Model mặc định theo backend. MDX (ONNX) nhẹ, chạy CPU tốt (khuyên cho máy yếu);
# VR Architecture cần torch; Demucs chất lượng cao nhất nhưng nặng.
_DEFAULT_MODELS = {"mdx": "UVR-MDX-NET-Voc_FT.onnx", "vr": "1_HP-UVR.pth"}
# gói CLI cần cài theo backend
_BACKEND_PKG = {"demucs": "demucs (pip install demucs)",
                "mdx": "audio-separator (pip install audio-separator)",
                "vr": "audio-separator (pip install audio-separator)"}


def separator_command(backend: str, audio_wav: str, out_dir: str,
                      model: str = "", device: str = "cuda") -> list[str]:
    """Dựng lệnh CLI tách giọng theo backend (hàm THUẦN, test được).

    - demucs  : CLI demucs --two-stems vocals
    - mdx / vr: CLI audio-separator với model tương ứng (mdx=ONNX nhẹ, vr=.pth)
    """
    if backend == "demucs":
        return ["demucs", "--two-stems", "vocals", "-d", device, "-o", out_dir, audio_wav]
    m = model or _DEFAULT_MODELS.get(backend, _DEFAULT_MODELS["mdx"])
    return ["audio-separator", audio_wav, "--model_filename", m,
            "--output_dir", out_dir, "--output_format", "WAV"]


def _find_vocals(outp: Path, backend: str) -> str:
    if backend == "demucs":
        hits = list(outp.rglob("vocals.wav"))       # demucs: <out>/<model>/<track>/vocals.wav
    else:
        # audio-separator: <base>_(Vocals)_<model>.wav
        hits = [p for p in outp.rglob("*.wav")
                if "(vocals)" in p.name.lower() or "_vocals" in p.name.lower()]
        if not hits:
            hits = list(outp.rglob("*vocal*.wav"))
    if not hits:
        raise RuntimeError("Tách giọng xong nhưng không thấy file vocals.")
    return str(hits[0])


def separate_speech(audio_wav: str, out_dir: str, device: str = "cuda",
                    backend: str = "mdx", model: str = "", cancel_cb=None) -> str:
    """Tách giữ giọng thoại (vocals), bỏ nhạc nền. Trả đường dẫn wav vocals.

    backend: mdx (audio-separator, nhẹ, CPU tốt) | demucs (GPU) | vr (torch).
    """
    outp = Path(out_dir)
    outp.mkdir(parents=True, exist_ok=True)
    # MDX/VR tự quản lý backend; Demucs cần device cụ thể. Với auto, thử GPU trước
    # và tự chạy lại bằng CPU nếu driver/runtime CUDA không dùng được.
    devices = ("cuda", "cpu") if device == "auto" and backend == "demucs" else (device,)
    last_error = None
    for chosen_device in devices:
        cmd = separator_command(backend, audio_wav, str(outp), model, chosen_device)
        try:
            return _run_separator_command(cmd, outp, backend, cancel_cb)
        except RuntimeError as exc:
            last_error = exc
            if chosen_device != "cuda" or len(devices) == 1:
                raise
            log.warning("Demucs CUDA lỗi (%s); thử lại bằng CPU.", exc)
    raise last_error or RuntimeError("Không thể tách giọng.")


def _run_separator_command(cmd: list[str], outp: Path, backend: str, cancel_cb=None) -> str:
    # Khi chạy trực tiếp .venv/Scripts/python.exe, Scripts không tự được thêm PATH.
    suffix = ".exe" if sys.platform == "win32" else ""
    sibling = Path(sys.executable).with_name(cmd[0] + suffix)
    if sibling.exists():
        cmd[0] = str(sibling)
    else:
        cmd[0] = shutil.which(cmd[0]) or cmd[0]
    log.info("Tách giọng (%s): %s", backend, " ".join(cmd))
    try:
        run_cancellable(cmd, cancel_cb=cancel_cb, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"Chưa cài {_BACKEND_PKG.get(backend, 'công cụ tách giọng')}.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Tách giọng ({backend}) lỗi: {e.stderr[-500:] if e.stderr else e}")
    return _find_vocals(outp, backend)


# --------------------------- filter builders (thuần) ---------------------------
def _fmt(f: float) -> str:
    """Số gọn, luôn có ít nhất 1 chữ số thập phân (2.0, 0.5, 1.25)."""
    s = f"{f:.6f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _atempo_chain(factor: float) -> list[str]:
    """Chuỗi atempo đạt hệ số tempo bất kỳ (mỗi atempo chỉ nhận 0.5..2.0)."""
    if factor <= 0:
        raise ValueError("tempo factor phải > 0")
    factors: list[float] = []
    remaining = factor
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return [f"atempo={_fmt(f)}" for f in factors]


def pitch_speed_filters(pitch_semitones: int, tempo_factor: float = 1.0) -> list[str]:
    """Danh sách filter cho đổi cao độ + tempo trên một luồng audio 44100Hz.

    - pitch: asetrate=44100*ratio làm cả cao độ lẫn tempo tăng theo `ratio`;
      atempo bù để tempo cuối đúng bằng `tempo_factor` (giữ cao độ đã dịch).
    - không pitch: chỉ chuỗi atempo cho `tempo_factor`.
    Trả [] nếu không phải làm gì (pitch=0 và tempo_factor≈1).
    """
    filters: list[str] = []
    ratio = 1.0
    if pitch_semitones:
        ratio = 2 ** (pitch_semitones / 12.0)
        filters.append(f"asetrate={_SR}*{ratio:.6f}")
        filters.append(f"aresample={_SR}")
    eff = tempo_factor / ratio
    if abs(eff - 1.0) > 1e-6:
        filters += _atempo_chain(eff)
    return filters


def build_audio_filtergraph(cfg, *, original: str = "0:a",
                            voiceover: str | None = None, vocals: str | None = None,
                            music: str | None = None, out_label: str = "aout") -> str:
    """Dựng filter_complex audio, kết thúc bằng [out_label].

    Tham số voiceover/vocals/music là NHÃN luồng ffmpeg (vd "3:a"), None = không dùng.
    """
    a = cfg.audio
    # base = nguồn giọng chính; nếu không có (nguồn im lặng, chỉ có nhạc) -> nhạc làm base.
    base = voiceover or vocals or original or music
    if base is None:
        raise ValueError("build_audio_filtergraph: không có nguồn audio nào")
    stmts: list[str] = [f"[{base}]aresample={_SR}[abase]"]
    cur = "abase"
    if music and music != base:
        if getattr(a, "duck_music", False):
            # Nhạc tự HẠ khi có lời thoại: dùng giọng làm sidechain nén nhạc.
            stmts.append(f"[abase]asplit=2[avoice][akey]")
            stmts.append(f"[{music}]aresample={_SR},volume={_fmt(a.music_volume)}[amus0]")
            stmts.append("[amus0][akey]sidechaincompress=threshold=0.05:ratio=6:"
                         "attack=15:release=250[amusd]")
            stmts.append("[avoice][amusd]amix=inputs=2:duration=first:normalize=0[amix]")
        else:
            stmts.append(f"[{music}]aresample={_SR},volume={_fmt(a.music_volume)}[amus]")
            # normalize=0 để giọng giữ nguyên, nhạc đã hạ theo music_volume.
            stmts.append(f"[{cur}][amus]amix=inputs=2:duration=first:normalize=0[amix]")
        cur = "amix"
    tempo_factor = max(1e-6, float(cfg.speed)) * max(1e-6, float(a.audio_speed))
    # apad ở cuối: nếu audio (voiceover/nhạc) NGẮN hơn video thì đệm im lặng cho đủ,
    # tránh -shortest cắt cụt phần hình còn lại.
    tail = pitch_speed_filters(a.pitch_shift_semitones, tempo_factor) or ["anull"]
    tail = tail + ["apad"]
    stmts.append(f"[{cur}]{','.join(tail)}[{out_label}]")
    return ";".join(stmts)


def needs_audio_filtergraph(cfg, *, has_voiceover: bool, has_vocals: bool,
                            has_music: bool) -> bool:
    """True nếu phải dựng filter_complex audio (thay vì map thẳng audio gốc)."""
    a = cfg.audio
    tempo_factor = max(1e-6, float(cfg.speed)) * max(1e-6, float(a.audio_speed))
    return bool(has_voiceover or has_vocals or has_music
                or a.pitch_shift_semitones
                or abs(tempo_factor - 1.0) > 1e-6)
