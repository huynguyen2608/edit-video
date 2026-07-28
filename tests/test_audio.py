"""Test dựng filter_complex audio: ưu tiên nguồn, trộn nhạc, đồng bộ tempo, mute."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.config import EditorCfg
from app.editor import audio_ops as ao
from app.editor import export
from app.editor.export import RenderInputs


def test_timed_voiceover_delays_each_cue_and_fits_long_audio():
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        class Result:
            returncode = 0
            stderr = ""
        return Result()

    durations = {"one.mp3": 1.0, "two.mp3": 4.0}
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(ao, "audio_duration", side_effect=lambda path: durations[path]), \
            patch.object(ao, "run_cancellable", side_effect=fake_run):
        out = str(Path(tmp) / "timed.wav")
        assert ao.compose_timed_voiceover([
            ("one.mp3", 1.0, 3.0),
            ("two.mp3", 5.0, 7.0),
        ], out) == out
    graph = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert "adelay=1000:all=1" in graph
    assert "adelay=5000:all=1" in graph
    assert "atempo=2.0" in graph


def test_mute_all_produces_no_audio_stream():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.audio.mute_all = True
    e.audio.voiceover = "vo.mp3"      # phải bị bỏ qua khi mute
    e.audio.replace_music = "bg.mp3"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080)
    cmd = export.build_command(e, ri, "out.mp4")
    assert "-an" in cmd
    assert "[aout]" not in cmd and "0:a?" not in cmd
    assert "-c:a" not in cmd                 # không cần codec audio
    # không thêm input voiceover/nhạc khi mute
    assert "vo.mp3" not in cmd and "bg.mp3" not in cmd


def test_default_keeps_original_audio():
    e = EditorCfg()
    e.speed = 1.0  # trường hợp người dùng chọn giữ nguyên tốc độ
    e.export.video_codec = "libx264"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080)
    cmd = export.build_command(e, ri, "out.mp4")
    assert "0:a?" in cmd and "-an" not in cmd


def test_generated_edge_voiceover_overrides_manual_voiceover():
    e = EditorCfg()
    e.audio.voiceover = "manual.mp3"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      voiceover_path="edge.mp3")
    cmd = export.build_command(e, ri, "out.mp4")
    assert "edge.mp3" in cmd
    assert "manual.mp3" not in cmd


def test_edge_voiceover_excludes_isolated_and_original_speech():
    e = EditorCfg()
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      voiceover_path="edge.mp3", vocals_wav="vocals.wav",
                      has_audio=True)
    cmd = export.build_command(e, ri, "out.mp4")
    joined = " ".join(cmd)
    assert "edge.mp3" in cmd
    assert "vocals.wav" not in cmd
    assert "[0:a]aresample" not in joined


def test_voiceover_uses_new_audio_speed():
    """Có lồng tiếng -> tempo CHỈ theo cfg.speed (bỏ audio_speed) để không lệch lời."""
    e = EditorCfg(); e.speed = 1.0; e.audio.audio_speed = 2.0; e.audio.pitch_shift_semitones = 0
    g_vo = ao.build_audio_filtergraph(e, original="0:a", voiceover="1:a")
    assert "atempo=2.0" in g_vo
    # Không lồng tiếng nhưng có nhạc -> tempo = speed*audio_speed = 2.0
    g_music = ao.build_audio_filtergraph(e, original="0:a", music="1:a")
    assert "atempo=2.0" not in g_music
    g_music_only = ao.build_audio_filtergraph(e, original=None, music="1:a")
    assert "atempo=2.0" in g_music_only


def test_edge_voiceover_can_mix_only_with_background_music():
    e = EditorCfg()
    e.audio.replace_music = "music.mp3"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      voiceover_path="edge.mp3", vocals_wav="vocals.wav")
    joined = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "edge.mp3" in joined and "music.mp3" in joined
    assert "vocals.wav" not in joined
    assert "sidechaincompress" in joined


def test_high_quality_encode_options_and_audio_copy():
    e = EditorCfg()
    e.speed = 1.0
    ri = RenderInputs("in.mp4", 1920, 1080, has_audio=True, audio_codec="aac")
    joined = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "-crf 20" in joined and "-preset medium" in joined
    assert "+faststart" not in joined
    assert "-pix_fmt yuv420p" in joined and "-c:a copy" in joined


def test_processed_audio_uses_256k():
    e = EditorCfg(); e.audio.audio_speed = 1.1
    ri = RenderInputs("in.mp4", 1920, 1080, has_audio=True, audio_codec="aac")
    joined = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "-c:a aac -b:a 256k" in joined


def test_highlight_seek_is_after_inputs_to_keep_subtitle_timestamps():
    e = EditorCfg()
    ri = RenderInputs("in.mp4", 1920, 1080)
    cmd = export.build_command(e, ri, "out.mp4", duration=60, start=120)
    assert cmd.index("-ss") > cmd.index("-i")
    assert cmd.count("-t") == 1


def test_separator_command_backends():
    # mdx (audio-separator + model ONNX mặc định)
    c = ao.separator_command("mdx", "a.wav", "/out")
    assert c[0] == "audio-separator" and "UVR-MDX-NET-Voc_FT.onnx" in c
    assert "--output_dir" in c and "/out" in c
    # demucs
    d = ao.separator_command("demucs", "a.wav", "/out", device="cpu")
    assert d[:3] == ["demucs", "--two-stems", "vocals"] and "cpu" in d
    # vr dùng model .pth mặc định
    v = ao.separator_command("vr", "a.wav", "/out")
    assert v[0] == "audio-separator" and v[c.index("--model_filename") + 1].endswith(".pth")
    # model tuỳ chỉnh ghi đè
    m = ao.separator_command("mdx", "a.wav", "/out", model="Custom.onnx")
    assert "Custom.onnx" in m and "UVR-MDX-NET-Voc_FT.onnx" not in m


def test_find_vocals_routing(tmp_path):
    # demucs: vocals.wav lồng thư mục
    (tmp_path / "htdemucs" / "a").mkdir(parents=True)
    (tmp_path / "htdemucs" / "a" / "vocals.wav").write_bytes(b"x")
    assert ao._find_vocals(tmp_path, "demucs").endswith("vocals.wav")
    # audio-separator: <base>_(Vocals)_<model>.wav
    d2 = tmp_path / "mdx"; d2.mkdir()
    (d2 / "a_(Vocals)_UVR-MDX-NET-Voc_FT.wav").write_bytes(b"x")
    assert "(Vocals)" in ao._find_vocals(d2, "mdx")


def test_silent_source_with_speed_no_crash_filter():
    # Nguồn KHÔNG audio + đổi speed: không được sinh [0:a] rỗng -> phải -an
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.speed = 2.0
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080, has_audio=False)
    cmd = export.build_command(e, ri, "out.mp4")
    assert "-an" in cmd
    assert "[0:a]" not in " ".join(cmd) and "[aout]" not in cmd


def test_silent_source_with_music_uses_music_as_audio():
    # Nguồn im lặng nhưng có nhạc thay -> nhạc thành audio (filtergraph, không lỗi)
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.audio.replace_music = "bg.mp3"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080, has_audio=False)
    cmd = export.build_command(e, ri, "out.mp4")
    assert "[aout]" in cmd and "-an" not in cmd
    assert "bg.mp3" in cmd  # nhạc được nạp làm input


def test_needs_false_when_speed_is_original():
    e = EditorCfg()
    e.speed = 1.0
    assert ao.needs_audio_filtergraph(
        e, has_voiceover=False, has_vocals=False, has_music=False) is False


def test_needs_true_on_video_speed():
    e = EditorCfg()
    e.speed = 1.5  # đổi tốc độ video -> audio cũng phải xử lý để đồng bộ
    assert ao.needs_audio_filtergraph(
        e, has_voiceover=False, has_vocals=False, has_music=False) is True


def test_needs_true_on_pitch():
    e = EditorCfg()
    e.audio.pitch_shift_semitones = 2
    assert ao.needs_audio_filtergraph(
        e, has_voiceover=False, has_vocals=False, has_music=False) is True


def test_source_priority_voiceover_over_vocals():
    e = EditorCfg()
    g = ao.build_audio_filtergraph(e, original="0:a", voiceover="3:a", vocals="4:a")
    assert g.startswith("[3:a]") and g.endswith("[aout]")


def test_source_vocals_when_no_voiceover():
    e = EditorCfg()
    g = ao.build_audio_filtergraph(e, original="0:a", vocals="4:a")
    assert g.startswith("[4:a]")


def test_music_mixed_at_volume():
    e = EditorCfg()
    e.audio.music_volume = 0.3
    g = ao.build_audio_filtergraph(e, original="0:a", music="5:a")
    assert "amix=inputs=2" in g and "normalize=0" in g and "volume=0.3" in g


def test_tempo_follows_editor_speed():
    e = EditorCfg()
    e.speed = 2.0  # video 2x -> audio tempo 2x để KHÔNG lệch tiếng
    g = ao.build_audio_filtergraph(e, original="0:a")
    assert "atempo=2.0" in g


def test_audio_speed_is_ignored_for_original_dialogue():
    e = EditorCfg()
    e.speed = 2.0
    e.audio.audio_speed = 2.0  # 2 * 2 = 4 -> hai atempo=2.0
    g = ao.build_audio_filtergraph(e, original="0:a")
    assert g.count("atempo=2.0") == 1


def test_audio_speed_applies_to_replacement_voiceover():
    e = EditorCfg()
    e.speed = 0.5
    e.audio.audio_speed = 1.5
    g = ao.build_audio_filtergraph(e, original="0:a", voiceover="1:a")
    assert "atempo=0.75" in g


def test_no_op_audio_is_anull_with_apad():
    e = EditorCfg(); e.speed = 1.0  # speed=1, audio_speed=1, pitch=0
    g = ao.build_audio_filtergraph(e, original="0:a")
    # không xử lý gì -> anull, kèm apad ở cuối để không cắt cụt video
    assert "anull,apad[aout]" in g


def test_apad_appended_when_processing():
    e = EditorCfg()
    e.speed = 2.0
    g = ao.build_audio_filtergraph(e, original="0:a")
    assert g.endswith("apad[aout]") and "atempo=2.0" in g


def test_enhance_original_voice_builds_safe_filter_order():
    e = EditorCfg()
    e.audio.enhance_original_voice = True
    g = ao.build_audio_filtergraph(e, original="0:a")
    assert g.startswith("[0:a]aresample=44100,highpass=f=80,lowpass=f=18000")
    assert "afftdn=nr=10:tn=1" in g
    assert "equalizer=f=1500" in g and "acompressor=" in g
    assert g.index("loudnorm=") < g.index("alimiter=") < g.index("apad")


def test_enhancement_can_process_replacement_voiceover():
    e = EditorCfg()
    e.audio.enhance_original_voice = True
    g = ao.build_audio_filtergraph(e, original="0:a", voiceover="1:a")
    assert g.startswith("[1:a]aresample=44100,highpass=f=80")
    assert "afftdn" in g and "loudnorm" in g


def test_enhancement_forces_audio_reencode():
    e = EditorCfg()
    e.audio.enhance_original_voice = True
    ri = RenderInputs("in.mp4", 1920, 1080, has_audio=True, audio_codec="aac")
    joined = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "-c:a aac -b:a 256k" in joined
    assert "loudnorm=I=-16.0:TP=-1.0:LRA=11" in joined


def test_mono_enhancement_uses_mono_loudness_target():
    e = EditorCfg()
    e.audio.enhance_original_voice = True
    ri = RenderInputs(
        "in.mp4", 1920, 1080, has_audio=True, audio_codec="aac", audio_channels=1)
    joined = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "loudnorm=I=-19.0:TP=-1.0:LRA=11" in joined


def test_disabled_enhancement_keeps_copy_path():
    e = EditorCfg()
    e.speed = 1.0
    e.audio.audio_speed = 1.0
    e.audio.enhance_original_voice = False
    ri = RenderInputs("in.mp4", 1920, 1080, has_audio=True, audio_codec="aac")
    joined = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "-c:a copy" in joined and "afftdn" not in joined
