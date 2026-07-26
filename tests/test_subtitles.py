"""Test phụ đề: timestamp, SRT, gộp cue ngắn, dịch (fake), burn filter, content 2 cột."""
from app.config import EditorCfg
from app.editor import subtitles as sub
from app.editor import translate as tr
from app.editor import export
from app.editor.export import RenderInputs
from app.editor.subtitles import Cue


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
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080,
                      subtitle_path="D:\\out\\v\\v.srt")
    fc = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "subtitles=" in fc and "force_style=" in fc
    assert "Alignment=2" in fc and "FontSize=28" in fc
    assert "original_size=1080x1920" in fc
    assert "D\\\\:/out/v/v.srt" in fc        # path escape (double-colon) đúng


def test_export_no_subtitle_when_disabled():
    e = EditorCfg()
    e.export.video_codec = "libx264"
    ri = RenderInputs(video="in.mp4", src_w=1920, src_h=1080, subtitle_path="x.srt")
    fc = " ".join(export.build_command(e, ri, "out.mp4"))
    assert "subtitles=" not in fc            # tắt phụ đề -> không burn


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
