"""Xử lý audio: tách giọng/nhạc, thay nhạc, voiceover, pitch, tempo — và DỰNG
filter_complex audio để export ghép vào lệnh ffmpeg cuối.

Ưu tiên nguồn audio đích:  voiceover  >  vocals (đã tách giọng)  >  audio gốc.
Nếu có nhạc thay thế -> trộn xuống nền ở `music_volume`.
Tempo audio:
  - editor.speed áp cho CẢ video (setpts) lẫn audio -> giữ đồng bộ hình/tiếng.
  - audio.audio_speed là tinh chỉnh tempo RIÊNG cho audio (đổi dấu vân tay âm thanh);
    để = 1.0 nếu chỉ muốn đổi tốc độ chung mà không lệch tiếng.
  - KHI CÓ LỒNG TIẾNG (voiceover/TTS): BỎ QUA audio_speed, chỉ dùng editor.speed để
    giọng đọc khớp đúng tốc độ video, tránh lệch lời.

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


def audio_duration(path: str, ffprobe: str = "ffprobe") -> float:
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20)
        return max(0.0, float((result.stdout or "0").strip() or 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def compose_timed_voiceover(items, output_path: str, cancel_cb=None,
                            ffmpeg: str = "ffmpeg") -> str:
    """Ghép các file TTS vào đúng timestamp cue và co nhẹ câu dài cho vừa ô thời gian."""
    entries = [(str(path), float(start), float(end))
               for path, start, end in items if path and float(end) > float(start)]
    if not entries:
        raise RuntimeError("Không có cue lồng tiếng hợp lệ.")
    # Windows giới hạn độ dài command line. Ghép theo lô để video nhiều cue
    # không tạo một lệnh FFmpeg quá dài hoặc mở hàng trăm input cùng lúc.
    if len(entries) > 40:
        target = Path(output_path)
        batches = []
        try:
            for index in range(0, len(entries), 40):
                part = target.with_name(
                    f"{target.stem}.tts_batch_{index // 40:03d}.wav")
                chunk = entries[index:index + 40]
                compose_timed_voiceover(
                    chunk, str(part), cancel_cb=cancel_cb, ffmpeg=ffmpeg)
                batches.append((str(part), 0.0, max(item[2] for item in chunk)))
            return compose_timed_voiceover(
                batches, output_path, cancel_cb=cancel_cb, ffmpeg=ffmpeg)
        finally:
            for path, _start, _end in batches:
                Path(path).unlink(missing_ok=True)
    cmd = [ffmpeg, "-y"]
    for path, _start, _end in entries:
        cmd += ["-i", path]
    filters = []
    labels = []
    for index, (path, start, end) in enumerate(entries):
        slot = max(0.25, end - start)
        duration = audio_duration(path)
        chain = [f"aresample={_SR}"]
        if duration > slot * 1.02:
            chain.extend(_atempo_chain(duration / slot))
        delay = max(0, int(round(start * 1000)))
        chain.append(f"adelay={delay}:all=1")
        label = f"tts{index}"
        filters.append(f"[{index}:a]{','.join(chain)}[{label}]")
        labels.append(f"[{label}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
        "normalize=0,alimiter=limit=0.95[ttsout]")
    cmd += [
        "-filter_complex", ";".join(filters), "-map", "[ttsout]",
        "-ac", "2", "-ar", str(_SR), output_path,
    ]
    try:
        run_cancellable(
            cmd, cancel_cb=cancel_cb, check=True, capture_output=True, text=True)
    except EditCancelled:
        Path(output_path).unlink(missing_ok=True)
        raise
    except subprocess.CalledProcessError as exc:
        Path(output_path).unlink(missing_ok=True)
        raise RuntimeError(
            "Không thể đồng bộ lồng tiếng theo phụ đề: "
            + ((exc.stderr or "")[-600:]))
    return output_path


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


def voice_enhancement_filters(
        audio_cfg, audio_channels: int = 2) -> tuple[list[str], list[str]]:
    """Dựng bộ lọc lời thoại nhẹ: (trước khi trộn nhạc, sau khi trộn nhạc)."""
    pre: list[str] = []
    hp = int(getattr(audio_cfg, "highpass_hz", 0) or 0)
    lp = int(getattr(audio_cfg, "lowpass_hz", 0) or 0)
    noise = max(0, min(20, int(
        getattr(audio_cfg, "noise_reduction_percent", 0) or 0)))
    if hp:
        pre.append(f"highpass=f={hp}")
    if lp:
        pre.append(f"lowpass=f={lp}")
    if noise:
        pre.append(f"afftdn=nr={noise}:tn=1")

    bass = float(getattr(audio_cfg, "bass_db", 0.0) or 0.0)
    mid = float(getattr(audio_cfg, "mid_db", 0.0) or 0.0)
    treble = float(getattr(audio_cfg, "treble_db", 0.0) or 0.0)
    if abs(bass) > 1e-6:
        pre.append(f"bass=g={_fmt(bass)}:f=120:w=0.5")
    if abs(mid) > 1e-6:
        pre.append(f"equalizer=f=1500:t=q:w=1:g={_fmt(mid)}")
    if abs(treble) > 1e-6:
        pre.append(f"treble=g={_fmt(treble)}:f=8000:w=0.5")

    deesser = max(0.0, min(4.0, float(
        getattr(audio_cfg, "deesser_db", 0.0) or 0.0)))
    if deesser:
        pre.append(f"deesser=i={_fmt(deesser / 4.0)}")

    if bool(getattr(audio_cfg, "compressor_enabled", False)):
        threshold_db = float(getattr(audio_cfg, "compressor_threshold_db", -18.0))
        threshold = 10 ** (threshold_db / 20.0)
        ratio = max(1.0, min(3.0, float(
            getattr(audio_cfg, "compressor_ratio", 2.0))))
        attack = max(5.0, min(20.0, float(
            getattr(audio_cfg, "compressor_attack_ms", 10.0))))
        release = max(80.0, min(150.0, float(
            getattr(audio_cfg, "compressor_release_ms", 100.0))))
        pre.append(
            "acompressor="
            f"threshold={threshold:.6f}:ratio={_fmt(ratio)}:"
            f"attack={_fmt(attack)}:release={_fmt(release)}")

    gain = max(-1.0, min(1.0, float(
        getattr(audio_cfg, "gain_db", 0.0) or 0.0)))
    if abs(gain) > 1e-6:
        pre.append(f"volume={_fmt(gain)}dB")

    master: list[str] = []
    ceiling = max(-2.0, min(-0.5, float(
        getattr(audio_cfg, "limiter_ceiling_db", -1.0))))
    if bool(getattr(audio_cfg, "loudness_enabled", False)):
        attr = "loudness_mono_lufs" if int(audio_channels or 2) == 1 else "loudness_stereo_lufs"
        fallback = -19.0 if attr == "loudness_mono_lufs" else -16.0
        target = max(-21.0, min(-14.0, float(
            getattr(audio_cfg, attr, fallback))))
        master.append(
            f"loudnorm=I={_fmt(target)}:TP={_fmt(ceiling)}:LRA=11")
    if bool(getattr(audio_cfg, "limiter_enabled", False)):
        master.append(f"alimiter=limit={10 ** (ceiling / 20.0):.6f}")
    return pre, master


def build_audio_filtergraph(cfg, *, original: str = "0:a",
                            voiceover: str | None = None, vocals: str | None = None,
                            music: str | None = None, out_label: str = "aout",
                            audio_channels: int = 2) -> str:
    """Dựng filter_complex audio, kết thúc bằng [out_label].

    Tham số voiceover/vocals/music là NHÃN luồng ffmpeg (vd "3:a"), None = không dùng.
    """
    a = cfg.audio
    # base = nguồn giọng chính; nếu không có (nguồn im lặng, chỉ có nhạc) -> nhạc làm base.
    base = voiceover or vocals or original or music
    if base is None:
        raise ValueError("build_audio_filtergraph: không có nguồn audio nào")
    # Tinh chỉnh áp dụng cho nguồn âm thanh CHÍNH cuối cùng: audio gốc, vocals
    # đã tách, Edge TTS hoặc file voiceover.
    enhance = bool(getattr(a, "enhance_original_voice", False))
    pre_filters, master_filters = (
        voice_enhancement_filters(a, audio_channels) if enhance else ([], []))
    stmts: list[str] = [
        f"[{base}]{','.join([f'aresample={_SR}', *pre_filters])}[abase]"]
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
    # Lồng tiếng (TTS/file thu sẵn) PHẢI khớp đúng tốc độ VIDEO để không lệch lời, nên
    # chỉ áp cfg.speed (bỏ qua audio_speed vốn dùng để đổi vân tay). Không có lồng tiếng
    # mới áp thêm audio_speed cho phần audio gốc/nhạc.
    replacement_audio = bool(
        voiceover or (music and not original and not vocals))
    aspeed = (
        max(1e-6, float(a.audio_speed)) if replacement_audio else 1.0)
    tempo_factor = max(1e-6, float(cfg.speed)) * aspeed
    # apad ở cuối: nếu audio (voiceover/nhạc) NGẮN hơn video thì đệm im lặng cho đủ,
    # tránh -shortest cắt cụt phần hình còn lại.
    tail = pitch_speed_filters(a.pitch_shift_semitones, tempo_factor)
    tail = tail + master_filters
    tail = (tail or ["anull"]) + ["apad"]
    stmts.append(f"[{cur}]{','.join(tail)}[{out_label}]")
    return ";".join(stmts)


def needs_audio_filtergraph(cfg, *, has_voiceover: bool, has_vocals: bool,
                            has_music: bool, has_original: bool = True) -> bool:
    """True nếu phải dựng filter_complex audio (thay vì map thẳng audio gốc)."""
    a = cfg.audio
    replacement_audio = bool(
        has_voiceover or (has_music and not has_vocals and not has_original))
    aspeed = (
        max(1e-6, float(a.audio_speed)) if replacement_audio else 1.0)
    tempo_factor = max(1e-6, float(cfg.speed)) * aspeed
    return bool(has_voiceover or has_vocals or has_music
                or getattr(a, "enhance_original_voice", False)
                or a.pitch_shift_semitones
                or abs(tempo_factor - 1.0) > 1e-6)
