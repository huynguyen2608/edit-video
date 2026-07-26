"""Kiểm tra môi trường trước khi khởi động job nặng."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path


def check(cfg) -> list[str]:
    """Trả danh sách cảnh báo có thể hành động; không chặn tính năng không được bật."""
    warnings: list[str] = []
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            warnings.append(f"Không tìm thấy {exe} trong PATH.")

    dcfg, ecfg = cfg.download, cfg.editor
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
