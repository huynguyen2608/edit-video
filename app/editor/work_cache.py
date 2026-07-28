"""Cache trung gian nằm ngoài thư mục kết quả cuối của từng video."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .subtitles import Cue


def source_signature(path: str, variant: str = "") -> str:
    p = Path(path)
    stat = p.stat()
    raw = f"{p.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{variant}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def value_signature(*values) -> str:
    raw = "|".join(str(value) for value in values)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def transcript_path(output_dir: str, video_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in video_id)
    return Path(output_dir) / ".vrs_cache" / "transcripts" / f"{safe_id}.json"


def artifact_dir(output_dir: str, category: str, video_id: str,
                 signature: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in video_id)
    return (Path(output_dir) / ".vrs_cache" / category /
            safe_id / signature[:16])


def load_json(path: Path, signature: str):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if raw.get("signature") == signature else None
    except (OSError, ValueError, TypeError):
        return None


def save_json(path: Path, signature: str, **data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"signature": signature, **data}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_transcript(path: Path, signature: str):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("signature") != signature:
            return None
        cues = [
            Cue(float(item["start"]), float(item["end"]), str(item["text"]),
                str(item.get("text2", "")))
            for item in raw.get("cues", [])
        ]
        return cues, str(raw.get("language", ""))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_transcript(path: Path, signature: str, cues, language: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signature": signature,
        "language": language,
        "cues": [
            {"start": cue.start, "end": cue.end, "text": cue.text, "text2": cue.text2}
            for cue in cues
        ],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def clear_all(output_dir: str) -> tuple[int, int]:
    """Remove intermediate cache without touching source or exported videos."""
    root = Path(output_dir).resolve() / ".vrs_cache"
    if root.name != ".vrs_cache" or not root.exists():
        return 0, 0
    files = [path for path in root.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    shutil.rmtree(root)
    return len(files), total_bytes
