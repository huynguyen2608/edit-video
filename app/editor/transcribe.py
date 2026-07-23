"""Tạo file content .txt từ nội dung nói trong video (transcribe).

Dùng faster-whisper (nhanh hơn openai-whisper, chạy tốt trên GPU). Xuất:
- transcript đầy đủ (.txt)
- có thể mở rộng: tiêu đề/mô tả/hashtag gợi ý (giai đoạn sau, có thể nối LLM).

Import trễ + fallback rõ nếu chưa cài, để không chặn các phần khác.
"""
from __future__ import annotations

from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("transcribe")


# Cache model theo (kích thước, device, compute_type) -> nạp 1 lần, dùng cho cả list
# video (tránh nạp lại Whisper mỗi video, tiết kiệm nhiều thời gian khi chạy hàng loạt).
_MODEL_CACHE: dict = {}


def _get_model(model_size: str, device: str, compute_type: str):
    if device == "auto":
        # Không chỉ kiểm tra có GPU: thử khởi tạo thật để phát hiện driver CUDA cũ,
        # runtime không tương thích hoặc thiếu thư viện, rồi hạ về CPU an toàn.
        try:
            return _get_model(model_size, "cuda", "float16")
        except Exception as exc:
            log.warning("CUDA không khả dụng cho Whisper (%s); chuyển sang CPU int8.", exc)
            return _get_model(model_size, "cpu", "int8")
    key = (model_size, device, compute_type)
    model = _MODEL_CACHE.get(key)
    if model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError("Chưa cài faster-whisper. pip install -r requirements-ml.txt")
        log.info("Nạp Whisper (%s, %s)…", model_size, device)
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
    return model


def transcribe_to_txt(video_or_audio: str, out_txt: str,
                      model_size: str = "small", device: str = "cuda",
                      language: str | None = None) -> str:
    """Transcribe và ghi ra out_txt. Trả đường dẫn out_txt.

    language=None -> tự nhận diện (phù hợp video tiếng Việt lẫn ngôn ngữ khác).
    Model được cache theo (size, device) nên gọi nhiều video chỉ nạp 1 lần.
    """
    cues, lang = transcribe_segments(video_or_audio, model_size, device, language)
    from .subtitles import write_content_txt
    return write_content_txt(out_txt, cues, lang)


def transcribe_segments(video_or_audio: str, model_size: str = "small",
                        device: str = "cuda", language: str | None = None):
    """Transcribe -> (list[Cue], ngôn_ngữ_nhận_diện). Dùng cho content + phụ đề."""
    if device == "auto":
        try:
            return transcribe_segments(video_or_audio, model_size, "cuda", language)
        except Exception as exc:
            # Lỗi CUDA có thể chỉ xuất hiện khi duyệt generator segments, không chỉ
            # lúc tạo model, nên fallback bao trọn toàn bộ lần nhận diện.
            log.warning("Whisper CUDA lỗi (%s); tự chuyển sang CPU int8.", exc)
            return transcribe_segments(video_or_audio, model_size, "cpu", language)
    from .subtitles import Cue
    compute_type = "float16" if device == "cuda" else "int8"
    model = _get_model(model_size, device, compute_type)
    segments, info = model.transcribe(video_or_audio, language=language, vad_filter=True)
    cues = [Cue(float(s.start), float(s.end), s.text.strip()) for s in segments]
    log.info("Transcribe %s: %d cue, ngôn ngữ %s", video_or_audio, len(cues), info.language)
    return cues, info.language


def _fmt(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
