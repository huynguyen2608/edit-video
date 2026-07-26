"""Test dựng config lồng nhau — lỗi từng khiến app crash (dict thay vì dataclass)."""
from app.config import _build, AppConfig, EditorCfg, DownloadCfg


def test_nested_dataclasses_built():
    e = _build(EditorCfg, {
        "target_aspect": "1:1",
        "color_grading": {"enabled": True, "brightness": 0.2},
        "audio": {"separate_speech": True, "audio_speed": 1.5},
        "tts": {"enabled": True, "language": "ja", "gender": "Male"},
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
    assert type(e.tts).__name__ == "TtsCfg"
    assert e.tts.enabled and e.tts.language == "ja" and e.tts.gender == "Male"


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


def test_short_export_can_be_disabled_while_full_remains_enabled():
    c = AppConfig()
    c.editor.export.make_short = False
    assert c.editor.export.make_full is True
    assert c.editor.export.make_short is False


def test_original_voice_enhancement_safe_defaults():
    e = EditorCfg()
    a = e.audio
    assert a.enhance_original_voice is False
    assert (a.gain_db, a.bass_db, a.mid_db, a.treble_db) == (0.0, 0.0, 0.5, 0.5)
    assert (a.highpass_hz, a.lowpass_hz, a.noise_reduction_percent) == (80, 18000, 10)
    assert a.compressor_enabled and a.compressor_ratio == 2.0
    assert a.loudness_enabled and a.loudness_stereo_lufs == -16.0
    assert a.limiter_enabled and a.limiter_ceiling_db == -1.0
    assert e.speed == 0.98 and e.audio.audio_speed == 1.0
    assert e.side_crop_percent == 2.0
    assert not e.flip_horizontal and not e.mirror_crop


def test_manual_focus_fields():
    e = _build(EditorCfg, {"crop_mode": "manual",
                           "manual_focus_x": 0.7, "manual_focus_y": 0.3})
    assert e.crop_mode == "manual"
    assert e.manual_focus_x == 0.7 and e.manual_focus_y == 0.3


def test_long_video_split_options():
    assert EditorCfg().long_video_segment_minutes == 0
    assert _build(EditorCfg, {"long_video_segment_minutes": 4}).long_video_segment_minutes == 4
