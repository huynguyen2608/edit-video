"""Edge TTS voice discovery and speech synthesis.

The dependency is imported lazily so the application can still start and edit
without dubbing when edge-tts is not installed or the service is unavailable.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .cancel import EditCancelled


def list_voices() -> list[dict]:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("Chưa cài edge-tts. Hãy cài requirements.txt rồi thử lại.") from exc
    voices = asyncio.run(edge_tts.list_voices())
    return sorted(voices, key=lambda v: (
        v.get("Locale", ""), v.get("Gender", ""), v.get("ShortName", "")))


def choose_voice(language: str, gender: str = "", preferred: str = "") -> str:
    voices = list_voices()
    lang = (language or "").lower()
    if preferred and any(
            v.get("ShortName") == preferred
            and (not lang or str(v.get("Locale", "")).lower().startswith(lang))
            and (not gender or v.get("Gender") == gender)
            for v in voices):
        return preferred
    matches = [v for v in voices
               if (not lang or str(v.get("Locale", "")).lower().startswith(lang))
               and (not gender or v.get("Gender") == gender)]
    if not matches and gender:
        matches = [v for v in voices
                   if not lang or str(v.get("Locale", "")).lower().startswith(lang)]
    if not matches:
        raise RuntimeError(f"Không tìm thấy giọng Edge TTS cho ngôn ngữ '{language}'.")
    return str(matches[0]["ShortName"])


def synthesize(text: str, output_path: str, *, language: str,
               voice: str = "", gender: str = "", rate_percent: int = 0,
               cancel_cb=None) -> tuple[str, str]:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("Chưa cài edge-tts. Hãy cài requirements.txt rồi thử lại.") from exc
    clean = " ".join((text or "").split())
    if not clean:
        raise RuntimeError("Không có nội dung transcript để tạo giọng đọc.")
    selected = choose_voice(language, gender, voice)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = f"{int(rate_percent):+d}%"
    communicate = edge_tts.Communicate(clean, selected, rate=rate)

    async def save_cancellable() -> None:
        try:
            with path.open("wb") as media:
                async for chunk in communicate.stream():
                    if cancel_cb and cancel_cb():
                        raise EditCancelled("Đã dừng tạo giọng Edge TTS")
                    if chunk.get("type") == "audio":
                        media.write(chunk["data"])
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    asyncio.run(save_cancellable())
    return str(path), selected
