"""Test path an toàn Windows, bóc video id, filter audio."""
import sys
import threading
import time

from app.paths import safe_name, video_dir
from app.downloader.monitor import _extract_vid, extract_video_id, in_date_range
from app.editor.audio_ops import pitch_speed_filters
from app.editor.cancel import EditCancelled, run_cancellable
from app.downloader.downloader import quality_format
from app.editor import transcribe


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
