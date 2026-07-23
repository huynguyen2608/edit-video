"""Test dựng config lồng nhau — lỗi từng khiến app crash (dict thay vì dataclass)."""
from app.config import _build, EditorCfg, DownloadCfg


def test_nested_dataclasses_built():
    e = _build(EditorCfg, {
        "target_aspect": "1:1",
        "color_grading": {"enabled": True, "brightness": 0.2},
        "audio": {"separate_speech": True, "audio_speed": 1.5},
        "export": {"video_codec": "libx264"},
        "picture_in_picture": {"enabled": True, "image_path": "x.png"},
    })
    assert e.target_aspect == "1:1"
    assert type(e.color_grading).__name__ == "ColorGradingCfg"
    assert e.color_grading.enabled is True
    assert abs(e.color_grading.brightness - 0.2) < 1e-9
    assert type(e.audio).__name__ == "AudioCfg"
    assert e.audio.separate_speech is True and e.audio.audio_speed == 1.5
    assert type(e.export).__name__ == "ExportCfg"
    assert e.export.video_codec == "libx264"
    assert e.picture_in_picture.enabled is True


def test_defaults_kept_for_missing_sections():
    e = _build(EditorCfg, {"target_aspect": "16:9"})
    assert e.fill_missing == "blur"                     # default giữ nguyên
    assert type(e.export).__name__ == "ExportCfg"       # section thiếu -> dataclass default
    assert e.export.make_full is True
    assert type(e.audio).__name__ == "AudioCfg"


def test_unknown_keys_ignored():
    e = _build(EditorCfg, {"khong_ton_tai": 1,
                           "color_grading": {"key_la": 2, "contrast": 1.4}})
    assert e.color_grading.contrast == 1.4


def test_channels_built_from_list():
    d = _build(DownloadCfg, {"channels": [{"name": "A", "url": "u"}, {"name": "B"}]})
    assert len(d.channels) == 2
    assert d.channels[0].name == "A" and d.channels[0].url == "u"
    assert d.channels[1].name == "B"


def test_download_update_period_options():
    d = _build(DownloadCfg, {"lookback_days": 60,
                             "history_scan": True, "history_limit": 1200,
                             "scan_interval_minutes": 45})
    assert d.lookback_days == 60
    assert d.history_scan is True and d.history_limit == 1200
    assert d.scan_interval_minutes == 45


def test_download_quality_setting():
    d = _build(DownloadCfg, {"quality_height": 720})
    assert d.quality_height == 720


def test_ai_device_defaults_to_auto_and_can_be_saved():
    assert EditorCfg().processing_device == "auto"
    e = _build(EditorCfg, {"processing_device": "cpu"})
    assert e.processing_device == "cpu"


def test_high_quality_export_defaults():
    e = EditorCfg()
    assert e.export.video_codec == "libx264"
    assert e.export.crf_or_cq == 20 and e.export.encoder_preset == "slow"
    assert e.export.output_short_edge == 0
    assert e.export.audio_bitrate_kbps == 256
    assert e.speed == 0.98 and e.audio.audio_speed == 1.0
    assert e.side_crop_percent == 2.0
    assert not e.flip_horizontal and not e.mirror_crop


def test_manual_focus_fields():
    e = _build(EditorCfg, {"crop_mode": "manual",
                           "manual_focus_x": 0.7, "manual_focus_y": 0.3})
    assert e.crop_mode == "manual"
    assert e.manual_focus_x == 0.7 and e.manual_focus_y == 0.3
