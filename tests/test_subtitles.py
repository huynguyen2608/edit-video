"""Test phụ đề: timestamp, SRT, gộp cue ngắn, dịch (fake), burn filter, content 2 cột."""
import tempfile
from pathlib import Path

from app.config import EditorCfg
from app.editor import subtitles as sub
from app.editor import translate as tr
from app.editor import export
from app.editor import work_cache
from app.editor.export import RenderInputs
from app.editor.subtitles import Cue


def test_slice_cues_clips_and_rebases_segment_timestamps():
    cues = [
        Cue(2.0, 6.0, "trước"),
        Cue(9.0, 13.0, "giao đoạn"),
        Cue(15.0, 18.0, "sau"),
    ]
    sliced = sub.slice_cues(cues, 10.0, 16.0)
    assert [(c.start, c.end, c.text) for c in sliced] == [
        (0.0, 3.0, "giao đoạn"),
        (5.0, 6.0, "sau"),
    ]


def test_transcript_cache_roundtrip_and_invalidates_with_source_change():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.mp4"
        source.write_bytes(b"video-a")
        signature = work_cache.source_signature(str(source), "model=a")
        target = work_cache.transcript_path(tmp, "video/1")
        cues = [Cue(0.0, 1.2, "xin chào")]
        work_cache.save_transcript(target, signature, cues, "vi")
        loaded = work_cache.load_transcript(target, signature)
        assert loaded and loaded[0][0].text == "xin chào" and loaded[1] == "vi"
        source.write_bytes(b"video-b-longer")
        changed = work_cache.source_signature(str(source), "model=a")
        assert work_cache.load_transcript(target, changed) is None


def test_generic_work_cache_is_scoped_by_signature():
    with tempfile.TemporaryDirectory() as tmp:
        folder = work_cache.artifact_dir(tmp, "focus", "video/1", "abcdef123456")
        path = folder / "focus.json"
        work_cache.save_json(path, "abcdef123456", x=0.4, y=0.6)
        assert work_cache.load_json(path, "abcdef123456")["x"] == 0.4
        assert work_cache.load_json(path, "other") is None
        assert work_cache.value_signature("a", 1) == work_cache.value_signature("a", 1)
        assert work_cache.value_signature("a", 1) != work_cache.value_signature("a", 2)


def test_clear_work_cache_keeps_output_files():
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp)
        cache_file = output / ".vrs_cache" / "audio" / "cached.wav"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"cache")
        video = output / "finished.mp4"
        video.write_bytes(b"video")

        count, total_bytes = work_cache.clear_all(tmp)

        assert count == 1 and total_bytes == 5
        assert not (output / ".vrs_cache").exists()
        assert video.read_bytes() == b"video"


def test_fingerprint_applies_tiny_fps_change_without_interpolation():
    cfg = EditorCfg()
    cfg._fingerprint_fps_multiplier = 0.9995
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080, src_fps=30.0,
                      has_audio=False)
    cmd = export.build_command(cfg, ri, "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "fps=29.985000" in graph
    assert "minterpolate" not in graph


def test_preview_can_apply_same_fingerprint_as_final_render(monkeypatch=None):
    cfg = EditorCfg()
    cfg.fingerprint_enabled = True
    # Kiểm tra trực tiếp bản cấu hình mà preview/final cùng sử dụng.
    from app.editor import fingerprint
    preview_cfg = fingerprint.apply(cfg, "video-1")
    final_cfg = fingerprint.apply(cfg, "video-1")
    assert preview_cfg.speed == final_cfg.speed
    assert preview_cfg.flip_horizontal == final_cfg.flip_horizontal
    assert preview_cfg._fingerprint_fps_multiplier == final_cfg._fingerprint_fps_multiplier


def test_srt_timestamp():
    assert sub.srt_timestamp(0) == "00:00:00,000"
    assert sub.srt_timestamp(1.5) == "00:00:01,500"
    assert sub.srt_timestamp(3661.234) == "01:01:01,234"


def test_to_srt_original_and_translation():
    cues = [Cue(0, 1, "xin chào", "hello"), Cue(1, 2, "bạn khỏe không", "how are you")]
    s = sub.to_srt(cues)                       # gốc
    assert "1\n00:00:00,000 --> 00:00:01,000\nxin chào" in s
    st = sub.to_srt(cues, use_translation=True)  # hiển thị bản dịch
    assert "hello" in st and "how are you" in st and "xin chào" not in st


def test_normalize_cues_sorts_deduplicates_clamps_and_preserves_text():
    cues = [
        Cue(2.0, 5.0, "second cue"),
        Cue(0.0, 1.5, "first cue"),
        Cue(1.0, 2.0, "first cue"),
        Cue(8.0, 12.0, "last cue"),
    ]
    result = sub.normalize_cues(cues, duration=10.0)
    assert [cue.text for cue in result] == ["first cue", "second cue", "last cue"]
    assert result[0].start == 0.0 and result[0].end == 2.0
    assert result[-1].end == 10.0


def test_language_variants_do_not_trigger_unnecessary_translation():
    assert sub.languages_equivalent("en-US", "en-orig")
    assert sub.languages_equivalent("vi", "vi-VN")
    assert not sub.languages_equivalent("vi", "en")


def test_wrap_text_balances_two_lines_without_dropping_words():
    text = "This complete subtitle sentence should be balanced across exactly two readable lines"
    wrapped = sub.wrap_text(text, max_chars=46, max_lines=2)
    assert wrapped.count("\n") == 1
    assert wrapped.replace("\n", " ") == text


def test_merge_short_cues_merges_close_and_short():
    # 3 cue rất ngắn, sát nhau -> gộp thành 1 (giữ mốc đầu-cuối)
    cues = [Cue(0.0, 0.4, "a"), Cue(0.5, 0.9, "b"), Cue(1.0, 1.3, "c")]
    m = sub.merge_short_cues(cues, merge_gap_ms=300, min_cue_ms=1200)
    assert len(m) == 1
    assert m[0].start == 0.0 and m[0].end == 1.3 and m[0].text == "a b c"


def test_merge_keeps_far_apart_and_respects_max():
    cues = [Cue(0.0, 2.0, "câu một"), Cue(10.0, 12.0, "câu hai")]  # cách xa
    m = sub.merge_short_cues(cues, merge_gap_ms=300, min_cue_ms=1200)
    assert len(m) == 2


def test_split_long_whisper_cue_keeps_timing_and_short_text():
    cues = [Cue(2.0, 6.0, "one two three four five six seven eight nine ten")]
    parts = sub.split_long_cues(cues, max_words=4)
    assert [len(item.text.split()) for item in parts] == [4, 4, 2]
    assert parts[0].start == 2.0 and parts[-1].end == 6.0
    assert all(parts[i].end == parts[i + 1].start for i in range(len(parts) - 1))


def test_split_by_sentence_preserves_complete_text_and_timing():
    original = "Tie the can tab. Hide the yarns. Join in the next stitch and chain 3."
    parts = sub.split_cues_by_sentence([Cue(1.0, 7.0, original)])
    assert [item.text for item in parts] == [
        "Tie the can tab.", "Hide the yarns.",
        "Join in the next stitch and chain 3.",
    ]
    assert " ".join(item.text for item in parts) == original
    assert parts[0].start == 1.0 and parts[-1].end == 7.0
    assert all(parts[i].end == parts[i + 1].start for i in range(len(parts) - 1))


def test_sentence_without_punctuation_is_never_cut():
    text = "this is one long sentence without punctuation and every word must remain"
    parts = sub.split_cues_by_sentence([Cue(0.0, 4.0, text)])
    assert len(parts) == 1 and parts[0].text == text


def test_subtitle_blur_bottom_uses_safe_margin():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.subtitle.enabled = True
    e.subtitle.position = "blur_bottom"
    e.subtitle.font_size = 12
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      subtitle_path="D:\\out\\v.srt")
    command = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "Alignment=2" in command
    assert "FontSize=12" in command
    assert "MarginV=0" in command
    assert "MarginL=0" in command and "MarginR=0" in command


def test_subtitle_uses_independent_configurable_margins():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.subtitle.enabled = True
    e.subtitle.position = "bottom"
    e.subtitle.margin_left_percent = 10
    e.subtitle.margin_right_percent = 5
    e.subtitle.margin_bottom_percent = 12
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      subtitle_path="D:\\out\\v.srt")
    command = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "MarginL=108" in command
    assert "MarginR=54" in command
    assert "MarginV=230" in command

    e.subtitle.position = "top"
    e.subtitle.margin_top_percent = 9
    command = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "Alignment=8" in command
    assert "MarginV=173" in command


def test_translate_cues_with_fake_backend():
    cues = [Cue(0, 1, "một"), Cue(1, 2, "hai")]
    fake = lambda texts, target, source: [t.upper() + "-" + target for t in texts]
    out, ok = tr.translate_cues(cues, "en", source="vi", translator=fake)
    assert ok and out[0].text2 == "MỘT-en" and out[1].text2 == "HAI-en"


def test_translate_no_target_or_no_backend():
    cues = [Cue(0, 1, "x")]
    out, ok = tr.translate_cues(cues, "", translator=lambda *a: ["y"])
    assert ok and out[0].text2 == ""                     # target rỗng -> không dịch
    out2, ok2 = tr.translate_cues(cues, "en", backend="none")
    assert ok2 is False and out2[0].text2 == ""          # không backend -> fallback


def test_translate_rejects_missing_or_empty_sentences():
    cues = [Cue(0, 1, "one"), Cue(1, 2, "two")]
    out, ok = tr.translate_cues(
        cues, "vi", source="en",
        translator=lambda *_args: ["một"])
    assert ok is False and all(not cue.text2 for cue in out)
    out, ok = tr.translate_cues(
        cues, "vi", source="en",
        translator=lambda *_args: ["một", ""])
    assert ok is False and all(not cue.text2 for cue in out)


def test_write_content_two_columns(tmp_path):
    cues = [Cue(0, 1, "xin chào", "hello")]
    p = tmp_path / "c.txt"
    sub.write_content_txt(str(p), cues, "vi", translate_to="en")
    txt = p.read_text(encoding="utf-8")
    assert "Ngôn ngữ gốc: vi" in txt and "Dịch sang: en" in txt
    assert "gốc : xin chào" in txt and "hello" in txt


def test_export_burns_subtitle_with_style():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.subtitle.enabled = True
    e.subtitle.burn_in = True
    e.subtitle.position = "bottom"
    e.subtitle.font_size = 28
    e.subtitle.font_color = "#FFFFFF"
    e.subtitle.background_color = "#000000"
    e.subtitle.background_opacity = 0.5
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      subtitle_path="D:\\out\\v\\v.srt")
    fc = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "subtitles=" in fc and "force_style=" in fc
    assert "Alignment=2" in fc and "FontSize=28" in fc
    assert "PrimaryColour=&H00FFFFFF" in fc
    assert "BackColour=&H7F000000" in fc and "BorderStyle=3" in fc
    assert "original_size=1080x1920" in fc
    assert "D\\\\:/out/v/v.srt" in fc        # path escape (double-colon) đúng


def test_ass_subtitle_color_conversion_is_safe():
    assert export._ass_color("#112233") == "&H00332211"
    assert export._ass_color("#112233", 0.0) == "&HFF332211"
    assert export._ass_color("not-a-color") == "&H00FFFFFF"


def test_export_no_subtitle_when_disabled():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080, subtitle_path="x.srt")
    fc = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "subtitles=" not in fc            # tắt phụ đề -> không burn


def test_subtitle_is_burned_before_video_speed_to_stay_synced():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.speed = 2.0
    e.subtitle.enabled = True
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      subtitle_path="D:\\out\\v.srt")
    cmd = export.build_command(e, ri, "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.index("subtitles=") < graph.index("setpts=0.500000*PTS")


def test_intel_qsv_uses_global_quality():
    e = EditorCfg()
    e.export.video_codec = "h264_qsv"
    e.export.crf_or_cq = 20
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080)
    cmd = export.build_command(e, ri, "out.mp4")
    joined = " ".join(cmd)
    assert "-c:v h264_qsv" in joined
    assert "-global_quality 20" in joined
    assert "-crf" not in cmd and "-cq" not in cmd


def test_ass_timestamp():
    assert sub.ass_timestamp(3.0) == "0:00:03.00"
    assert sub.ass_timestamp(65.25) == "0:01:05.25"


def test_build_overlay_ass_hook_and_cta():
    items = [
        {"start": 0.0, "end": 3.0, "text": "Xem đến cuối nhé!", "position": "top",
         "font_size": 48, "box": True, "fade_ms": 300},
        {"start": 50.0, "end": 60.0, "text": "Theo dõi kênh nha", "position": "bottom",
         "font_size": 42, "box": False, "fade_ms": 0},
    ]
    a = sub.build_overlay_ass(items, 1080, 1920)
    assert "PlayResX: 1080" in a and "PlayResY: 1920" in a
    assert "{\\fad(300\\,300)\\an8\\fs48}Xem đến cuối nhé!" in a   # hook: fade + top + box style
    assert ",Box,," in a and ",Outline,," in a                    # 2 style dùng cả hai
    assert "{\\an2\\fs42}Theo dõi kênh nha" in a                  # cta: không fade, bottom
    assert "0:00:00.00,0:00:03.00" in a and "0:00:50.00,0:01:00.00" in a


def test_hook_templates_safe_margin_and_auto_fit_keep_all_text():
    text = (
        "This is a deliberately long hook sentence that must remain complete "
        "while the renderer reduces its size and wraps it safely")
    item = {
        "start": 0.0, "end": 3.0, "text": text, "position": "top",
        "font_size": 48, "style_preset": "highlight",
        "safe_margin_percent": 10, "fade_ms": 0,
    }
    ass = sub.build_overlay_ass([item], 1080, 1920)
    assert ",Highlight,,108,108,192,," in ass
    assert "\\an8\\fs" in ass
    assert text.replace(" ", "") in ass.replace("\\N", "").replace(" ", "")


def test_hook_and_cta_keep_existing_default_font_sizes():
    cfg = EditorCfg()
    assert cfg.intro_hook.font_size == 48
    assert cfg.outro_cta.font_size == 42
    assert cfg.intro_hook.safe_margin_percent == 5
    assert cfg.outro_cta.safe_margin_percent == 5


def test_pick_auto_hook():
    from app.editor.subtitles import Cue as C
    cues = [C(0, 1, "  "), C(1, 2, "Đây là bí quyết giữ chân người xem cực kỳ hiệu quả nha các bạn ơi")]
    h = sub.pick_auto_hook(cues, max_words=6)
    assert h == "Đây là bí quyết giữ chân…"
    assert sub.pick_auto_hook([]) == ""


def test_export_logo_scale_and_hook_cta_burn():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.target_aspect = "9:16"           # out_w = 1080
    e.overlay.enabled = True
    e.overlay.image_path = "logo.png"
    e.overlay.scale = 0.15             # 1080*0.15 = 162
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      overlay_ass_path="D:\\o\\v_overlay.ass")
    fc = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "scale=162:-1" in fc                          # size logo đúng
    assert "v_overlay.ass" in fc and "subtitles=" in fc  # hook/CTA burn


def test_export_no_logo_scale_when_zero():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    e.overlay.enabled = True
    e.overlay.image_path = "logo.png"   # scale mặc định 0 -> giữ nguyên
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080)
    fc = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "colorchannelmixer=aa=0.8[ovl]" in fc      # không chèn scale khi scale=0
