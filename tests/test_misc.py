"""Test path an toàn Windows, bóc video id, filter audio."""
import sys
import threading
import time

from app.paths import safe_name, video_dir, output_video_path, existing_output_video_path
from app.downloader.monitor import _extract_vid, extract_video_id, in_date_range
from app.editor.audio_ops import pitch_speed_filters
from app.editor.cancel import EditCancelled, run_cancellable
from app.downloader.downloader import quality_format, _looks_like_cookie_error
from app.editor import transcribe
from app.config import AppConfig
from app.editor.pipeline import EditPipeline
from app.store import ExcelStore


def test_cookie_error_dpapi_detected():
    """yt-dlp #10927: message ngoài cùng 'Failed to decrypt with DPAPI' (không có 'cookie')."""
    class CookieLoadError(Exception): pass
    class DownloadError(Exception): pass
    try:
        try:
            raise CookieLoadError("failed to load cookies")
        except CookieLoadError as inner:
            raise DownloadError("ERROR: ERROR: Failed to decrypt with DPAPI. See ... for info") from inner
    except DownloadError as e:
        assert _looks_like_cookie_error(e) is True


def test_cookie_error_via_context_chain():
    """Nguyên nhân là CookieLoadError dù message ngoài cùng không có từ khóa."""
    class CookieLoadError(Exception): pass
    try:
        try:
            raise CookieLoadError("failed to load cookies")
        except CookieLoadError:
            raise RuntimeError("download aborted")
    except RuntimeError as e:
        assert _looks_like_cookie_error(e) is True


def test_non_cookie_error_not_flagged():
    assert _looks_like_cookie_error(RuntimeError("HTTP Error 404: Not Found")) is False
    assert _looks_like_cookie_error(ValueError("network timeout")) is False


def test_cookie_lock_permission_error_detected():
    """yt-dlp #7271: Chrome mở -> 'Could not copy Chrome cookie database' + PermissionError."""
    class CookieLoadError(Exception): pass
    class DownloadError(Exception): pass
    try:
        try:
            try:
                raise PermissionError(13, "Permission denied",
                                      r"C:\...\Google\Chrome\User Data\Default\Network\Cookies")
            except PermissionError:
                raise CookieLoadError("failed to load cookies")
        except CookieLoadError as inner:
            raise DownloadError("ERROR: Could not copy Chrome cookie database.") from inner
    except DownloadError as e:
        assert _looks_like_cookie_error(e) is True


def test_needs_authentication():
    from app.downloader.downloader import _needs_authentication
    assert _needs_authentication(RuntimeError(
        "[youtube] X: Sign in to confirm you’re not a bot.")) is True
    assert _needs_authentication(RuntimeError("Private video")) is True
    assert _needs_authentication(RuntimeError("HTTP Error 500")) is False


def test_pipeline_skips_srt_when_no_cues(tmp_path):
    """Bug FFmpeg: transcription rỗng -> KHÔNG tạo srt_path (tránh burn .srt rỗng)."""
    import types
    from app.config import AppConfig
    from app.editor import pipeline as pl, transcribe as tr
    cfg = AppConfig()
    cfg.editor.subtitle.enabled = True
    cfg.editor.audio.separate_speech = False
    cfg.editor.tts.enabled = False
    cfg.editor.export.make_content_txt = False
    cfg.editor.intro_hook.enabled = False
    orig = tr.transcribe_segments
    tr.transcribe_segments = lambda *a, **k: ([], "en")     # không nhận được lời thoại
    try:
        pipe = pl.EditPipeline(cfg, None, device="cpu")
        row = types.SimpleNamespace(video_id="v", download_path=str(tmp_path / "v.mp4"))
        _, content_txt, srt_path, _, gen_vo = pipe._prepare_audio_and_content(
            cfg.editor, row, str(tmp_path / "v.mp4"), tmp_path)
        assert srt_path is None                              # không burn phụ đề rỗng
        assert not list(tmp_path.glob("*.srt"))              # không tạo file .srt nào
    finally:
        tr.transcribe_segments = orig


def test_long_video_parts_keep_original_name_and_same_output_folder(tmp_path):
    import subprocess
    from pathlib import Path
    from app.config import AppConfig
    from app.editor import pipeline as pl
    from app.editor.pipeline import EditOutputs
    from app.store import VideoRow

    class DB:
        def set_edit_status(self, *_args, **_kwargs): pass

    cfg = AppConfig()
    pipe = pl.EditPipeline(cfg, DB())
    source = tmp_path / "My Original.mp4"
    source.write_bytes(b"x")
    out = tmp_path / "out"; out.mkdir()
    row = VideoRow("vid", "cid", "Channel", "Title", "url", "",
                   "downloaded", str(source), "processing")
    seen = []
    original_run = pl.run_cancellable
    original_process = pipe.process_one

    def fake_run(cmd, **_kwargs):
        pattern = cmd[-1]
        for number in (1, 2, 3):
            Path(pattern.replace("%d", str(number))).write_bytes(b"part")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_process(part_row):
        seen.append(Path(part_row.download_path).name)
        return EditOutputs(full=str(out / (Path(part_row.download_path).stem + "_full.mp4")))

    pl.run_cancellable = fake_run
    pipe.process_one = fake_process
    try:
        pipe._process_split_video(cfg.editor, row, str(source), out, 5)
    finally:
        pl.run_cancellable = original_run
        pipe.process_one = original_process
    assert seen == [
        "My Original - phần 1.mp4",
        "My Original - phần 2.mp4",
        "My Original - phần 3.mp4",
    ]


def test_extract_video_id_from_urls():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=30") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/shorts/abc123DEF_-") == "abc123DEF_-"
    assert extract_video_id("https://m.youtube.com/watch?feature=x&v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"        # đã là id
    assert extract_video_id("không phải link") == ""
    assert extract_video_id("") == ""


def test_video_date_range_is_inclusive_and_custom():
    assert in_date_range("2026-05-10T12:00:00+00:00", "2026-05-10", "2026-06-01")
    assert not in_date_range("2026-05-09", "2026-05-10", "2026-06-01")
    assert not in_date_range("2026-06-02", "2026-05-10", "2026-06-01")
    assert not in_date_range("", "2026-05-10", "")
    assert in_date_range("", "", "")


def test_download_quality_falls_back_without_exceeding_selected_height():
    fmt = quality_format(1080)
    assert "height<=1080" in fmt
    assert "height>=1080" not in fmt
    assert quality_format(720).startswith("bestvideo[height<=720]")
    assert quality_format(0) == "bestvideo+bestaudio/best"


def test_whisper_auto_falls_back_to_cpu_when_cuda_fails():
    original = transcribe._get_model
    calls = []

    class FakeModel:
        def __init__(self, device): self.device = device
        def transcribe(self, *_args, **_kwargs):
            calls.append(self.device)
            if self.device == "cuda":
                def broken():
                    raise RuntimeError("CUDA driver too old")
                    yield
                return broken(), type("Info", (), {"language": "en"})()
            seg = type("Seg", (), {"start": 0.0, "end": 1.0, "text": "ok"})()
            return iter([seg]), type("Info", (), {"language": "en"})()

    transcribe._get_model = lambda _size, device, _compute: FakeModel(device)
    try:
        cues, lang = transcribe.transcribe_segments("input.mp4", device="auto")
    finally:
        transcribe._get_model = original
    assert calls == ["cuda", "cpu"]
    assert lang == "en" and cues[0].text == "ok"


def test_safe_name_strips_forbidden():
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_name("  . trailing . ") == "trailing"
    assert safe_name("") == "untitled"
    assert len(safe_name("x" * 200)) <= 60


def test_output_filename_uses_original_source_name_and_legacy_fallback(tmp_path):
    new = output_video_path(tmp_path, "D:/input/My old video.mp4", "abc123", "full")
    assert new.name == "My old video_full.mp4"
    legacy = tmp_path / "abc123_full.mp4"; legacy.write_bytes(b"old")
    assert existing_output_video_path(
        tmp_path, "D:/input/My old video.mp4", "abc123", "full") == legacy


def test_output_cleanup_keeps_only_full_and_short_videos(tmp_path):
    out = tmp_path / "job"; out.mkdir()
    full = out / "source_full.mp4"; full.write_bytes(b"full")
    short = out / "source_short.mp4"; short.write_bytes(b"short")
    for name in ("source.srt", "source_content.txt", "source_audio.wav",
                 "source_edge_tts.mp3", "source_overlay.ass"):
        (out / name).write_bytes(b"temporary")
    sep = out / "source_sep"; sep.mkdir()
    (sep / "vocals.wav").write_bytes(b"temporary")

    pipe = EditPipeline(AppConfig(), ExcelStore(tmp_path / "data.xlsx"))
    pipe._cleanup_output_artifacts(out, (str(full), str(short)))

    assert {path.name for path in out.iterdir()} == {
        "source_full.mp4", "source_short.mp4"}


def test_video_dir_layout():
    p = video_dir("D:/root", "My Chan", "abc123")
    parts = p.as_posix()
    assert "My Chan" in parts and parts.endswith("abc123")


def test_extract_vid():
    assert _extract_vid("yt:video:ABCDEFG") == "ABCDEFG"
    assert _extract_vid("") == ""


def test_pitch_speed_filters():
    # không đổi gì -> rỗng
    assert pitch_speed_filters(0, 1.0) == []
    # chỉ tempo
    assert ",".join(pitch_speed_filters(0, 1.25)) == "atempo=1.25"
    # pitch tạo asetrate + atempo bù (giữ tempo -> atempo=0.5 khi +12 semitone)
    s = ",".join(pitch_speed_filters(12, 1.0))
    assert "asetrate=44100" in s and "atempo=0.5" in s
    # tempo lớn phải xâu chuỗi atempo (mỗi cái ≤ 2.0): 4x -> hai atempo=2.0
    assert pitch_speed_filters(0, 4.0).count("atempo=2.0") == 2


def test_cancellable_process_stops_immediately():
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    try:
        run_cancellable([sys.executable, "-c", "import time; time.sleep(10)"],
                        cancel_cb=stop.is_set)
        assert False, "process phải bị hủy"
    except EditCancelled:
        pass
    assert time.monotonic() - started < 3
