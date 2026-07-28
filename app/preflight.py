"""Kiểm tra môi trường trước khi khởi động job nặng."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

_ENCODER_TRIAL_CACHE: dict[tuple[str, str], str] = {}


def check(cfg) -> list[str]:
    """Trả danh sách cảnh báo có thể hành động; không chặn tính năng không được bật."""
    warnings: list[str] = []
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            warnings.append(f"Không tìm thấy {exe} trong PATH.")

    dcfg, ecfg = cfg.download, cfg.editor
    codec = str(ecfg.export.video_codec or "")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and ("qsv" in codec or "nvenc" in codec):
        try:
            encoders = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=8).stdout
            if codec not in encoders:
                warnings.append(
                    f"FFmpeg hiện tại không có encoder {codec}; hãy dùng bản FFmpeg "
                    "có hỗ trợ GPU hoặc chọn libx264.")
        except (OSError, subprocess.SubprocessError):
            warnings.append(f"Không kiểm tra được khả năng GPU của FFmpeg cho {codec}.")
    if dcfg.cookies_file and not Path(dcfg.cookies_file).is_file():
        warnings.append(f"Không thấy cookies_file: {dcfg.cookies_file}")
    if ecfg.audio.separate_speech:
        backend = ecfg.audio.separator_backend
        module = "audio_separator" if backend in ("mdx", "vr") else "demucs"
        if importlib.util.find_spec(module) is None:
            warnings.append(f"Đã bật tách giọng nhưng thiếu module {module}.")
    need_speech = (ecfg.subtitle.enabled or ecfg.tts.enabled or ecfg.export.make_content_txt or
                   (ecfg.intro_hook.enabled and ecfg.intro_hook.auto))
    if need_speech and importlib.util.find_spec("faster_whisper") is None:
        warnings.append("Đã bật phụ đề/content nhưng thiếu faster-whisper.")
    if ((ecfg.subtitle.enabled and ecfg.subtitle.translate_to)
            or (ecfg.tts.enabled and not ecfg.subtitle.enabled and ecfg.tts.language)):
        has_translator = (importlib.util.find_spec("deep_translator") is not None or
                          importlib.util.find_spec("argostranslate") is not None)
        if not has_translator:
            warnings.append("Đã bật dịch nhưng thiếu deep-translator/argostranslate.")
    if ecfg.tts.enabled and importlib.util.find_spec("edge_tts") is None:
        warnings.append("Đã bật lồng tiếng nhưng thiếu edge-tts.")
    if ecfg.tts.enabled and ecfg.audio.mute_all:
        warnings.append("Lồng tiếng Edge TTS đang bật nhưng 'Xóa hết âm thanh' sẽ loại bỏ cả giọng đọc.")
    for label, enabled, path in (
        ("logo", ecfg.overlay.enabled, ecfg.overlay.image_path),
        ("picture-in-picture", ecfg.picture_in_picture.enabled, ecfg.picture_in_picture.image_path),
        ("nhạc thay thế", bool(ecfg.audio.replace_music), ecfg.audio.replace_music),
        ("voiceover", bool(ecfg.audio.voiceover), ecfg.audio.voiceover),
    ):
        if enabled and (not path or not Path(path).is_file()):
            warnings.append(f"Đã bật {label} nhưng file không tồn tại: {path or '(trống)'}")
    return warnings


def _encoder_trial(codec: str, ffmpeg: str = "ffmpeg") -> str:
    """Mã hóa thử một clip cực nhỏ; trả lỗi rút gọn hoặc rỗng khi thành công."""
    if not codec or codec in ("libx264", "libx265"):
        return ""
    with tempfile.TemporaryDirectory(prefix="vrs_preflight_") as tmp:
        out = str(Path(tmp) / "probe.mp4")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=0.2",
            "-an", "-c:v", codec, "-frames:v", "2", out,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        if proc.returncode == 0 and Path(out).is_file():
            return ""
        message = (proc.stderr or proc.stdout or f"FFmpeg trả mã {proc.returncode}").strip()
        return message[-500:]


def _cached_encoder_trial(codec: str, ffmpeg: str) -> str:
    key = (str(Path(ffmpeg).resolve()), codec)
    if key not in _ENCODER_TRIAL_CACHE:
        _ENCODER_TRIAL_CACHE[key] = _encoder_trial(codec, ffmpeg)
    return _ENCODER_TRIAL_CACHE[key]


def _available_memory_bytes() -> int:
    """RAM khả dụng trên Windows; trả 0 nếu hệ điều hành không cung cấp."""
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    except Exception:
        pass
    return 0


def runtime_check(cfg, source_paths=()) -> dict:
    """Kiểm tra thực tế trước hàng đợi nặng; không thay đổi cấu hình."""
    warnings = check(cfg)
    blockers: list[str] = []
    info: list[str] = []
    ffmpeg = shutil.which("ffmpeg")
    codec = str(cfg.editor.export.video_codec or "")

    if ffmpeg and ("qsv" in codec or "nvenc" in codec):
        failure = _cached_encoder_trial(codec, ffmpeg)
        if failure:
            blockers.append(
                f"Codec {codec} không mã hóa thử được. Hãy chọn libx264 hoặc codec GPU "
                f"tương thích.\n{failure}")
        else:
            info.append(f"Mã hóa {codec}: hoạt động")

    missing = [str(path) for path in source_paths if path and not Path(path).is_file()]
    if missing:
        blockers.append(f"Không tìm thấy {len(missing)} file nguồn; ví dụ: {missing[0]}")

    output = Path(cfg.editor.output_dir)
    probe_dir = output if output.exists() else output.parent
    try:
        free = shutil.disk_usage(probe_dir).free
        info.append(f"Ổ đĩa đầu ra còn {free / (1024 ** 3):.1f} GB")
        if free < 2 * 1024 ** 3:
            blockers.append("Ổ đĩa đầu ra còn dưới 2 GB.")
        elif free < 8 * 1024 ** 3:
            warnings.append("Ổ đĩa đầu ra còn dưới 8 GB; video dài có thể hết chỗ tạm.")
    except OSError:
        warnings.append(f"Không kiểm tra được dung lượng tại {probe_dir}.")

    available = _available_memory_bytes()
    if available:
        gib = available / (1024 ** 3)
        info.append(f"RAM khả dụng {gib:.1f} GB")
        if gib < 1.0:
            blockers.append("RAM khả dụng dưới 1 GB; không an toàn để bắt đầu biên tập.")
        elif gib < 2.0:
            warnings.append(
                "RAM khả dụng dưới 2 GB; nên đóng ứng dụng khác và chia video dài thành 4 phút.")
    return {"blockers": blockers, "warnings": list(dict.fromkeys(warnings)), "info": info}
