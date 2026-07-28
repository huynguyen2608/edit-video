"""Xem trước NHANH: render 1 khung hình thành công (reframe + biến đổi + logo) cho
MỘT video hoặc HÀNG LOẠT video, để xem kiểu dáng trước khi render đầy đủ.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..logging_setup import get_logger
from . import export, fingerprint, smart_crop, subtitles, video_ops
from .export import RenderInputs

log = get_logger("preview")


def preview_frame(cfg, video_path: str, out_png: str, at_seconds: float = 1.0,
                  overlay_target: str = "logo", video_id: str = "") -> str:
    """Render 1 khung xem trước cho 1 video. Trả đường dẫn PNG."""
    dims = smart_crop.probe_dimensions(video_path)
    ecfg = (
        fingerprint.apply(cfg.editor, video_id)
        if video_id and cfg.editor.fingerprint_enabled else cfg.editor)
    focus_x = focus_y = 0.5
    if ecfg.fill_missing == "none":
        if ecfg.crop_mode == "manual":
            focus_x, focus_y = ecfg.manual_focus_x, ecfg.manual_focus_y
        elif ecfg.crop_mode == "auto":
            # Dùng cùng thuật toán với render thật để preview phản ánh đúng output.
            focus_x, focus_y = smart_crop.detect_focus(video_path)
    ri = RenderInputs(video=video_path, src_w=dims.width, src_h=dims.height,
                      focus_x=focus_x, focus_y=focus_y)
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Preview trước đây chỉ có hình + logo nên đổi fontsize không thể nhìn thấy.
    # Ưu tiên một câu thật đủ dài để kiểm tra wrap/font/nền; timestamp preview
    # được kéo dài vì đây là ảnh tĩnh, không làm thay đổi sidecar nguồn.
    if ecfg.subtitle.enabled and ecfg.subtitle.burn_in:
        sub_path = out.with_name(out.stem + "_subtitle.srt")
        source_subtitle, _lang = subtitles.find_source_subtitle(video_path)
        source_cues = (
            subtitles.normalize_cues(subtitles.read_subtitle(source_subtitle))
            if source_subtitle else [])
        sample = (
            max(source_cues, key=lambda cue: len(cue.text)).text
            if source_cues else "Phụ đề xem trước với nội dung đủ dài để kiểm tra cách xuống dòng")
        max_chars = 40 if ecfg.target_aspect in {"9:16", "1:1"} else 50
        sub_path.write_text(
            subtitles.to_srt(
                [subtitles.Cue(0.0, 600.0, sample)],
                max_chars=max_chars, max_lines=2),
            encoding="utf-8",
        )
        ri.subtitle_path = str(sub_path)
    if overlay_target in ("hook", "cta"):
        item_cfg = ecfg.intro_hook if overlay_target == "hook" else ecfg.outro_cta
        text = item_cfg.text.strip() or (
            "Hook xem trước" if overlay_target == "hook" else "CTA xem trước")
        short_edge = export._output_short_edge(ecfg, ri)
        ow, oh = video_ops.target_resolution(ecfg.target_aspect, short_edge)
        ass_path = out.with_name(out.stem + f"_{overlay_target}.ass")
        ass_path.write_text(subtitles.build_overlay_ass([{
            "start": 0.0, "end": 600.0, "text": text,
            "position": item_cfg.position, "font_size": item_cfg.font_size,
            "box": item_cfg.box, "style_preset": item_cfg.style_preset,
            "safe_margin_percent": item_cfg.safe_margin_percent, "fade_ms": 0,
        }], ow, oh), encoding="utf-8")
        ri.overlay_ass_path = str(ass_path)
    cmd = export.build_preview_command(ecfg, ri, out_png, at_seconds)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Preview lỗi: {proc.stderr[-400:]}")
    return out_png


def preview_batch(cfg, video_paths: list[str], out_dir: str,
                  at_seconds: float = 1.0, overlay_target: str = "logo",
                  video_ids: list[str] | None = None) -> list[dict]:
    """Xem trước HÀNG LOẠT: mỗi video 1 PNG. Trả list {video, png, ok, error}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, v in enumerate(video_paths):
        png = str(out / f"preview_{i:02d}_{Path(v).stem}.png")
        try:
            video_id = video_ids[i] if video_ids and i < len(video_ids) else ""
            preview_frame(cfg, v, png, at_seconds, overlay_target, video_id)
            results.append({"video": v, "png": png, "ok": True, "error": ""})
        except Exception as e:
            log.exception("preview lỗi %s", v)
            results.append({"video": v, "png": "", "ok": False, "error": str(e)})
    log.info("Preview hàng loạt: %d/%d ok", sum(r["ok"] for r in results), len(results))
    return results
