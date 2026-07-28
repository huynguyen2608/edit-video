"""Phụ đề: cue, dựng SRT, và GỘP các đoạn hội thoại ngắn gần nhau.

Vì sao gộp: Whisper hay tách lời thoại thành nhiều cue rất ngắn, sát nhau. Dịch từng
mảnh vụn dễ SAI (mất ngữ cảnh, ghép nhầm câu). Ta gộp các cue ngắn/sát nhau thành 1
cue (giữ mốc thời gian đầu-cuối) trước khi dịch để bản dịch bám đúng câu.

Toàn bộ hàm ở đây là hàm THUẦN nên unit test được, không cần Whisper.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from pathlib import Path
import re


@dataclass
class Cue:
    start: float          # giây
    end: float
    text: str             # lời thoại NGÔN NGỮ GỐC
    text2: str = ""       # bản DỊCH (nếu có)


_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_TAG = re.compile(r"<[^>]+>")
_ASS_TAG = re.compile(r"\{[^}]*\}")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def _clean_caption(text: str) -> str:
    text = _TAG.sub("", unescape(text)).replace("\\N", "\n").replace("\\n", "\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def parse_srt(path: str | Path) -> list[Cue]:
    """Đọc SRT nguồn thành cue cho phụ đề, dịch, hook và lồng tiếng."""
    body = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", body.strip()):
        lines = block.splitlines()
        time_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if time_idx < 0:
            continue
        match = _SRT_TIME.search(lines[time_idx])
        if not match:
            continue
        values = match.groups()
        text = _clean_caption("\n".join(lines[time_idx + 1:]))
        if text:
            cues.append(Cue(_seconds(*values[:4]), _seconds(*values[4:]), text))
    return cues


def _ass_seconds(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        return 0.0
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def parse_ass(path: str | Path) -> list[Cue]:
    """Đọc Dialogue của ASS và bỏ tag định dạng khi dùng nội dung cho TTS."""
    cues: list[Cue] = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.lstrip().lower().startswith("dialogue:"):
            continue
        fields = line.split(":", 1)[1].split(",", 9)
        if len(fields) < 10:
            continue
        text = _clean_caption(_ASS_TAG.sub("", fields[9]))
        if text:
            cues.append(Cue(_ass_seconds(fields[1]), _ass_seconds(fields[2]), text))
    return cues


def read_subtitle(path: str | Path) -> list[Cue]:
    if Path(path).suffix.lower() == ".srt":
        return parse_srt(path)
    if Path(path).suffix.lower() == ".ass":
        return parse_ass(path)
    return []


def normalize_language(code: str) -> str:
    """Chuẩn hóa mã ngôn ngữ YouTube/Whisper để tránh dịch lại cùng một ngôn ngữ."""
    value = str(code or "").strip().lower().replace("_", "-")
    value = re.sub(r"-(orig|auto)$", "", value)
    aliases = {"iw": "he", "in": "id", "jp": "ja", "kr": "ko", "zh-hans": "zh-cn"}
    return aliases.get(value, value)


def languages_equivalent(first: str, second: str) -> bool:
    a, b = normalize_language(first), normalize_language(second)
    if not a or not b:
        return False
    return a == b or a.split("-", 1)[0] == b.split("-", 1)[0]


def normalize_cues(cues: list[Cue], duration: float = 0.0) -> list[Cue]:
    """Làm sạch cue nhưng không tự phân lại timestamp hay làm mất nội dung."""
    limit = max(0.0, float(duration or 0.0))
    ordered = sorted(cues, key=lambda cue: (float(cue.start), float(cue.end)))
    out: list[Cue] = []
    for cue in ordered:
        start = max(0.0, float(cue.start))
        end = max(start, float(cue.end))
        if limit:
            start, end = min(start, limit), min(end, limit)
        text = " ".join(str(cue.text or "").split()).strip()
        text2 = " ".join(str(cue.text2 or "").split()).strip()
        if not text or end - start < 0.05:
            continue
        if out and text.casefold() == out[-1].text.casefold() and start <= out[-1].end + 0.25:
            out[-1].end = max(out[-1].end, end)
            if text2 and not out[-1].text2:
                out[-1].text2 = text2
            continue
        if out and start < out[-1].end:
            # Cắt phần chồng của cue trước nếu vẫn giữ được ít nhất 100 ms.
            if start - out[-1].start >= 0.10:
                out[-1].end = start
        out.append(Cue(start, end, text, text2))
    return out


def wrap_text(text: str, max_chars: int = 42, max_lines: int = 2) -> str:
    """Ngắt tại khoảng trắng, tối đa ``max_lines`` khi có thể và tuyệt đối không bỏ chữ."""
    clean = " ".join(str(text or "").replace("\n", " ").split()).strip()
    if not clean or len(clean) <= max_chars:
        return clean
    words = clean.split()
    if max_lines == 2:
        best = None
        for index in range(1, len(words)):
            left, right = " ".join(words[:index]), " ".join(words[index:])
            if len(left) <= max_chars and len(right) <= max_chars:
                score = abs(len(left) - len(right))
                if best is None or score < best[0]:
                    best = (score, left, right)
        if best:
            return best[1] + "\n" + best[2]
    # Câu quá dài để vừa số dòng cho phép: giữ đủ chữ và để libass tự wrap.
    return clean


def find_source_subtitle(video_path: str | Path) -> tuple[Path | None, str]:
    """Tìm sidecar ``video.<lang>.srt/ass`` tải cùng video."""
    video = Path(video_path)
    candidates: list[Path] = []
    for ext in (".srt", ".ass"):
        candidates.extend(video.parent.glob(f"{video.stem}*{ext}"))
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None, ""

    def rank(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        automatic = 1 if any(x in name for x in (".auto.", ".live_chat.")) else 0
        return (0 if path.suffix.lower() == ".srt" else 1, automatic, name)

    selected = sorted(candidates, key=rank)[0]
    remainder = selected.stem[len(video.stem):].strip(".")
    lang = remainder.split(".")[0].replace("-orig", "") if remainder else ""
    return selected, lang


def srt_timestamp(sec: float) -> str:
    """Giây -> 'HH:MM:SS,mmm' (định dạng SRT)."""
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue], use_translation: bool = False,
           max_chars: int = 0, max_lines: int = 2) -> str:
    """Dựng nội dung file .srt. use_translation=True -> hiển thị bản dịch (nếu có)."""
    blocks: list[str] = []
    for i, c in enumerate(cues, 1):
        text = (c.text2 if (use_translation and c.text2) else c.text).strip()
        if max_chars > 0:
            text = wrap_text(text, max_chars=max_chars, max_lines=max_lines)
        blocks.append(f"{i}\n{srt_timestamp(c.start)} --> {srt_timestamp(c.end)}\n{text}\n")
    return "\n".join(blocks)


def slice_cues(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """Lấy cue giao với một đoạn video và đưa timestamp đoạn đó về mốc 0."""
    start = max(0.0, float(start))
    end = max(start, float(end))
    out: list[Cue] = []
    for cue in cues:
        clipped_start = max(float(cue.start), start)
        clipped_end = min(float(cue.end), end)
        if clipped_end <= clipped_start:
            continue
        out.append(Cue(
            clipped_start - start,
            clipped_end - start,
            cue.text,
            cue.text2,
        ))
    return out


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


def split_long_cues(cues: list[Cue], max_words: int = 7) -> list[Cue]:
    """Chia cue Whisper dài thành các dòng ngắn và phân bổ lại thời gian.

    Whisper có thể trả cả một đoạn nhiều câu trong một cue. Burn nguyên cue làm
    chữ chiếm vùng nội dung và khác nhịp với caption có sẵn trong video.
    """
    max_words = max(2, int(max_words))
    out: list[Cue] = []
    for cue in cues:
        words = cue.text.split()
        if len(words) <= max_words:
            out.append(Cue(cue.start, cue.end, cue.text.strip(), cue.text2))
            continue
        chunks = [
            " ".join(words[i:i + max_words])
            for i in range(0, len(words), max_words)
        ]
        duration = max(0.001, cue.end - cue.start)
        total_words = max(1, len(words))
        cursor = cue.start
        consumed = 0
        for index, chunk in enumerate(chunks):
            chunk_words = len(chunk.split())
            consumed += chunk_words
            end = (cue.end if index == len(chunks) - 1 else
                   cue.start + duration * consumed / total_words)
            out.append(Cue(cursor, end, chunk))
            cursor = end
    return out


def split_cues_by_sentence(cues: list[Cue]) -> list[Cue]:
    """Tách cue nhiều câu thành từng câu hoàn chỉnh, không cắt hoặc bỏ chữ.

    Thời gian của cue gốc được chia theo độ dài ký tự của từng câu. Nếu Whisper
    không tạo dấu kết thúc câu, giữ nguyên toàn bộ cue để libass tự xuống dòng.
    """
    out: list[Cue] = []
    for cue in cues:
        text = " ".join(cue.text.split()).strip()
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?…])\s+", text)
            if part.strip()
        ]
        if len(sentences) <= 1:
            out.append(Cue(cue.start, cue.end, text, cue.text2))
            continue
        weights = [max(1, len(sentence)) for sentence in sentences]
        total_weight = sum(weights)
        duration = max(0.001, cue.end - cue.start)
        cursor = cue.start
        elapsed_weight = 0
        for index, (sentence, weight) in enumerate(zip(sentences, weights)):
            elapsed_weight += weight
            end = (cue.end if index == len(sentences) - 1 else
                   cue.start + duration * elapsed_weight / total_weight)
            out.append(Cue(cursor, end, sentence))
            cursor = end
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


def _fit_overlay_text(text: str, font_size: int, play_w: int,
                      safe_margin_percent: int = 5) -> tuple[str, int]:
    """Giữ đủ nội dung Hook/CTA trong tối đa hai dòng khi kích thước cho phép."""
    clean = " ".join(str(text or "").split()).strip()
    size = max(16, int(font_size or 48))
    margin = max(0, min(40, int(safe_margin_percent or 0)))
    available = max(120, int(play_w * (1.0 - margin * 2 / 100.0)))
    # Bề rộng ký tự trung bình của font sans đậm xấp xỉ 0.56em.
    while size > 16:
        chars = max(8, int(available / max(1.0, size * 0.56)))
        if len(clean) <= chars * 2:
            break
        size -= 1
    chars = max(8, int(available / max(1.0, size * 0.56)))
    return wrap_text(clean, max_chars=chars, max_lines=2), size


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
        "Style: Minimal,Arial,48,&H00FFFFFF,&H00000000,&H00000000,1,1,3,0,2,0,0,0\n"
        "Style: SoftBox,Arial,48,&H00FFFFFF,&H00000000,&HA0000000,1,3,6,0,2,0,0,0\n"
        "Style: Highlight,Arial,48,&H0000EFFF,&H00000000,&H00000000,1,1,4,1,2,0,0,0\n"
        "Style: TitleBar,Arial,48,&H00FFFFFF,&H00000000,&H880F172A,1,3,14,0,2,0,0,0\n"
        "Style: Outline,Arial,48,&H00FFFFFF,&H00000000,&H64000000,1,1,3,0,2,0,0,0\n"
        "Style: Box,Arial,48,&H00FFFFFF,&H00000000,&HA0000000,1,3,6,0,2,0,0,0\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    rows = []
    for it in items:
        an = _ASS_ALIGN.get(it.get("position", "bottom"), 2)
        preset = str(it.get("style_preset", "custom") or "custom")
        style = {
            "minimal": "Minimal",
            "soft_box": "SoftBox",
            "highlight": "Highlight",
            "title_bar": "TitleBar",
        }.get(preset, "Box" if it.get("box", True) else "Outline")
        fade = int(it.get("fade_ms", 0) or 0)
        fad = f"\\fad({fade}\\,{fade})" if fade > 0 else ""
        safe = max(0, min(40, int(it.get("safe_margin_percent", 5) or 0)))
        margin_h = int(round(play_w * safe / 100.0))
        margin_v = int(round(play_h * safe / 100.0))
        fitted_text, fitted_size = _fit_overlay_text(
            it.get("text", ""), int(it.get("font_size", 48)), play_w, safe)
        tag = f"{{{fad}\\an{an}\\fs{fitted_size}}}"
        rows.append(f"Dialogue: 0,{ass_timestamp(it['start'])},{ass_timestamp(it['end'])},"
                    f"{style},,{margin_h},{margin_h},{margin_v},,"
                    f"{tag}{_ass_escape(fitted_text)}")
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
