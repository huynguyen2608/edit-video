"""Test hình học crop + dựng filter — nơi dễ sai nhất."""
import math

import pytest

from app.editor import video_ops as vo


def test_reframe_squeeze_only_for_16_9():
    # Nguồn ĐÚNG 16:9 + squeeze -> có bước co ngang [sqz] và scale nén rộng.
    f = vo.build_reframe_filter(1920, 1080, "9:16", "blur", side_squeeze_percent=10)
    assert "[sqz]" in f and "scale=trunc(iw*0.9000/2)*2:ih" in f
    # Chế độ crop-to-fill (none) cũng áp dụng co ngang cho 16:9.
    assert "[sqz]" in vo.build_reframe_filter(1920, 1080, "9:16", "none", side_squeeze_percent=8)


def test_reframe_no_squeeze_when_not_16_9_or_zero():
    # Nguồn 9:16 (không phải 16:9) -> KHÔNG co ngang.
    assert "[sqz]" not in vo.build_reframe_filter(1080, 1920, "9:16", "blur", side_squeeze_percent=10)
    # Nguồn 4:3 -> KHÔNG co ngang (chỉ đúng 16:9).
    assert "[sqz]" not in vo.build_reframe_filter(1440, 1080, "9:16", "blur", side_squeeze_percent=10)
    # squeeze = 0 -> KHÔNG co ngang.
    assert "[sqz]" not in vo.build_reframe_filter(1920, 1080, "9:16", "blur", side_squeeze_percent=0)


def test_target_resolution():
    assert vo.target_resolution("9:16") == (1080, 1920)
    assert vo.target_resolution("1:1") == (1080, 1080)
    assert vo.target_resolution("16:9") == (1920, 1080)
    with pytest.raises(ValueError):
        vo.target_resolution("4:3")


def test_crop_landscape_to_portrait_aspect_and_bounds():
    # 1920x1080 -> 9:16
    ar = 1080 / 1920
    r = vo.compute_crop_rect(1920, 1080, ar)
    # đúng tỉ lệ (sai số do làm tròn chẵn)
    assert abs((r.w / r.h) - ar) < 0.02
    # nằm trong khung
    assert 0 <= r.x and r.x + r.w <= 1920
    assert 0 <= r.y and r.y + r.h <= 1080
    # chiều cao lấp đầy vì source rộng hơn
    assert r.h == 1080


def test_crop_focus_offset_clamped():
    ar = 1080 / 1920
    # focus lệch hẳn sang phải -> vẫn không tràn biên
    r = vo.compute_crop_rect(1920, 1080, ar, focus_x=0.95)
    assert r.x + r.w <= 1920
    # và phải dịch sang phải so với canh giữa
    center = vo.compute_crop_rect(1920, 1080, ar, focus_x=0.5)
    assert r.x >= center.x


def test_crop_zoom_shrinks_region():
    ar = 1080 / 1920
    base = vo.compute_crop_rect(1920, 1080, ar, zoom_percent=0)
    zoomed = vo.compute_crop_rect(1920, 1080, ar, zoom_percent=10)
    assert zoomed.w < base.w and zoomed.h < base.h


def test_even_dimensions():
    r = vo.compute_crop_rect(1921, 1081, 0.5625, zoom_percent=7)
    assert r.w % 2 == 0 and r.h % 2 == 0
    assert r.x % 2 == 0 and r.y % 2 == 0


def test_reframe_blur_vs_none():
    blur = vo.build_reframe_filter(1920, 1080, "9:16", fill_missing="blur")
    assert "gblur" in blur and blur.endswith("[rf]")
    none = vo.build_reframe_filter(1920, 1080, "9:16", fill_missing="none")
    assert "crop=" in none and "gblur" not in none and none.endswith("[rf]")


def test_side_crop_expr():
    assert vo.side_crop_expr(0) == ""
    assert vo.side_crop_expr(5) == "crop=trunc(iw*0.9000/2)*2:ih:trunc(iw*0.0500):0,"
    # kẹp 0..10
    assert vo.side_crop_expr(50) == vo.side_crop_expr(10)
    assert vo.side_crop_expr(-3) == ""
    # theo % chiều rộng (iw) nên độc lập độ phân giải
    assert "iw" in vo.side_crop_expr(10) and "trunc(iw*0.8000/2)*2" in vo.side_crop_expr(10)


def test_four_edge_crop_expr():
    f = vo.four_edge_crop_expr(5)
    assert "iw*0.9000" in f and "ih*0.9000" in f
    assert "iw*0.0500" in f and "ih*0.0500" in f


def test_reframe_blur_side_crop_applied_to_foreground_only():
    f = vo.build_reframe_filter(1920, 1080, "9:16", fill_missing="blur", side_crop_percent=5)
    # foreground bị cắt hai bên TRƯỚC khi scale
    assert "[fg]crop=trunc(iw*0.9000/2)*2:ih:trunc(iw*0.0500):0,scale=1080:1920" in f
    # nền KHÔNG bị cắt — vẫn scale phủ đầy + blur
    assert "[bg]scale=1080:1920:force_original_aspect_ratio=increase" in f
    assert "gblur" in f and f.endswith("[rf]")


def test_reframe_blur_no_side_crop_when_zero():
    f = vo.build_reframe_filter(1920, 1080, "9:16", fill_missing="blur", side_crop_percent=0)
    assert "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fgs]" in f
    assert "crop=trunc" not in f


def test_reframe_existing_9x16_crops_four_edges_without_blur():
    f = vo.build_reframe_filter(1080, 1920, "9:16", fill_missing="blur", side_crop_percent=5)
    assert "iw*0.9000" in f and "ih*0.9000" in f
    assert "scale=1080:1920:flags=lanczos[rf]" in f
    assert "gblur" not in f and "overlay" not in f and "split" not in f


def test_reframe_square_still_uses_side_crop_and_blur():
    f = vo.build_reframe_filter(1080, 1080, "9:16", fill_missing="blur", side_crop_percent=5)
    assert "crop=trunc(iw*0.9000/2)*2:ih" in f
    assert "gblur" in f and "overlay" in f


def test_export_flip_is_full_frame_and_never_half_mirror():
    from app.config import EditorCfg
    from app.editor import export
    e = EditorCfg(); e.flip_horizontal = True; e.mirror_crop = True
    ri = export.RenderInputs("in.mp4", 1080, 1920)
    graph = export._build_video_filter(e, ri, {"video": 0})
    assert "hflip" in graph
    assert "hstack" not in graph and "crop=iw/2" not in graph


def test_mask_regions_render_before_new_logo_and_support_three_modes():
    from app.config import EditorCfg, MaskRegionCfg
    from app.editor import export
    e = EditorCfg()
    e.mask_regions = [
        MaskRegionCfg(mode="blur", x=.1, y=.2, width=.3, height=.1),
        MaskRegionCfg(mode="pixelate", x=.2, y=.3, width=.2, height=.1),
        MaskRegionCfg(mode="solid", x=.1, y=.8, width=.8, height=.1,
                      color="#000000", opacity=.7),
    ]
    e.overlay.enabled = True; e.overlay.image_path = "logo.png"
    ri = export.RenderInputs("in.mp4", 1080, 1920)
    graph = export._build_video_filter(e, ri, {"video": 0, "overlay": 1})
    assert "boxblur=" in graph
    assert "flags=neighbor" in graph
    assert "drawbox=" in graph
    assert graph.index("drawbox=") < graph.index("[1:v]format=rgba")


def test_mask_region_time_range_is_in_filter_graph():
    from app.config import EditorCfg, MaskRegionCfg
    from app.editor import export
    e = EditorCfg(); e.speed = 1.0; e.mask_regions = [MaskRegionCfg(
        mode="solid", start_seconds=2.5, end_seconds=8.0)]
    graph = export._build_video_filter(
        e, export.RenderInputs("in.mp4", 1920, 1080), {"video": 0})
    assert "between(t\\,2.500\\,8.000)" in graph


def test_mask_runs_before_new_subtitle_and_time_tracks_output_speed():
    from app.config import EditorCfg, MaskRegionCfg
    from app.editor import export
    e = EditorCfg(); e.speed = 1.2
    e.subtitle.enabled = True; e.subtitle.burn_in = True
    e.mask_regions = [MaskRegionCfg(
        mode="solid", start_seconds=2.0, end_seconds=5.0)]
    graph = export._build_video_filter(
        e, export.RenderInputs("in.mp4", 1920, 1080,
                               subtitle_path="new.srt"), {"video": 0})
    assert "between(t\\,2.400\\,6.000)" in graph
    assert graph.index("drawbox=") < graph.index("subtitles=")


def test_old_subtitle_mask_follows_new_subtitle_cues(tmp_path):
    from app.config import EditorCfg, MaskRegionCfg
    from app.editor import export
    sub = tmp_path / "new.srt"
    sub.write_text(
        "1\n00:00:02,000 --> 00:00:04,000\nHello\n\n"
        "2\n00:00:06,000 --> 00:00:07,000\nWorld\n",
        encoding="utf-8")
    e = EditorCfg()
    e.subtitle.enabled = True; e.subtitle.burn_in = True
    e.mask_regions = [MaskRegionCfg(
        purpose="old_subtitle", mode="solid", timing_mode="subtitle",
        subtitle_pad_before=.10, subtitle_pad_after=.15)]
    graph = export._build_video_filter(
        e, export.RenderInputs("in.mp4", 1920, 1080,
                               subtitle_path=str(sub)), {"video": 0})
    assert "between(t\\,1.900\\,4.150)" in graph
    assert "between(t\\,5.900\\,7.150)" in graph
    assert graph.index("drawbox=") < graph.index("subtitles=")


def test_old_subtitle_mask_is_disabled_without_new_subtitle():
    from app.config import EditorCfg, MaskRegionCfg
    from app.editor import export
    e = EditorCfg(); e.mask_regions = [MaskRegionCfg(
        purpose="old_subtitle", mode="solid", timing_mode="subtitle")]
    graph = export._build_video_filter(
        e, export.RenderInputs("in.mp4", 1920, 1080), {"video": 0})
    assert "enable='0'" in graph


def test_reframe_pad_black_unaffected_by_side_crop():
    f = vo.build_reframe_filter(1920, 1080, "9:16", fill_missing="pad_black", side_crop_percent=5)
    assert "pad=" in f and "crop=trunc" not in f


def test_speed_filters_chain_atempo():
    v, a = vo.speed_filters(1.0)
    assert "atempo=1.0" in ",".join(a) or "atempo=1.000000" in ",".join(a)
    # tốc độ 4x -> phải chuỗi 2 atempo=2.0 (mỗi cái tối đa 2.0)
    _, a4 = vo.speed_filters(4.0)
    assert a4.count("atempo=2.0") == 2
    # tốc độ 0.25 -> 2 atempo=0.5
    _, aq = vo.speed_filters(0.25)
    assert aq.count("atempo=0.5") == 2


def test_speed_invalid():
    with pytest.raises(ValueError):
        vo.speed_filters(0)
