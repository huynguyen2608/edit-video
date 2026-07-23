"""Dịch phụ đề với độ chính xác cao, MIỄN PHÍ, không cần API key.

Backend (import trễ, tùy chọn — không cài vẫn chạy, chỉ là không dịch):
  - "google": deep-translator (Google, chất lượng cao, free/không key, cần mạng)
  - "argos" : argostranslate (dịch máy neural OFFLINE, free)
  - "auto"  : thử google trước, rồi argos
  - "none"  : không dịch

translate_cues() gộp toàn bộ cue đã-gộp lại và dịch để giữ ngữ cảnh câu; nếu không có
backend -> trả cues nguyên trạng + ok=False (pipeline sẽ dùng ngôn ngữ gốc, có cảnh báo).
"""
from __future__ import annotations

from typing import Callable, Optional

from ..logging_setup import get_logger

log = get_logger("translate")

# translator: callable(texts, target, source) -> list[str] cùng độ dài
Translator = Callable[[list[str], str, Optional[str]], list[str]]


def _google(texts: list[str], target: str, source: Optional[str]) -> list[str]:
    from deep_translator import GoogleTranslator
    gt = GoogleTranslator(source=source or "auto", target=target)
    out = gt.translate_batch(texts)
    return [o if isinstance(o, str) else "" for o in out]


def _argos(texts: list[str], target: str, source: Optional[str]) -> list[str]:
    import argostranslate.translate as at
    src = source or "auto"
    return [at.translate(t, src, target) for t in texts]


def resolve_backend(backend: str = "auto") -> Optional[Translator]:
    """Trả hàm dịch khả dụng, hoặc None nếu không có công cụ nào."""
    if backend == "none":
        return None
    order = {"auto": ("google", "argos"), "google": ("google",),
             "argos": ("argos",)}.get(backend, ("google", "argos"))
    for name in order:
        fn = {"google": _google, "argos": _argos}[name]
        try:
            fn([""], "en", None)   # thử khả dụng (import + gọi rỗng)
            return fn
        except Exception:
            continue
    return None


def translate_cues(cues, target: str, source: Optional[str] = None,
                   backend: str = "auto", translator: Optional[Translator] = None):
    """Điền cue.text2 = bản dịch. Trả (cues, ok).

    - target rỗng -> không dịch, ok=True.
    - Không có backend -> ok=False (giữ nguyên).
    translator: tiêm hàm dịch (dùng cho test); nếu None thì tự resolve theo backend.
    """
    if not target:
        return cues, True
    if not cues:
        return cues, True
    fn = translator or resolve_backend(backend)
    if fn is None:
        log.warning("Không có công cụ dịch (pip install deep-translator hoặc argostranslate).")
        return cues, False
    try:
        translated = fn([c.text for c in cues], target, source)
    except Exception as e:
        log.exception("Dịch lỗi: %s", e)
        return cues, False
    for c, t in zip(cues, translated):
        c.text2 = (t or "").strip()
    return cues, True
