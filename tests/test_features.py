"""Test các chức năng phát triển: highlight, ducking, preset, fingerprint, report."""
from app.config import AppConfig, EditorCfg
from app import presets, report
from app.editor import analyze, audio_ops as ao, fingerprint


# ---- #3 highlight ----
def test_parse_astats_and_best_window():
    s = ("frame:0 pts_time:0.5\nlavfi.astats.Overall.RMS_level=-30.0\n"
         "frame:1 pts_time:1.0\nlavfi.astats.Overall.RMS_level=-12.0\n"
         "frame:2 pts_time:1.5\nlavfi.astats.Overall.RMS_level=-11.0\n")
    series = analyze.parse_astats(s)
    assert series == [(0.5, -30.0), (1.0, -12.0), (1.5, -11.0)]
    # cửa sổ 0.6s: [1.0,1.6) to nhất -> bắt đầu 1.0
    assert analyze.best_window(series, 0.6, 2.0) == 1.0
    # video ngắn hơn cửa sổ -> 0
    assert analyze.best_window(series, 5.0, 2.0) == 0.0


# ---- #4 ducking ----
def test_ducking_uses_sidechaincompress():
    e = EditorCfg(); e.audio.duck_music = True
    g = ao.build_audio_filtergraph(e, original="0:a", music="2:a")
    assert "sidechaincompress" in g
    e2 = EditorCfg(); e2.audio.duck_music = False
    g2 = ao.build_audio_filtergraph(e2, original="0:a", music="2:a")
    assert "sidechaincompress" not in g2 and "amix" in g2


# ---- #6 preset ----
def test_platform_preset():
    c = AppConfig()
    c.editor.subtitle.font_size = 17
    assert presets.apply_platform_preset(c, "tiktok") is True
    assert c.editor.target_aspect == "9:16" and c.editor.export.short_seconds == 60
    assert c.editor.subtitle.font_size == 17
    assert presets.apply_platform_preset(c, "youtube") is True
    assert c.editor.target_aspect == "16:9"
    assert presets.apply_platform_preset(c, "khong-co") is False


# ---- #7 fingerprint ----
def test_fingerprint_deterministic_and_bounded():
    d1 = fingerprint.deltas_for("vid1")
    assert d1 == fingerprint.deltas_for("vid1")          # xác định
    assert fingerprint.deltas_for("vid2") != d1          # khác video -> khác
    assert 0.98 - 0.01 <= d1["speed_mul"] <= 1.02 + 0.01
    assert -1.0 <= d1["fps_unit"] <= 1.0


def test_fingerprint_apply_does_not_mutate_original():
    e = EditorCfg(); e.speed = 1.0
    e2 = fingerprint.apply(e, "vid1")
    assert e2 is not e and e.speed == 1.0                # gốc không đổi
    assert e2.color_grading.enabled is True
    assert 0.9 < e2.speed < 1.1
    assert 0.999 <= e2._fingerprint_fps_multiplier <= 1.001


# ---- #10 report ----
def test_build_report_groups_by_day():
    exports = [
        {"video_id": "a", "channel_name": "C", "exported_at": "2026-07-21 10:00",
         "full_path": "f", "short_path": "s", "content_txt": "c", "srt_path": None},
        {"video_id": "b", "channel_name": "C", "exported_at": "2026-07-20 09:00",
         "full_path": "f", "short_path": None, "content_txt": None, "srt_path": None},
    ]
    events = [{"time": "2026-07-21 10:01", "level": "ERROR", "source": "edit", "message": "boom"}]
    r = report.build_report(exports, events)
    assert "Tổng video đã xuất: **2**" in r and "Số lỗi ghi nhận: **1**" in r
    assert "## 2026-07-21 — 1 video" in r and "## 2026-07-20 — 1 video" in r
    assert "boom" in r
