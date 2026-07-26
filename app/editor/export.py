"""Ghép lệnh FFmpeg cuối cùng và xuất bản: bản full + short 100s + file content.

Đây là nơi duy nhất gọi ffmpeg cho video. Filter graph video được dựng theo thứ tự:
  reframe (về khung đích) -> transform (flip/mirror/color) -> speed -> overlay -> pip
Audio (filter_complex riêng): chọn nguồn (gốc / vocals đã tách / voiceover)
  -> trộn nhạc thay thế -> tempo (đồng bộ speed) + pitch -> [aout].

Ghi chú GPU: video_codec mặc định h264_nvenc (NVIDIA). Không có GPU -> đổi libx264
trong config.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import EditorCfg
from ..logging_setup import get_logger
from .cancel import EditCancelled, run_cancellable, stop_process
from . import audio_ops, video_ops

log = get_logger("export")

_POS = {
    "top-left": "10:10",
    "top-right": "W-w-10:10",
    "bottom-left": "10:H-h-10",
    "bottom-right": "W-w-10:H-h-10",
}
# vị trí phụ đề -> Alignment ASS (numpad): 8=trên-giữa, 5=giữa, 2=dưới-giữa
_SUB_ALIGN = {"top": 8, "middle": 5, "bottom": 2}


def _escape_sub_path(path: str) -> str:
    """Escape đường dẫn cho filter subtitles.

    Windows path (D:\\a\\b.srt) phải qua HAI tầng parse (filtergraph rồi option của
    filter) nên colon ổ đĩa cần '\\\\:' (sau khi filtergraph bỏ 1 lớp còn '\\:', rồi
    option-parser bỏ nốt thành ':'), và dùng '/' thay '\\'.
    """
    return path.replace("\\", "/").replace(":", "\\\\:")


def _subtitle_filter(cfg, sub_path: str, out_w: int, out_h: int) -> str:
    scfg = cfg.subtitle
    align = _SUB_ALIGN.get(scfg.position, 2)
    style = f"Alignment={align},FontSize={int(scfg.font_size)},MarginV=40,Outline=1,Shadow=0"
    return (f"subtitles={_escape_sub_path(sub_path)}:"
            f"original_size={out_w}x{out_h}:force_style='{style}'")


@dataclass
class RenderInputs:
    video: str
    src_w: int
    src_h: int
    src_fps: float = 0.0
    focus_x: float = 0.5
    focus_y: float = 0.5
    voiceover_path: Optional[str] = None
    vocals_wav: Optional[str] = None  # nếu đã tách giọng
    has_audio: bool = True            # nguồn có track audio không (tránh map [0:a] rỗng)
    audio_codec: str = ""             # để copy audio gốc khi tương thích MP4 và không xử lý
    audio_channels: int = 2           # chọn đúng loudness mono/stereo
    subtitle_path: Optional[str] = None  # file .srt để burn lên video (nếu bật phụ đề)
    overlay_ass_path: Optional[str] = None  # file .ass chữ hook/CTA theo thời gian


def _output_short_edge(cfg: EditorCfg, ri: RenderInputs) -> int:
    requested = int(getattr(cfg.export, "output_short_edge", 0) or 0)
    if requested > 0:
        return requested
    source_short = min(int(ri.src_w), int(ri.src_h))
    for edge in (2160, 1440, 1080, 720):
        if source_short >= edge:
            return edge
    return max(360, source_short - source_short % 2)


def _build_video_filter(cfg: EditorCfg, ri: RenderInputs, input_map: dict[str, int]) -> str:
    """Dựng phần filter_complex cho VIDEO, kết thúc bằng [vout]."""
    parts: list[str] = []

    # 1) reframe -> [rf]
    short_edge = _output_short_edge(cfg, ri)
    parts.append(video_ops.build_reframe_filter(
        ri.src_w, ri.src_h, cfg.target_aspect, cfg.fill_missing,
        ri.focus_x, ri.focus_y, cfg.zoom_fill_percent, cfg.side_crop_percent, short_edge,
        cfg.side_squeeze_percent,
    ))

    # 2) transform -> [tf]
    cg = cfg.color_grading
    parts.append(video_ops.build_transform_chain(
        "rf", "tf",
        flip_horizontal=cfg.flip_horizontal,
        # Lật ngang là hflip TOÀN KHUNG. Không dùng mirror_crop cũ vì thao tác đó
        # cắt nửa trái rồi nhân đôi, gây hình đối xứng như trong preview người dùng.
        brightness=cg.brightness, contrast=cg.contrast, saturation=cg.saturation,
        color_enabled=cg.enabled, mirror_crop=False,
    ))

    # 3) speed (video) -> [sp]
    vset, _ = video_ops.speed_filters(cfg.speed)
    parts.append(f"[tf]{vset}[sp]")
    cur = "sp"

    # 4) overlay logo/watermark
    if cfg.overlay.enabled and cfg.overlay.image_path and "overlay" in input_map:
        idx = input_map["overlay"]
        pos = _POS.get(cfg.overlay.position, _POS["top-right"])
        chain = f"format=rgba,colorchannelmixer=aa={cfg.overlay.opacity}"
        if cfg.overlay.scale and cfg.overlay.scale > 0:   # chỉnh SIZE logo theo % chiều rộng
            out_w, _ = video_ops.target_resolution(cfg.target_aspect, short_edge)
            chain += f",scale={int(out_w * cfg.overlay.scale)}:-1"
        parts.append(f"[{idx}:v]{chain}[ovl];[{cur}][ovl]overlay={pos}[ov]")
        cur = "ov"

    # 5) picture-in-picture (chỉ khi có ảnh, đúng yêu cầu)
    if cfg.picture_in_picture.enabled and cfg.picture_in_picture.image_path and "pip" in input_map:
        idx = input_map["pip"]
        out_w, _ = video_ops.target_resolution(cfg.target_aspect, short_edge)
        pip_w = int(out_w * cfg.picture_in_picture.scale)
        pos = _POS.get(cfg.picture_in_picture.position, _POS["bottom-right"])
        parts.append(
            f"[{idx}:v]scale={pip_w}:-1[pipimg];"
            f"[{cur}][pipimg]overlay={pos}[pp]"
        )
        cur = "pp"

    # 6) phụ đề burn-in (chỉ ngôn ngữ đã chọn) — áp SAU khi đã về khung đích
    if cfg.subtitle.enabled and cfg.subtitle.burn_in and ri.subtitle_path:
        out_w, out_h = video_ops.target_resolution(cfg.target_aspect, short_edge)
        parts.append(
            f"[{cur}]{_subtitle_filter(cfg, ri.subtitle_path, out_w, out_h)}[sub]")
        cur = "sub"

    # 7) chữ hook (giây đầu) + CTA (giây cuối) qua file .ass (libass tự đặt vị trí/size)
    if ri.overlay_ass_path:
        parts.append(f"[{cur}]subtitles={_escape_sub_path(ri.overlay_ass_path)}[hcta]")
        cur = "hcta"

    parts.append(f"[{cur}]null[vout]")
    return ";".join(parts)


def build_command(cfg: EditorCfg, ri: RenderInputs, out_path: str,
                  duration: Optional[float] = None, ffmpeg: str = "ffmpeg",
                  start: float = 0.0) -> list[str]:
    """Dựng lệnh ffmpeg đầy đủ (list argv). duration != None -> giới hạn (bản short)."""
    a = cfg.audio
    # mute_all: xóa HẾT âm thanh -> bỏ mọi nguồn audio, không map luồng nào (-an).
    voiceover = None if a.mute_all else (ri.voiceover_path or a.voiceover or None)
    music = None if a.mute_all else (a.replace_music or None)
    # Một video chỉ có MỘT nguồn lời. Khi có voiceover/TTS, vocals đã tách chỉ
    # phục vụ transcript và tuyệt đối không được đưa vào graph để tránh chồng giọng.
    vocals = None if (a.mute_all or voiceover) else (ri.vocals_wav or None)

    # ---- inputs (bám index để tham chiếu trong filter_complex) ----
    inputs: list[str] = []
    input_map: dict[str, int] = {}
    idx = 0
    inputs += ["-i", ri.video]; input_map["video"] = idx; idx += 1          # 0:a = audio gốc
    if cfg.overlay.enabled and cfg.overlay.image_path:
        inputs += ["-loop", "1", "-i", cfg.overlay.image_path]; input_map["overlay"] = idx; idx += 1
    if cfg.picture_in_picture.enabled and cfg.picture_in_picture.image_path:
        inputs += ["-loop", "1", "-i", cfg.picture_in_picture.image_path]; input_map["pip"] = idx; idx += 1
    if voiceover:
        inputs += ["-i", voiceover]; input_map["voiceover"] = idx; idx += 1
    if vocals:
        inputs += ["-i", vocals]; input_map["vocals"] = idx; idx += 1
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]; input_map["music"] = idx; idx += 1  # lặp nhạc phủ hết clip

    # ---- filter_complex ----
    fc = _build_video_filter(cfg, ri, input_map)
    # Chỉ dùng audio gốc nếu nguồn THỰC SỰ có track audio (tránh [0:a] rỗng -> ffmpeg lỗi).
    original = "0:a" if ri.has_audio else None
    tempo_factor = max(1e-6, float(cfg.speed)) * max(1e-6, float(a.audio_speed))
    needs_proc = (bool(a.pitch_shift_semitones)
                  or bool(getattr(a, "enhance_original_voice", False))
                  or abs(tempo_factor - 1.0) > 1e-6)
    has_any_audio = bool(voiceover or vocals or original or music)

    if a.mute_all or not has_any_audio:
        audio_map = ["-an"]           # không có luồng audio nào trong output
    elif voiceover or vocals or music or needs_proc:
        fc += ";" + audio_ops.build_audio_filtergraph(
            cfg, original=original,
            voiceover=f"{input_map['voiceover']}:a" if voiceover else None,
            vocals=f"{input_map['vocals']}:a" if vocals else None,
            music=f"{input_map['music']}:a" if music else None,
            audio_channels=ri.audio_channels,
        )
        audio_map = ["-map", "[aout]"]
    else:
        audio_map = ["-map", "0:a?"]  # chỉ audio gốc, không xử lý gì

    cmd = [ffmpeg, "-y"]
    # Với đoạn bắt đầu từ 0, giới hạn input giúp giảm giải mã thừa. Với highlight
    # ở giữa video, dùng output seek SAU inputs để timestamp phụ đề/ASS vẫn khớp.
    if duration and start <= 0:
        cmd += ["-t", str(duration)]     # input option: chỉ giải mã `duration` s đầu của video (hiệu quả)
    cmd += inputs
    if start > 0:
        cmd += ["-ss", str(start)]
    cmd += ["-filter_complex", fc, "-map", "[vout]"]
    cmd += audio_map
    cmd += [
        "-c:v", cfg.export.video_codec,
        "-cq" if "nvenc" in cfg.export.video_codec else "-crf", str(cfg.export.crf_or_cq),
        "-preset", getattr(cfg.export, "encoder_preset", "slow"),
        "-pix_fmt", "yuv420p",
    ]
    if not a.mute_all:
        can_copy_audio = (getattr(cfg.export, "copy_audio_when_unchanged", True)
                          and original and not (voiceover or vocals or music or needs_proc)
                          and ri.audio_codec in ("aac", "mp3", "ac3"))
        if can_copy_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", f"{getattr(cfg.export, 'audio_bitrate_kbps', 256)}k"]
    cmd += [
        "-movflags", "+faststart",
        "-shortest",                     # chặn output chạy dài theo nhạc/voiceover lặp
    ]
    if duration:
        cmd += ["-t", str(duration)]     # output option: chặn cứng độ dài bản short
    cmd += [out_path]
    return cmd


def build_preview_command(cfg: EditorCfg, ri: RenderInputs, out_png: str,
                          at_seconds: float = 1.0, ffmpeg: str = "ffmpeg") -> list[str]:
    """Lệnh ffmpeg render 1 KHUNG xem trước (reframe + biến đổi + logo/phụ đề nếu có),
    không encode video/audio đầy đủ -> rất nhanh."""
    inputs: list[str] = ["-ss", f"{max(0.0, at_seconds):.3f}", "-i", ri.video]
    input_map: dict[str, int] = {"video": 0}
    idx = 1
    if cfg.overlay.enabled and cfg.overlay.image_path:
        inputs += ["-loop", "1", "-i", cfg.overlay.image_path]; input_map["overlay"] = idx; idx += 1
    if cfg.picture_in_picture.enabled and cfg.picture_in_picture.image_path:
        inputs += ["-loop", "1", "-i", cfg.picture_in_picture.image_path]; input_map["pip"] = idx; idx += 1
    fc = _build_video_filter(cfg, ri, input_map)
    return [ffmpeg, "-y", *inputs, "-filter_complex", fc, "-map", "[vout]",
            "-frames:v", "1", out_png]


def render(cfg: EditorCfg, ri: RenderInputs, out_path: str,
           duration: Optional[float] = None, ffmpeg: str = "ffmpeg",
           progress_cb=None, duration_hint: Optional[float] = None,
           cancel_cb=None, start: float = 0.0) -> str:
    """Render video. Nếu có progress_cb -> phát tiến trình THẬT (0..1) đọc từ FFmpeg
    `-progress` theo out_time so với `duration_hint` (giây)."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(cfg, ri, out_path, duration, ffmpeg, start)
    log.info("FFmpeg: %s", " ".join(cmd))
    if progress_cb is None and cancel_cb is None:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg lỗi ({proc.returncode}): {proc.stderr[-800:]}")
        return out_path
    # chèn -progress pipe:1 -nostats (option toàn cục, ngay sau 'ffmpeg')
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    try:
        source_seconds = (
            float(duration) if duration
            else float(duration_hint or 0.0) * max(1e-6, float(cfg.speed)))
        expected_frames = source_seconds * max(0.0, float(ri.src_fps))
        rc, err = _run_ffmpeg_progress(
            cmd, duration_hint or duration or 0.0,
            progress_cb or (lambda _f: None), cancel_cb,
            expected_frames=expected_frames)
    except EditCancelled:
        Path(out_path).unlink(missing_ok=True)
        raise
    if rc != 0:
        raise RuntimeError(f"FFmpeg lỗi ({rc}): {err[-800:]}")
    return out_path


def _run_ffmpeg_progress(cmd: list[str], duration_hint: float, progress_cb,
                         cancel_cb=None, expected_frames: float = 0.0) -> tuple[int, str]:
    """Chạy ffmpeg, đọc `-progress` từ stdout -> gọi progress_cb(fraction 0..1).

    stderr ghi ra FILE TẠM (không dùng PIPE) để tránh deadlock khi buffer stderr đầy
    trong lúc ta đang đọc stdout.
    """
    import tempfile
    import threading
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1)
        cancelled = threading.Event()

        def watch_cancel():
            while proc.poll() is None:
                if cancel_cb and cancel_cb():
                    cancelled.set()
                    stop_process(proc)
                    return
                threading.Event().wait(0.1)

        watcher = None
        if cancel_cb:
            watcher = threading.Thread(target=watch_cancel, name="ffmpeg-cancel", daemon=True)
            watcher.start()
        for line in proc.stdout:                       # từng dòng key=value
            line = line.strip()
            if line.startswith("frame=") and expected_frames > 0:
                try:
                    frame = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                # 99% là khung hình cuối; chỉ báo hoàn tất sau khi process thoát.
                progress_cb(max(0.0, min(0.99, frame / expected_frames)))
            elif (line.startswith("out_time_us=") and duration_hint > 0
                  and expected_frames <= 0):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                progress_cb(max(
                    0.0, min(0.99, us / 1_000_000.0 / duration_hint)))
        rc = proc.wait()
        if watcher:
            watcher.join(timeout=0.3)
        errf.seek(0)
        err = errf.read()
    if cancelled.is_set():
        raise EditCancelled("Đã dừng FFmpeg của video hiện tại")
    if rc == 0:
        progress_cb(1.0)
    return rc, err


def preview_frame(cfg: EditorCfg, video_path: str, out_png: str,
                  at_seconds: float = 1.0, ffmpeg: str = "ffmpeg") -> str:
    """Render MỘT khung hình đã reframe/biến đổi (không audio, không tách/transcribe)
    để XEM TRƯỚC nhanh khung hình đầu ra. Chỉ dùng cho video được chọn/đầu tiên."""
    from . import smart_crop
    dims = smart_crop.probe_dimensions(video_path)
    ri = RenderInputs(video=video_path, src_w=dims.width, src_h=dims.height, has_audio=False)
    fc = _build_video_filter(cfg, ri, {"video": 0})   # chỉ nhánh video (không overlay/pip/sub)
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-ss", str(at_seconds), "-i", video_path,
           "-filter_complex", fc, "-map", "[vout]", "-frames:v", "1", out_png]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Preview lỗi ({proc.returncode}): {proc.stderr[-400:]}")
    return out_png


def make_short_from_full(full_path: str, out_path: str, seconds: float,
                         start: float = 0.0, ffmpeg: str = "ffmpeg",
                         accurate: bool = False, video_codec: str = "libx264",
                         quality: int = 23, cancel_cb=None) -> str:
    """Tạo bản SHORT bằng cách CẮT COPY `seconds` giây từ `start` của bản full (không
    encode lại) — nhanh gần như tức thì thay vì render lại toàn bộ filter graph.
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", full_path, "-t", str(seconds)]
    if accurate:
        cmd += ["-c:v", video_codec,
                "-cq" if "nvenc" in video_codec else "-crf", str(quality),
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", out_path]
    try:
        proc = run_cancellable(cmd, cancel_cb=cancel_cb, capture_output=True, text=True)
    except EditCancelled:
        Path(out_path).unlink(missing_ok=True)
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg (short) lỗi ({proc.returncode}): {proc.stderr[-500:]}")
    return out_path
