"""Phụ đề: cue, dựng SRT, và GỘP các đoạn hội thoại ngắn gần nhau.

Vì sao gộp: Whisper hay tách lời thoại thành nhiều cue rất ngắn, sát nhau. Dịch từng
mảnh vụn dễ SAI (mất ngữ cảnh, ghép nhầm câu). Ta gộp các cue ngắn/sát nhau thành 1
cue (giữ mốc thời gian đầu-cuối) trước khi dịch để bản dịch bám đúng câu.

Toàn bộ hàm ở đây là hàm THUẦN nên unit test được, không cần Whisper.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Cue:
    start: float          # giây
    end: float
    text: str             # lời thoại NGÔN NGỮ GỐC
    text2: str = ""       # bản DỊCH (nếu có)


def srt_timestamp(sec: float) -> str:
    """Giây -> 'HH:MM:SS,mmm' (định dạng SRT)."""
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue], use_translation: bool = False) -> str:
    """Dựng nội dung file .srt. use_translation=True -> hiển thị bản dịch (nếu có)."""
    blocks: list[str] = []
    for i, c in enumerate(cues, 1):
        text = (c.text2 if (use_translation and c.text2) else c.text).strip()
        blocks.append(f"{i}\n{srt_timestamp(c.start)} --> {srt_timestamp(c.end)}\n{text}\n")
    return "\n".join(blocks)


def merge_short_cues(cues: list[Cue], merge_gap_ms: int = 300,
                     min_cue_ms: int = 1200, max_cue_ms: int = 7000) -> list[Cue]:
    """Gộp cue liên tiếp khi KHOẢNG CÁCH nhỏ hoặc cue trước QUÁ NGẮN, không vượt trần.

    Giữ start của cue đầu và end của cue cuối trong nhóm -> phụ đề vẫn khớp thời gian.
    """
    if not cues:
        return []
    gap = merge_gap_ms / 1000.0
    min_dur = min_cue_ms / 1000.0
    max_dur = max_cue_ms / 1000.0
    out: list[Cue] = [Cue(cues[0].start, cues[0].end, cues[0].text.strip())]
    for c in cues[1:]:
        prev = out[-1]
        prev_dur = prev.end - prev.start
        gap_to = c.start - prev.end
        would_dur = c.end - prev.start
        if (gap_to <= gap or prev_dur < min_dur) and would_dur <= max_dur:
            prev.end = c.end
            prev.text = (prev.text + " " + c.text.strip()).strip()
        else:
            out.append(Cue(c.start, c.end, c.text.strip()))
    return out


def pick_auto_hook(cues: list[Cue], max_words: int = 12) -> str:
    """Gợi ý HOOK từ transcript: lấy câu mở đầu (cắt gọn ~max_words từ)."""
    for c in cues:
        t = (c.text or "").strip()
        if t:
            words = t.split()
            return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
    return ""


_ASS_ALIGN = {"top": 8, "middle": 5, "bottom": 2}  # numpad ASS


def ass_timestamp(sec: float) -> str:
    """Giây -> 'H:MM:SS.cc' (định dạng thời gian ASS)."""
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N").strip()


def build_overlay_ass(items: list[dict], play_w: int = 1080, play_h: int = 1920) -> str:
    """Dựng file ASS chữ-theo-thời-gian (hook/CTA).

    Mỗi item là dict: {start, end, text, position, font_size, box?, fade_ms?}.
    - box=True  -> style nền hộp bán trong suốt (dễ đọc); False -> chỉ viền.
    - fade_ms>0 -> mờ dần vào/ra bằng \\fad.
    PlayResX/Y = khung đầu ra để cỡ chữ/vị trí đúng tỉ lệ.
    """
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {play_w}\nPlayResY: {play_h}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        # Outline: viền chữ; Box: BorderStyle=3 (nền hộp)
        "Style: Outline,Arial,48,&H00FFFFFF,&H00000000,&H64000000,1,1,3,0,2,60,60,90\n"
        "Style: Box,Arial,48,&H00FFFFFF,&H00000000,&HA0000000,1,3,6,0,2,60,60,90\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    rows = []
    for it in items:
        an = _ASS_ALIGN.get(it.get("position", "bottom"), 2)
        style = "Box" if it.get("box", True) else "Outline"
        fade = int(it.get("fade_ms", 0) or 0)
        fad = f"\\fad({fade}\\,{fade})" if fade > 0 else ""
        tag = f"{{{fad}\\an{an}\\fs{int(it.get('font_size', 48))}}}"
        rows.append(f"Dialogue: 0,{ass_timestamp(it['start'])},{ass_timestamp(it['end'])},"
                    f"{style},,0,0,0,,{tag}{_ass_escape(it['text'])}")
    return header + "\n".join(rows) + "\n"


def write_content_txt(out_path: str, cues: list[Cue], language: str,
                      translate_to: str = "") -> str:
    """Ghi file content: toàn văn gốc (+ bản dịch) và bản có mốc thời gian 2 cột."""
    from pathlib import Path
    tr = bool(translate_to)
    header = f"# Ngôn ngữ gốc: {language}" + (f" | Dịch sang: {translate_to}\n\n" if tr else "\n\n")
    orig_full = " ".join(c.text.strip() for c in cues).strip()
    parts = [header, "## Toàn văn (gốc)\n", orig_full, "\n"]
    if tr:
        tr_full = " ".join((c.text2 or "").strip() for c in cues).strip()
        parts += [f"\n## Toàn văn (dịch: {translate_to})\n", tr_full, "\n"]
    parts.append("\n## Có mốc thời gian\n")
    for c in cues:
        parts.append(f"[{srt_timestamp(c.start)} --> {srt_timestamp(c.end)}]")
        parts.append(f"  gốc : {c.text.strip()}")
        if tr:
            parts.append(f"  {translate_to:<4}: {(c.text2 or '').strip()}")
    body = "\n".join(parts) + "\n"
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return out_path
