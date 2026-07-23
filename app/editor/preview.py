"""Xem trước NHANH: render 1 khung hình thành công (reframe + biến đổi + logo) cho
MỘT video hoặc HÀNG LOẠT video, để xem kiểu dáng trước khi render đầy đủ.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..logging_setup import get_logger
from . import export, smart_crop
from .export import RenderInputs

log = get_logger("preview")


def preview_frame(cfg, video_path: str, out_png: str, at_seconds: float = 1.0) -> str:
    """Render 1 khung xem trước cho 1 video. Trả đường dẫn PNG."""
    dims = smart_crop.probe_dimensions(video_path)
    ecfg = cfg.editor
    focus_x = focus_y = 0.5
    if ecfg.fill_missing == "none":
        if ecfg.crop_mode == "manual":
            focus_x, focus_y = ecfg.manual_focus_x, ecfg.manual_focus_y
        elif ecfg.crop_mode == "auto":
            # Dùng cùng thuật toán với render thật để preview phản ánh đúng output.
            focus_x, focus_y = smart_crop.detect_focus(video_path)
    ri = RenderInputs(video=video_path, src_w=dims.width, src_h=dims.height,
                      focus_x=focus_x, focus_y=focus_y)
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    cmd = export.build_preview_command(cfg.editor, ri, out_png, at_seconds)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Preview lỗi: {proc.stderr[-400:]}")
    return out_png


def preview_batch(cfg, video_paths: list[str], out_dir: str,
                  at_seconds: float = 1.0) -> list[dict]:
    """Xem trước HÀNG LOẠT: mỗi video 1 PNG. Trả list {video, png, ok, error}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, v in enumerate(video_paths):
        png = str(out / f"preview_{i:02d}_{Path(v).stem}.png")
        try:
            preview_frame(cfg, v, png, at_seconds)
            results.append({"video": v, "png": png, "ok": True, "error": ""})
        except Exception as e:
            log.exception("preview lỗi %s", v)
            results.append({"video": v, "png": "", "ok": False, "error": str(e)})
    log.info("Preview hàng loạt: %d/%d ok", sum(r["ok"] for r in results), len(results))
    return results
